"""MiroFish 리포트 export — 백엔드 API → 공유 폴더(latest.json).

업스트림 MiroFish(666ghj/MiroFish)는 리포트를 `{success, data:{...}}` 봉투로
반환한다. 우리 파이프라인은 `data`(outline/markdown_content/report_id…)를
기대하므로 봉투를 벗겨 `<shared>/out/latest.json`에 저장한다.

실행:
    python -m src.mirofish_export --report-id report_xxxx
    python -m src.mirofish_export --simulation-id sim_xxxx
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

import requests

from .config import Settings
from .report_store import latest_report_path

logger = logging.getLogger(__name__)


class MiroFishError(RuntimeError):
    """MiroFish API 호출/응답 실패."""


class SupportsGetJson(Protocol):
    def get_json(self, path: str) -> Dict[str, Any]: ...


class MiroFishClient:
    """MiroFish 백엔드(기본 :5001) GET 클라이언트."""

    def __init__(self, base_url: str = "http://localhost:5001", timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _as_dict(self, resp: requests.Response, path: str) -> Dict[str, Any]:
        try:
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise MiroFishError(f"MiroFish 요청 실패 ({path}): {exc}") from exc
        if not isinstance(data, dict):
            raise MiroFishError(f"MiroFish 응답이 객체가 아님 ({path})")
        return data

    def get_json(self, path: str) -> Dict[str, Any]:
        try:
            resp = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
        except requests.RequestException as exc:
            raise MiroFishError(f"MiroFish GET 실패 ({path}): {exc}") from exc
        return self._as_dict(resp, path)

    def post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = requests.post(
                f"{self.base_url}{path}", json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise MiroFishError(f"MiroFish POST 실패 ({path}): {exc}") from exc
        return self._as_dict(resp, path)

    def post_files(
        self, path: str, *, files: Dict[str, Any], data: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            resp = requests.post(
                f"{self.base_url}{path}", files=files, data=data, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise MiroFishError(f"MiroFish 업로드 실패 ({path}): {exc}") from exc
        return self._as_dict(resp, path)


def _unwrap(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """{success, data} 봉투에서 data 추출. 실패면 MiroFishError."""
    if not envelope.get("success"):
        raise MiroFishError(f"MiroFish 응답 실패: {envelope.get('error')}")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise MiroFishError("응답에 data 객체가 없음")
    return data


def fetch_report(
    client: SupportsGetJson,
    *,
    report_id: Optional[str] = None,
    simulation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """리포트 dict(봉투 제거)를 가져온다. report_id 우선."""
    if report_id:
        path = f"/api/report/{report_id}"
    elif simulation_id:
        path = f"/api/report/by-simulation/{simulation_id}"
    else:
        raise MiroFishError("report_id 또는 simulation_id 가 필요함")
    report = _unwrap(client.get_json(path))
    status = report.get("status")
    if status != "completed":
        logger.warning("리포트가 완료 상태가 아님 (status=%s) — 그대로 저장", status)
    return report


def export_report(report: Dict[str, Any], shared_dir: str) -> Path:
    """리포트 dict를 `<shared>/out/latest.json`에 저장하고 경로 반환."""
    out = latest_report_path(shared_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("리포트 저장: %s (report_id=%s)", out, report.get("report_id"))
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="MiroFish 리포트 export → latest.json")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--report-id")
    group.add_argument("--simulation-id")
    args = parser.parse_args()

    settings = Settings.from_env()
    client = MiroFishClient(settings.mirofish_api_url)
    report = fetch_report(
        client, report_id=args.report_id, simulation_id=args.simulation_id
    )
    path = export_report(report, settings.mirofish_shared_dir)
    print(f"저장됨: {path}")


if __name__ == "__main__":
    main()
