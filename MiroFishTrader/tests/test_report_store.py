"""report_store 단위 테스트."""
from __future__ import annotations

import json

from src.report_store import load_latest_report


def _write_report(shared_dir, data):
    out = shared_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(data), encoding="utf-8")


def test_load_missing_returns_none(tmp_path):
    assert load_latest_report(str(tmp_path)) is None


def test_load_valid(tmp_path):
    _write_report(tmp_path, {"report_id": "r1"})
    report = load_latest_report(str(tmp_path))
    assert report == {"report_id": "r1"}


def test_load_corrupt_returns_none(tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "latest.json").write_text("{not json", encoding="utf-8")
    assert load_latest_report(str(tmp_path)) is None


def test_load_non_object_returns_none(tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "latest.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_latest_report(str(tmp_path)) is None
