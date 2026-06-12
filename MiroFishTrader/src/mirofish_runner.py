"""MiroFish 5단계 헤드리스 자동화 러너.

업스트림 MiroFish(666ghj/MiroFish) 백엔드 API를 순차 구동해
시드 문서 → 그래프 → 시뮬레이션 → 리포트 → `latest.json` 까지 자동화한다.

흐름 (모두 :5001):
  1. POST /api/graph/ontology/generate  (multipart: 시드 파일 + simulation_requirement) → project_id
  2. POST /api/graph/build {project_id} → task → poll GET /api/graph/task/{id}
  3. POST /api/simulation/create {project_id} → simulation_id
  4. POST /api/simulation/prepare {simulation_id} → poll POST /prepare/status
  5. POST /api/simulation/start {simulation_id, max_rounds} → poll GET /{id}/run-status
  6. POST /api/report/generate {simulation_id} → poll /generate/status → export

실행:
    python -m src.mirofish_runner --max-rounds 10
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict

from .config import Settings
from .mirofish_export import (
    MiroFishClient,
    MiroFishError,
    _unwrap,
    export_report,
    fetch_report,
)
from .polymarket import PolymarketClient
from .seed import generate_seed

logger = logging.getLogger(__name__)


def _progress_str(data: Dict[str, Any]) -> str:
    """폴링 데이터에서 사람이 읽을 진행률 문자열 추출."""
    if "progress_percent" in data:  # 시뮬레이션 실행
        return (
            f"{data.get('current_round', 0)}/{data.get('total_rounds', '?')} 라운드 "
            f"({data.get('progress_percent', 0)}%)"
        )
    if "progress" in data:  # 비동기 태스크 (빌드/리포트)
        msg = data.get("message", "")
        return f"{data.get('progress', 0)}%{' — ' + msg if msg else ''}"
    return str(data.get("status") or data.get("runner_status") or "...")


class MiroFishRunner:
    """MiroFish 백엔드를 단계별로 구동하는 오케스트레이터."""

    def __init__(
        self,
        client: MiroFishClient,
        *,
        shared_dir: str,
        max_rounds: int = 10,
        platform: str = "parallel",
        poll_interval: float = 5.0,
        poll_timeout: float = 3600.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.shared_dir = shared_dir
        self.max_rounds = max_rounds
        self.platform = platform
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._sleep = sleep

    def _poll(
        self,
        fetch: Callable[[], Dict[str, Any]],
        *,
        done: Callable[[Dict[str, Any]], bool],
        fail: Callable[[Dict[str, Any]], bool],
        label: str,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            data = fetch()
            if done(data):
                return data
            if fail(data):
                raise MiroFishError(f"{label} 실패: {data}")
            logger.info("%s 진행 중... %s", label, _progress_str(data))
            self._sleep(self.poll_interval)
        raise MiroFishError(f"{label} 타임아웃 ({self.poll_timeout}s)")

    # ── 단계별 ──
    def generate_ontology(self, seed_path: Path, requirement: str, project_name: str) -> str:
        with open(seed_path, "rb") as f:
            env = self.client.post_files(
                "/api/graph/ontology/generate",
                files={"files": (Path(seed_path).name, f, "text/markdown")},
                data={"simulation_requirement": requirement, "project_name": project_name},
            )
        return _unwrap(env)["project_id"]

    def build_graph(self, project_id: str) -> None:
        task_id = _unwrap(self.client.post_json("/api/graph/build", {"project_id": project_id}))["task_id"]
        self._poll(
            lambda: _unwrap(self.client.get_json(f"/api/graph/task/{task_id}")),
            done=lambda d: d.get("status") == "completed",
            fail=lambda d: d.get("status") == "failed",
            label="그래프 빌드",
        )

    def create_simulation(self, project_id: str) -> str:
        env = self.client.post_json("/api/simulation/create", {"project_id": project_id})
        return _unwrap(env)["simulation_id"]

    def prepare(self, sim_id: str) -> None:
        d = _unwrap(self.client.post_json("/api/simulation/prepare", {"simulation_id": sim_id}))
        if d.get("already_prepared") or d.get("status") in ("ready", "completed"):
            return
        self._poll(
            lambda: _unwrap(self.client.post_json("/api/simulation/prepare/status", {"simulation_id": sim_id})),
            done=lambda x: x.get("status") in ("ready", "completed"),
            fail=lambda x: x.get("status") == "failed",
            label="환경 준비",
        )

    def start(self, sim_id: str) -> None:
        self.client.post_json(
            "/api/simulation/start",
            {"simulation_id": sim_id, "max_rounds": self.max_rounds, "platform": self.platform},
        )
        self._poll(
            lambda: _unwrap(self.client.get_json(f"/api/simulation/{sim_id}/run-status")),
            done=lambda d: d.get("runner_status") == "completed",
            fail=lambda d: d.get("runner_status") in ("failed", "stopped"),
            label="시뮬레이션 실행",
        )

    def generate_report(self, sim_id: str) -> str:
        d = _unwrap(self.client.post_json("/api/report/generate", {"simulation_id": sim_id}))
        report_id, task_id = d["report_id"], d.get("task_id")
        if not d.get("already_generated"):
            self._poll(
                lambda: _unwrap(self.client.post_json(
                    "/api/report/generate/status", {"task_id": task_id, "simulation_id": sim_id})),
                done=lambda x: x.get("status") == "completed",
                fail=lambda x: x.get("status") == "failed",
                label="리포트 생성",
            )
        return report_id

    def run(self, seed_path: Path, requirement: str, *, project_name: str = "MiroFishTrader Daily") -> Path:
        """전체 5단계 실행 후 latest.json 경로 반환."""
        logger.info("① 온톨로지 생성 (시드 업로드)")
        project_id = self.generate_ontology(seed_path, requirement, project_name)
        logger.info("② 그래프 빌드 (project=%s)", project_id)
        self.build_graph(project_id)
        logger.info("③ 시뮬레이션 생성")
        sim_id = self.create_simulation(project_id)
        logger.info("④ 환경 준비 (sim=%s)", sim_id)
        self.prepare(sim_id)
        logger.info("⑤ 시뮬레이션 실행 (max_rounds=%d)", self.max_rounds)
        self.start(sim_id)
        logger.info("⑥ 리포트 생성")
        report_id = self.generate_report(sim_id)
        report = fetch_report(self.client, report_id=report_id)
        return export_report(report, self.shared_dir)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="MiroFish 5단계 자동 실행 → latest.json")
    parser.add_argument("--max-rounds", type=int, default=10, help="시뮬레이션 최대 라운드 (M2는 낮게)")
    args = parser.parse_args()

    settings = Settings.from_env()
    seed_path, requirement = generate_seed(
        PolymarketClient(), shared_dir=settings.mirofish_shared_dir
    )
    logger.info("시드 생성: %s", seed_path)

    client = MiroFishClient(settings.mirofish_api_url, timeout=300)
    runner = MiroFishRunner(
        client, shared_dir=settings.mirofish_shared_dir, max_rounds=args.max_rounds
    )
    out = runner.run(seed_path, requirement)
    print(f"latest.json 저장: {out}")


if __name__ == "__main__":
    main()
