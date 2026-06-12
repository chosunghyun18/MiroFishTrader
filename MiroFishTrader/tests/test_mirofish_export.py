"""mirofish_export 단위 테스트 (HTTP 없이 FakeClient)."""
from __future__ import annotations

import json

import pytest

from src.mirofish_export import MiroFishError, export_report, fetch_report
from src.report_store import load_latest_report

REPORT_DATA = {
    "report_id": "report_abc",
    "simulation_id": "sim_abc",
    "status": "completed",
    "outline": {"title": "T", "summary": "S", "sections": []},
    "markdown_content": "# T\n\nbody",
}


class FakeClient:
    def __init__(self, envelope):
        self.envelope = envelope
        self.last_path = None

    def get_json(self, path):
        self.last_path = path
        return self.envelope


def test_fetch_unwraps_envelope():
    client = FakeClient({"success": True, "data": REPORT_DATA})
    report = fetch_report(client, report_id="report_abc")
    assert report["report_id"] == "report_abc"
    assert client.last_path == "/api/report/report_abc"


def test_fetch_by_simulation_path():
    client = FakeClient({"success": True, "data": REPORT_DATA})
    fetch_report(client, simulation_id="sim_abc")
    assert client.last_path == "/api/report/by-simulation/sim_abc"


def test_fetch_failure_envelope_raises():
    client = FakeClient({"success": False, "error": "boom"})
    with pytest.raises(MiroFishError):
        fetch_report(client, report_id="x")


def test_fetch_requires_an_id():
    client = FakeClient({"success": True, "data": REPORT_DATA})
    with pytest.raises(MiroFishError):
        fetch_report(client)


def test_export_writes_loadable_report(tmp_path):
    path = export_report(REPORT_DATA, str(tmp_path))
    assert path.exists()
    # report_store가 다시 읽어낼 수 있어야 함 (파이프라인 호환)
    loaded = load_latest_report(str(tmp_path))
    assert loaded == REPORT_DATA
