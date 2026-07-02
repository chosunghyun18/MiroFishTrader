"""mirofish_runner 통합 테스트 — 라이브 서버 없이 FakeClient로 5단계 흐름 검증."""
from __future__ import annotations

from src.mirofish_export import MiroFishError
from src.mirofish_runner import MiroFishRunner
from src.report_store import load_latest_report

REPORT = {
    "report_id": "rep1",
    "simulation_id": "sim1",
    "status": "completed",
    "outline": {"title": "T", "summary": "S", "sections": []},
    "markdown_content": "# T\n\nbody",
}


def _ok(data):
    return {"success": True, "data": data}


class FakeClient:
    """엔드포인트별 정해진 응답을 돌려주는 가짜 MiroFish 백엔드."""

    def __init__(self, *, run_status_sequence=None):
        self.calls = []
        # run-status를 여러 번 폴링하는 흐름 검증용 시퀀스
        self._run_seq = list(run_status_sequence or ["running", "completed"])

    def post_files(self, path, *, files, data):
        self.calls.append(("POST_FILES", path))
        return _ok({"project_id": "proj1"})

    def post_json(self, path, payload):
        self.calls.append(("POST", path))
        if path == "/api/graph/build":
            return _ok({"task_id": "gtask"})
        if path == "/api/simulation/create":
            return _ok({"simulation_id": "sim1"})
        if path == "/api/simulation/prepare":
            return _ok({"status": "preparing", "task_id": "ptask"})
        if path == "/api/simulation/prepare/status":
            return _ok({"status": "ready"})
        if path == "/api/simulation/start":
            return _ok({"runner_status": "running"})
        if path == "/api/report/generate":
            return _ok({"report_id": "rep1", "task_id": "rtask", "already_generated": False})
        if path == "/api/report/generate/status":
            return _ok({"status": "completed"})
        raise AssertionError(f"예상치 못한 POST {path}")

    def get_json(self, path):
        self.calls.append(("GET", path))
        if path == "/api/graph/task/gtask":
            return _ok({"status": "completed"})
        if path == "/api/simulation/sim1/run-status":
            nxt = self._run_seq.pop(0) if len(self._run_seq) > 1 else self._run_seq[0]
            return _ok({"runner_status": nxt})
        if path == "/api/report/rep1":
            return _ok(REPORT)
        raise AssertionError(f"예상치 못한 GET {path}")


def _runner(client, shared_dir):
    return MiroFishRunner(
        client, shared_dir=str(shared_dir), poll_interval=0, sleep=lambda *_: None
    )


def test_full_flow_writes_latest(tmp_path):
    seed = tmp_path / "seed.md"
    seed.write_text("# seed", encoding="utf-8")
    client = FakeClient(run_status_sequence=["running", "running", "completed"])

    out = _runner(client, tmp_path).run(seed, "predict X")

    assert out.exists()
    assert load_latest_report(str(tmp_path)) == REPORT
    # 5단계 핵심 엔드포인트가 순서대로 호출됐는지
    paths = [p for _, p in client.calls]
    assert "/api/graph/ontology/generate" in paths
    assert "/api/graph/build" in paths
    assert "/api/simulation/create" in paths
    assert "/api/simulation/start" in paths
    assert "/api/report/generate" in paths


def test_run_polls_until_completed(tmp_path):
    seed = tmp_path / "seed.md"
    seed.write_text("# seed", encoding="utf-8")
    client = FakeClient(run_status_sequence=["running", "running", "completed"])
    _runner(client, tmp_path).run(seed, "predict X")
    # run-status가 여러 번 폴링됐는지
    run_polls = [c for c in client.calls if c == ("GET", "/api/simulation/sim1/run-status")]
    assert len(run_polls) >= 2


def test_run_fails_on_simulation_failure(tmp_path):
    seed = tmp_path / "seed.md"
    seed.write_text("# seed", encoding="utf-8")
    client = FakeClient(run_status_sequence=["failed"])
    import pytest

    with pytest.raises(MiroFishError):
        _runner(client, tmp_path).run(seed, "predict X")


def test_poll_backoff_grows_and_is_capped(tmp_path):
    """_poll이 poll_initial에서 시작해 poll_backoff 배로 증가하다 poll_interval에서 멈추는지."""
    import pytest

    client = FakeClient()
    runner = MiroFishRunner(
        client,
        shared_dir=str(tmp_path),
        poll_initial=1.0,
        poll_backoff=1.5,
        poll_interval=3.0,
        sleep=lambda s: sleeps.append(s),
    )
    sleeps: list[float] = []

    statuses = iter(["running"] * 6 + ["completed"])

    runner._poll(
        lambda: {"status": next(statuses)},
        done=lambda d: d.get("status") == "completed",
        fail=lambda d: d.get("status") == "failed",
        label="테스트",
    )

    assert sleeps[0] == 1.0
    assert sleeps[1] == 1.5
    assert sleeps[2] == pytest.approx(2.25)
    # 상한(poll_interval)을 넘지 않는지
    assert all(s <= 3.0 for s in sleeps)
    # 충분히 반복하면 상한에 도달해 더 이상 커지지 않는지
    assert sleeps[-1] == 3.0
    # 전체적으로 증가(비감소) 추세인지
    assert sleeps == sorted(sleeps)


def test_poll_interval_zero_disables_wait(tmp_path):
    """poll_interval(상한)이 0이면 poll_initial과 무관하게 첫 슬립부터 0이어야 함."""
    client = FakeClient()
    sleeps: list[float] = []
    runner = MiroFishRunner(
        client,
        shared_dir=str(tmp_path),
        poll_initial=1.0,
        poll_interval=0.0,
        sleep=lambda s: sleeps.append(s),
    )

    statuses = iter(["running", "completed"])
    runner._poll(
        lambda: {"status": next(statuses)},
        done=lambda d: d.get("status") == "completed",
        fail=lambda d: d.get("status") == "failed",
        label="테스트",
    )

    assert sleeps == [0.0]
