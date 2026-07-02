"""시드 생성 — 그날의 시장 컨텍스트를 MiroFish 입력 문서로 생성.

MiroFish는 "시드 자료(문서) + 자연어 예측 요구사항"을 입력으로 받는다.
여기서는 Polymarket 예측시장 신호 + 뉴스 헤드라인 + 시세/매크로 지표 +
Reddit/Google Trends 군중 관심 신호 + 관심 섹터를 묶어 마크다운 시드 문서와
예측 요구사항 문자열을 만들어 `<shared>/in/seed-YYYYMMDD.md` 에 저장한다.

각 소스는 서로 독립적으로 병렬 조회한다(ThreadPoolExecutor). 한 소스가
실패해도 다른 소스는 영향받지 않고, 실패한 소스는 해당 섹션만 비운 채
(graceful degrade) 시드 생성을 계속한다.
"""
from __future__ import annotations

import concurrent.futures
import datetime as _dt
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .polymarket import PolymarketMarket, SupportsFetchMarkets, find_markets
from .sources.market import (
    DEFAULT_MACRO_SERIES,
    DEFAULT_SYMBOLS,
    MacroIndicator,
    Quote,
    SupportsFetchQuote,
    SupportsFetchSeries,
    fetch_macro,
    fetch_quotes,
)
from .sources.news import Headline, SupportsFetchArticles, fetch_headlines
from .sources.social import (
    DEFAULT_SUBREDDITS,
    RedditPost,
    SupportsFetchRedditHot,
    SupportsFetchTrends,
    TrendTopic,
    derive_interested_topics,
    fetch_reddit_hot,
    fetch_trends,
)

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
    quotes: Optional[List[Quote]] = None,
    macro: Optional[List[MacroIndicator]] = None,
    reddit_posts: Optional[List[RedditPost]] = None,
    trends: Optional[List[TrendTopic]] = None,
    topics: Optional[List[str]] = None,
) -> str:
    topics_display = topics if topics is not None else watchlist
    lines = [
        f"# Market Context — {date}",
        "",
        f"> Data as of {generated_at or date} — treat the items below as the most "
        "recent real-world information available; do not assume newer events.",
        "",
        "## Focus Sectors",
    ]
    lines += [f"- {s}" for s in watchlist]
    lines += ["", "## Interested Topics"]
    if topics_display:
        lines += [f"- {t}" for t in topics_display]
    else:
        lines.append("- (none)")
    lines += ["", "## Latest Headlines"]
    if headlines:
        for h in headlines:
            src = f" ({h.domain})" if h.domain else ""
            lines.append(f"- {h.title}{src}")
    else:
        lines.append("- (no recent headlines available)")
    lines += ["", "## Market Snapshot"]
    if quotes or macro:
        for q in quotes or []:
            change = (
                f" ({q.day_change_pct:+.2f}%)" if q.day_change_pct is not None else ""
            )
            lines.append(f"- {q.symbol}: {q.price}{change}")
        for m in macro or []:
            lines.append(f"- {m.name}: {m.value} (as of {m.asof})")
    else:
        lines.append("- (no market data available)")
    lines += ["", "## Social Attention"]
    if reddit_posts or trends:
        for p in reddit_posts or []:
            lines.append(f"- r/{p.subreddit}: {p.title} (score {p.score})")
        for t in trends or []:
            lines.append(f"- Trending: {t.keyword}")
    else:
        lines.append("- (no social data available)")
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
        "These headlines, prediction markets, market/macro data, social attention, "
        "and focus sectors represent current public and market attention. Use them "
        "as seed signals for crowd-sentiment simulation.",
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
    market_quote_clients: Optional[List[SupportsFetchQuote]] = None,
    fred_client: Optional[SupportsFetchSeries] = None,
    reddit_client: Optional[SupportsFetchRedditHot] = None,
    trends_client: Optional[SupportsFetchTrends] = None,
    symbols: Optional[List[str]] = None,
    macro_series: Optional[List[Tuple[str, str]]] = None,
    subreddits: Optional[List[str]] = None,
) -> Tuple[Path, str]:
    """시드 문서 작성 + 예측 요구사항 반환. (seed_path, requirement).

    Polymarket + (선택) GDELT 뉴스 + (선택) 시세/매크로 + (선택) Reddit/Trends를
    모두 병렬로 조회한다. 각 클라이언트가 주어지지 않으면 해당 섹션은 비운 채
    진행하고(기존 동작과 동일), 주어졌더라도 조회 중 예외가 발생하면 그 소스만
    빈 결과로 degrade 시키고 나머지 소스와 시드 생성 자체는 계속 진행한다.
    """
    wl = watchlist or DEFAULT_WATCHLIST
    day = date or _today()
    syms = symbols or DEFAULT_SYMBOLS
    macro_defs = macro_series or DEFAULT_MACRO_SERIES
    subs = subreddits or DEFAULT_SUBREDDITS

    def _fetch_markets() -> List[PolymarketMarket]:
        return find_markets(FINANCE_KEYWORDS, pm_client, max_results=max_markets)

    def _fetch_headlines() -> List[Headline]:
        if news_client is None:
            return []
        return fetch_headlines(
            FINANCE_KEYWORDS + wl, news_client, max_records=max_headlines
        )

    def _fetch_quotes() -> List[Quote]:
        if not market_quote_clients:
            return []
        return fetch_quotes(syms, market_quote_clients)

    def _fetch_macro() -> List[MacroIndicator]:
        if fred_client is None:
            return []
        return fetch_macro(macro_defs, fred_client)

    def _fetch_reddit() -> List[RedditPost]:
        if reddit_client is None:
            return []
        return fetch_reddit_hot(subs, reddit_client)

    def _fetch_trends() -> List[TrendTopic]:
        if trends_client is None:
            return []
        return fetch_trends(trends_client)

    tasks: Dict[str, Callable[[], Any]] = {
        "markets": _fetch_markets,
        "headlines": _fetch_headlines,
        "quotes": _fetch_quotes,
        "macro": _fetch_macro,
        "reddit": _fetch_reddit,
        "trends": _fetch_trends,
    }

    results: Dict[str, Any] = {name: [] for name in tasks}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 - 소스 하나의 실패가 전체를 막지 않도록
                logger.warning("%s 조회 실패(무시하고 진행): %s", name, exc)
                results[name] = []

    markets: List[PolymarketMarket] = results["markets"]
    headlines: List[Headline] = results["headlines"]
    quotes: List[Quote] = results["quotes"]
    macro: List[MacroIndicator] = results["macro"]
    reddit_posts: List[RedditPost] = results["reddit"]
    trends: List[TrendTopic] = results["trends"]

    topics = derive_interested_topics(reddit_posts, trends, wl)

    text = build_seed_markdown(
        markets,
        wl,
        day,
        headlines,
        generated_at=_now_utc(),
        quotes=quotes,
        macro=macro,
        reddit_posts=reddit_posts,
        trends=trends,
        topics=topics,
    )
    path = write_seed(text, shared_dir, day)
    return path, build_simulation_requirement(topics or wl)
