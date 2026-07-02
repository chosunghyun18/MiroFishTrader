"""seed 단위 테스트 (HTTP 없이 FakeClient)."""
from __future__ import annotations

from src.seed import (
    DEFAULT_WATCHLIST,
    build_seed_markdown,
    build_simulation_requirement,
    generate_seed,
)
from src.polymarket import PolymarketMarket
from src.sources.market import MacroIndicator, Quote
from src.sources.social import RedditPost, TrendTopic


class FakePM:
    def fetch_markets(self, limit=100):
        return [
            {
                "question": "Will the Fed cut rates in July?",
                "slug": "fed",
                "outcomePrices": ["0.6", "0.4"],
                "volume24hr": 50000,
            },
            {
                "question": "Will Spain win the World Cup?",  # 무관 — 매칭 안 됨
                "slug": "spain",
                "outcomePrices": ["0.2", "0.8"],
                "volume24hr": 10000,
            },
        ]


def test_requirement_mentions_sectors():
    req = build_simulation_requirement(["semiconductors", "AI"])
    assert "semiconductors" in req and "AI" in req


def test_seed_markdown_structure():
    markets = [PolymarketMarket("Fed cut?", 0.6, 50000, 0.0, "u")]
    md = build_seed_markdown(markets, ["semiconductors"], "2026-06-12")
    assert "# Market Context — 2026-06-12" in md
    assert "- semiconductors" in md
    assert "Fed cut?" in md and "60%" in md


def test_seed_markdown_empty_markets():
    md = build_seed_markdown([], ["AI"], "2026-06-12")
    assert "no relevant markets" in md


def test_generate_seed_writes_file(tmp_path):
    path, req = generate_seed(FakePM(), shared_dir=str(tmp_path), date="2026-06-12")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    # 금융 키워드(rates)로 Fed 마켓은 잡히고, Spain은 제외
    assert "Will the Fed cut rates in July?" in content
    assert "Spain" not in content
    assert "semiconductors" in req  # 기본 워치리스트 반영
    assert path.name == "seed-2026-06-12.md"


def test_default_watchlist_used(tmp_path):
    _, req = generate_seed(FakePM(), shared_dir=str(tmp_path))
    for sector in DEFAULT_WATCHLIST:
        assert sector in req


class FakeNews:
    def fetch_articles(self, query, *, max_records=20, timespan="1d"):
        return [
            {
                "title": "Chip stocks jump on AI demand",
                "url": "https://ex.com/x",
                "domain": "ex.com",
                "seendate": "20260628T120000Z",
            }
        ]


def test_seed_markdown_has_timestamp_and_headlines():
    from src.sources.news import Headline

    md = build_seed_markdown(
        [],
        ["AI"],
        "2026-06-28",
        headlines=[Headline("Chip stocks jump", "u", "ex.com", "s")],
        generated_at="2026-06-28 09:00 UTC",
    )
    assert "Data as of 2026-06-28 09:00 UTC" in md
    assert "## Latest Headlines" in md
    assert "Chip stocks jump (ex.com)" in md


def test_seed_markdown_no_headlines_degrades():
    md = build_seed_markdown([], ["AI"], "2026-06-28")
    assert "no recent headlines available" in md


def test_generate_seed_includes_news_when_client_given(tmp_path):
    path, _ = generate_seed(
        FakePM(), shared_dir=str(tmp_path), news_client=FakeNews(), date="2026-06-28"
    )
    content = path.read_text(encoding="utf-8")
    assert "Chip stocks jump on AI demand" in content
    assert "Data as of" in content


def test_generate_seed_without_news_client_skips_headlines(tmp_path):
    path, _ = generate_seed(FakePM(), shared_dir=str(tmp_path), date="2026-06-28")
    content = path.read_text(encoding="utf-8")
    assert "no recent headlines available" in content


# ---------------------------------------------------------------------------
# (a) build_seed_markdown — Market Snapshot / Social Attention 섹션
# ---------------------------------------------------------------------------


def test_seed_markdown_market_snapshot_and_social_attention():
    quotes = [Quote("spy.us", 550.2, 1.234, "2026-07-01")]
    macro = [MacroIndicator("DFF", "Fed Funds Rate", 5.25, "2026-06-01")]
    reddit_posts = [RedditPost("NVDA to the moon", "wallstreetbets", 500, "u")]
    trends = [TrendTopic("nvidia earnings", 1)]
    md = build_seed_markdown(
        [],
        ["AI"],
        "2026-07-02",
        quotes=quotes,
        macro=macro,
        reddit_posts=reddit_posts,
        trends=trends,
    )
    assert "## Market Snapshot" in md
    assert "- spy.us: 550.2 (+1.23%)" in md
    assert "- Fed Funds Rate: 5.25 (as of 2026-06-01)" in md
    assert "## Social Attention" in md
    assert "- r/wallstreetbets: NVDA to the moon (score 500)" in md
    assert "- Trending: nvidia earnings" in md


def test_seed_markdown_market_snapshot_omits_change_when_none():
    quotes = [Quote("qqq.us", 480.0, None, "2026-07-01")]
    md = build_seed_markdown([], ["AI"], "2026-07-02", quotes=quotes)
    assert "- qqq.us: 480.0" in md
    assert "480.0 (" not in md


def test_seed_markdown_market_and_social_degrade_when_empty():
    md = build_seed_markdown([], ["AI"], "2026-07-02")
    assert "## Market Snapshot" in md
    assert "- (no market data available)" in md
    assert "## Social Attention" in md
    assert "- (no social data available)" in md


# ---------------------------------------------------------------------------
# (b) generate_seed — 시세/Reddit/Trends 클라이언트 주입 + interested_topics
# ---------------------------------------------------------------------------


class FakeQuoteClient:
    def fetch_quote(self, symbol):
        return Quote(symbol, 100.0, 2.5, "2026-07-02")


class FakeFredClient:
    def fetch_series(self, series_id):
        return {"observations": [{"date": "2026-07-01", "value": "5.25"}]}


class FakeRedditClient:
    def fetch_hot(self, subreddits, *, limit=15):
        return [RedditPost("Nvidia earnings beat expectations", "stocks", 900, "u")]


class FakeTrendsClient:
    def fetch_trending(self, *, geo="united_states"):
        return ["nvidia earnings", "fed rate decision"]


def test_generate_seed_with_market_reddit_trends_clients(tmp_path):
    path, req = generate_seed(
        FakePM(),
        shared_dir=str(tmp_path),
        date="2026-07-02",
        market_quote_clients=[FakeQuoteClient()],
        fred_client=FakeFredClient(),
        reddit_client=FakeRedditClient(),
        trends_client=FakeTrendsClient(),
        symbols=["spy.us"],
    )
    content = path.read_text(encoding="utf-8")
    assert "## Market Snapshot" in content
    assert "spy.us: 100.0" in content
    assert "Fed Funds Rate: 5.25" in content
    assert "## Social Attention" in content
    assert "r/stocks: Nvidia earnings beat expectations (score 900)" in content
    assert "Trending: nvidia earnings" in content
    assert "## Interested Topics" in content
    # trends/reddit에서 파생된 관심 토픽이 시드와 시뮬레이션 요구사항에 반영되는지
    assert "- nvidia earnings" in content
    assert "nvidia earnings" in req


# ---------------------------------------------------------------------------
# (c) 부분 실패(partial degrade) — 한 소스가 예외를 던져도 나머지는 정상 생성
# ---------------------------------------------------------------------------


class RaisingPM:
    """find_markets이 잡지 않는 예외(RuntimeError)를 던지는 폴리마켓 클라이언트."""

    def fetch_markets(self, limit=100):
        raise RuntimeError("polymarket boom")


class RaisingQuoteClient:
    def fetch_quote(self, symbol):
        raise RuntimeError("quote boom")


def test_generate_seed_partial_degrade_polymarket_failure(tmp_path):
    """Polymarket 조회가 (find_markets이 잡지 않는) 예외로 실패해도 다른 섹션은 살아있다."""
    path, _ = generate_seed(
        RaisingPM(),
        shared_dir=str(tmp_path),
        date="2026-07-02",
        news_client=FakeNews(),
        market_quote_clients=[FakeQuoteClient()],
    )
    content = path.read_text(encoding="utf-8")
    assert "no relevant markets found" in content  # 실패한 섹션은 degrade
    assert "Chip stocks jump on AI demand" in content  # 뉴스는 정상
    assert "spy.us: 100.0" in content  # 시세도 정상


def test_generate_seed_partial_degrade_quote_client_failure(tmp_path):
    """시세 클라이언트가 실패해도 마켓/뉴스 등 다른 섹션은 정상 생성된다."""
    path, _ = generate_seed(
        FakePM(),
        shared_dir=str(tmp_path),
        date="2026-07-02",
        news_client=FakeNews(),
        market_quote_clients=[RaisingQuoteClient()],
    )
    content = path.read_text(encoding="utf-8")
    assert "Will the Fed cut rates in July?" in content
    assert "Chip stocks jump on AI demand" in content
    assert "- (no market data available)" in content
