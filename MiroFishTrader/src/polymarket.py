"""Polymarket 예측시장 확률 조회 (Gamma API).

테마 키워드로 활성 마켓을 검색해 현재 Yes 확률과 일간 변화를 가져온다.
v1 최소 구현: 24시간 거래량 상위 마켓을 받아 키워드로 클라이언트 측 필터링.
네트워크/파싱 실패 시 빈 리스트를 돌려 파이프라인이 degrade 하도록 한다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


@dataclass
class PolymarketMarket:
    question: str
    yes_probability: Optional[float]  # 0.0~1.0
    volume_24hr: float
    day_change: Optional[float]
    url: str


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_yes_price(raw: Dict[str, Any]) -> Optional[float]:
    """outcomePrices(JSON 문자열 또는 리스트)에서 첫 항목(Yes) 확률 추출."""
    prices = raw.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except ValueError:
            return None
    if isinstance(prices, list) and prices:
        return _opt_float(prices[0])
    return None


def parse_market(raw: Dict[str, Any]) -> PolymarketMarket:
    slug = str(raw.get("slug", "")).strip()
    url = f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com"
    return PolymarketMarket(
        question=str(raw.get("question", "")).strip(),
        yes_probability=_parse_yes_price(raw),
        volume_24hr=_to_float(raw.get("volume24hr")),
        day_change=_opt_float(raw.get("oneDayPriceChange")),
        url=url,
    )


def filter_markets(
    markets: List[PolymarketMarket],
    keywords: List[str],
    *,
    max_results: int = 5,
) -> List[PolymarketMarket]:
    """질문 텍스트에 키워드가 포함된 마켓만 (입력 순서=거래량순 유지)."""
    kws = [k.strip().lower() for k in keywords if k.strip()]
    out: List[PolymarketMarket] = []
    seen: set[str] = set()
    for m in markets:
        q = m.question.lower()
        if m.question and m.question not in seen and any(kw in q for kw in kws):
            seen.add(m.question)
            out.append(m)
        if len(out) >= max_results:
            break
    return out


class SupportsFetchMarkets(Protocol):
    def fetch_markets(self, limit: int = 100) -> List[Dict[str, Any]]: ...


class PolymarketClient:
    """Gamma API에서 활성 마켓을 거래량순으로 가져오는 클라이언트."""

    def __init__(self, base_url: str = GAMMA_BASE, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def fetch_markets(self, limit: int = 100) -> List[Dict[str, Any]]:
        params = {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": limit,
        }
        resp = requests.get(
            f"{self.base_url}/markets", params=params, timeout=self.timeout
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


def find_markets(
    keywords: List[str],
    client: SupportsFetchMarkets,
    *,
    fetch_limit: int = 100,
    max_results: int = 5,
) -> List[PolymarketMarket]:
    """키워드 관련 예측시장 조회. 실패 시 빈 리스트 (graceful)."""
    if not [k for k in keywords if k.strip()]:
        return []
    try:
        raw = client.fetch_markets(limit=fetch_limit)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Polymarket 조회 실패: %s", exc)
        return []
    parsed = [parse_market(m) for m in raw if isinstance(m, dict)]
    return filter_markets(parsed, keywords, max_results=max_results)
