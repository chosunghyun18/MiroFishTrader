"""리포트 조립 — 추출 신호 + 티커 + Polymarket → Slack 메시지.

Slack Incoming Webhook의 `blocks` 페이로드를 생성한다.
순수 함수로 구현해 LLM/네트워크 없이 테스트 가능하다.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .mapper import MappingResult
from .models import ExtractedSignal
from .polymarket import PolymarketMarket

_TREND_EMOJI = {"bullish": "📈", "bearish": "📉", "neutral": "➖"}


def _signal_line(signal: ExtractedSignal) -> str:
    emoji = _TREND_EMOJI.get(signal.trend_direction, "➖")
    pct = round(signal.confidence * 100)
    return f"{emoji} *추세: {signal.trend_direction}* (확신도 {pct}%)"


def _tickers_line(mapping: MappingResult) -> str:
    if not mapping.tickers:
        return "• 관련 티커: _매칭 없음_"
    return "• 관련 티커: " + ", ".join(f"`{t}`" for t in mapping.tickers)


def _polymarket_lines(markets: List[PolymarketMarket]) -> List[str]:
    if not markets:
        return ["• Polymarket: _관련 마켓 없음_"]
    lines = ["*Polymarket 예측시장*"]
    for m in markets:
        prob = (
            f"{round(m.yes_probability * 100)}%"
            if m.yes_probability is not None
            else "N/A"
        )
        chg = ""
        if m.day_change is not None:
            arrow = "▲" if m.day_change >= 0 else "▼"
            chg = f" ({arrow}{abs(round(m.day_change * 100, 1))}p)"
        lines.append(f"• <{m.url}|{m.question}> — Yes {prob}{chg}")
    return lines


def build_report_text(
    signal: ExtractedSignal,
    mapping: MappingResult,
    markets: List[PolymarketMarket],
) -> str:
    """Slack mrkdwn 본문 텍스트 (blocks 미지원 환경 폴백 겸용)."""
    parts: List[str] = [
        f"*MiroFishTrader 일일 리포트 — {signal.date}*",
        "",
        _signal_line(signal),
    ]
    if signal.summary:
        parts.append(f"> {signal.summary}")
    if signal.themes:
        parts.append("• 테마: " + ", ".join(signal.themes))
    parts.append(_tickers_line(mapping))
    parts.append("")
    parts.extend(_polymarket_lines(markets))
    return "\n".join(parts)


def build_slack_payload(
    signal: ExtractedSignal,
    mapping: MappingResult,
    markets: List[PolymarketMarket],
) -> Dict[str, Any]:
    """Slack Incoming Webhook 페이로드 (blocks + text 폴백)."""
    text = build_report_text(signal, mapping, markets)
    return {
        "text": f"MiroFishTrader 일일 리포트 — {signal.date}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}}
        ],
    }
