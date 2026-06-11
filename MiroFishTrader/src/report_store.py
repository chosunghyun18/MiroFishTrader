"""MiroFish 공유 폴더에서 리포트 JSON을 읽는다.

연동 계약(`mirofish-integration.md`)에 따라 `<shared>/out/latest.json`을 소비한다.
파일 부재·손상 시 예외 대신 None을 반환해 파이프라인이 graceful degrade 하도록 한다.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def latest_report_path(shared_dir: str) -> Path:
    return Path(shared_dir) / "out" / "latest.json"


def load_latest_report(shared_dir: str) -> Optional[Dict[str, Any]]:
    """가장 최근 MiroFish 리포트를 로드. 없거나 손상 시 None."""
    path = latest_report_path(shared_dir)
    if not path.is_file():
        logger.warning("MiroFish 리포트 없음: %s", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("리포트 로드 실패 (%s): %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.error("리포트 최상위가 객체가 아님: %s", path)
        return None
    return data


def report_age_hours(report: Dict[str, Any]) -> Optional[float]:
    """리포트 완료 시각 기준 경과 시간(시간). 파싱 불가 시 None."""
    import datetime as _dt

    stamp = report.get("completed_at") or report.get("created_at")
    if not stamp:
        return None
    try:
        completed = _dt.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    delta = _dt.datetime.now() - completed
    return delta.total_seconds() / 3600.0


def default_shared_dir() -> str:
    return os.getenv("MIROFISH_SHARED_DIR", "./shared/mirofish")
