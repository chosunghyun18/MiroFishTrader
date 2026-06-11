"""Slack Incoming Webhook 전송.

기존에 쓰던 Webhook URL을 재사용한다 (`SLACK_WEBHOOK_URL`).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)


class SlackError(RuntimeError):
    """Slack 전송 실패."""


def webhook_url_from_env() -> str:
    return os.getenv("SLACK_WEBHOOK_URL", "").strip()


def send_payload(payload: Dict[str, Any], webhook_url: str, *, timeout: int = 15) -> None:
    """Webhook으로 페이로드 전송. 실패 시 SlackError."""
    if not webhook_url:
        raise SlackError("SLACK_WEBHOOK_URL 미설정")
    try:
        resp = requests.post(webhook_url, json=payload, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SlackError(f"Slack 전송 실패: {exc}") from exc
    logger.info("Slack 전송 완료")
