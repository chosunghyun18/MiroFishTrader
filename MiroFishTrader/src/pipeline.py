"""일일 리포트 파이프라인 오케스트레이션.

흐름: latest.json 로드 → 신호 추출 → 티커 매핑 + Polymarket → Slack 전송.
각 단계는 실패해도 가능한 한 부분 리포트를 전송하도록 degrade 한다.

실행:
    python -m src.pipeline            # 실제 전송
    python -m src.pipeline --dry-run  # 전송 없이 페이로드만 출력
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
from typing import Any, Dict, Optional

from .config import Settings
from .extractor import extract_signal
from .llm import LLMError, OllamaClient, SupportsComplete
from .mapper import TickerMap, map_signal
from .models import ExtractedSignal
from .polymarket import PolymarketClient, SupportsFetchMarkets, find_markets
from .report_store import load_latest_report
from .reporter import build_slack_payload
from .slack import SlackError, send_payload

logger = logging.getLogger(__name__)


def _safe_extract(report: Dict[str, Any], llm: SupportsComplete) -> ExtractedSignal:
    """추출 실패 시 중립 신호로 degrade (리포트 메타데이터 유지)."""
    try:
        return extract_signal(report, llm)
    except (ValueError, LLMError) as exc:
        logger.warning("신호 추출 실패 → 중립 degrade: %s", exc)
        outline = report.get("outline") or {}
        return ExtractedSignal(
            date=_dt.date.today().isoformat(),
            source_report_id=str(report.get("report_id", "")),
            summary=str(outline.get("summary", "")).strip(),
        )


def run_pipeline(
    settings: Settings,
    *,
    llm: SupportsComplete,
    pm_client: SupportsFetchMarkets,
    dry_run: bool = False,
) -> Optional[Dict[str, Any]]:
    """파이프라인 1회 실행. 전송한(또는 dry-run) 페이로드 반환. 리포트 없으면 None."""
    report = load_latest_report(settings.mirofish_shared_dir)
    if report is None:
        payload = {"text": "오늘 MiroFish 리포트가 없습니다."}
        if not dry_run:
            send_payload(payload, settings.slack_webhook_url)
        return payload

    signal = _safe_extract(report, llm)
    mapping = map_signal(signal, TickerMap.load(settings.ticker_map_path))
    markets = find_markets(signal.themes, pm_client)
    payload = build_slack_payload(signal, mapping, markets)

    if not dry_run:
        send_payload(payload, settings.slack_webhook_url)
    return payload


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="MiroFishTrader 일일 리포트")
    parser.add_argument("--dry-run", action="store_true", help="전송 없이 페이로드 출력")
    args = parser.parse_args()

    settings = Settings.from_env()
    llm = OllamaClient(settings.llm_base_url, settings.llm_model)
    pm_client = PolymarketClient()
    try:
        payload = run_pipeline(
            settings, llm=llm, pm_client=pm_client, dry_run=args.dry_run
        )
    except SlackError as exc:
        logger.error("Slack 전송 실패: %s", exc)
        logger.error("→ .env의 SLACK_WEBHOOK_URL 설정을 확인하세요.")
        raise SystemExit(1)

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
