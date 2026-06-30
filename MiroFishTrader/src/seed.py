"""시드 생성 — 그날의 시장 컨텍스트를 MiroFish 입력 문서로 생성.

MiroFish는 "시드 자료(문서) + 자연어 예측 요구사항"을 입력으로 받는다.
여기서는 Polymarket 예측시장 신호 + 관심 섹터를 묶어 마크다운 시드 문서와
예측 요구사항 문자열을 만들어 `<shared>/in/seed-YYYYMMDD.md` 에 저장한다.

뉴스 소스 연동은 추후(v2). 현재는 Polymarket + 고정 워치리스트.
"""
from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from .polymarket import PolymarketMarket, SupportsFetchMarkets, find_markets
from .sources.news import Headline, SupportsFetchArticles, fetch_headlines

logger = logging.getLogger(__name__)

# Polymarket에서 금융 관련 마켓을 찾기 위한 검색 키워드
FINANCE_KEYWORDS = [
    "rates", "fed", "recession", "inflation", "stocks",
    "semiconductors", "ai", "oil", "bitcoin", "tariff",
]

# 시뮬레이션 관심 섹터 (기본 워치리스트)
DEFAULT_WATCHLIST = ["semiconductors", "AI", "rates", "energy"]


def _today() -> str:
    return _dt.date.today().isoformat()


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_simulation_requirement(watchlist: List[str]) -> str:
    sectors = ", ".join(watchlist)
    return (
        "Simulate near-term (1-8 week) market sentiment and likely price direction "
        f"for the focus sectors ({sectors}), using the prediction-market signals "
        "in the seed document as indicators of crowd expectations. Identify which "
        "sectors/assets show bullish vs bearish crowd sentiment."
    )


def build_seed_markdown(
    markets: List[PolymarketMarket],
    watchlist: List[str],
    date: str,
    headlines: Optional[List[Headline]] = None,
    generated_at: Optional[str] = None,
) -> str:
    lines = [
        f"# Market Context — {date}",
        "",
        f"> Data as of {generated_at or date} — treat the items below as the most "
        "recent real-world information available; do not assume newer events.",
        "",
        "## Focus Sectors",
    ]
    lines += [f"- {s}" for s in watchlist]
    lines += ["", "## Latest Headlines"]
    if headlines:
        for h in headlines:
            src = f" ({h.domain})" if h.domain else ""
            lines.append(f"- {h.title}{src}")
    else:
        lines.append("- (no recent headlines available)")
    lines += ["", "## Prediction Market Signals (Polymarket)"]
    if markets:
        for m in markets:
            prob = (
                f"{round(m.yes_probability * 100)}%"
                if m.yes_probability is not None
                else "N/A"
            )
            lines.append(f"- {m.question} — Yes {prob} (24h vol {round(m.volume_24hr)})")
    else:
        lines.append("- (no relevant markets found)")
    lines += [
        "",
        "## Context",
        "These headlines, prediction markets, and focus sectors represent current "
        "public and market attention. Use them as seed signals for crowd-sentiment "
        "simulation.",
    ]
    return "\n".join(lines) + "\n"


def write_seed(text: str, shared_dir: str, date: str) -> Path:
    out = Path(shared_dir) / "in" / f"seed-{date}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    logger.info("시드 저장: %s", out)
    return out


def generate_seed(
    pm_client: SupportsFetchMarkets,
    *,
    shared_dir: str,
    watchlist: Optional[List[str]] = None,
    date: Optional[str] = None,
    max_markets: int = 8,
    news_client: Optional[SupportsFetchArticles] = None,
    max_headlines: int = 8,
) -> Tuple[Path, str]:
    """시드 문서 작성 + 예측 요구사항 반환. (seed_path, requirement).

    news_client가 주어지면 최신 헤드라인을 시드에 포함한다(실패 시 graceful).
    주어지지 않으면 헤드라인 없이 진행(기존 동작과 동일).
    """
    wl = watchlist or DEFAULT_WATCHLIST
    day = date or _today()
    markets = find_markets(FINANCE_KEYWORDS, pm_client, max_results=max_markets)
    headlines: List[Headline] = []
    if news_client is not None:
        headlines = fetch_headlines(
            FINANCE_KEYWORDS + wl, news_client, max_records=max_headlines
        )
    text = build_seed_markdown(markets, wl, day, headlines, generated_at=_now_utc())
    path = write_seed(text, shared_dir, day)
    return path, build_simulation_requirement(wl)
