"""social (Reddit/Google Trends) 단위 테스트 (네트워크·praw·pytrends 없이 Fake 클라이언트)."""
from __future__ import annotations

from src.sources.social import (
    PrawRedditClient,
    PytrendsClient,
    RedditPost,
    TrendTopic,
    derive_interested_topics,
    fetch_reddit_hot,
    fetch_trends,
)


class FakeRedditClient:
    def __init__(self, posts=None, raise_exc=None):
        self._posts = posts if posts is not None else []
        self._raise = raise_exc

    def fetch_hot(self, subreddits, *, limit=15):
        if self._raise:
            raise self._raise
        return self._posts


class FakeTrendsClient:
    def __init__(self, keywords=None, raise_exc=None):
        self._keywords = keywords if keywords is not None else []
        self._raise = raise_exc

    def fetch_trending(self, *, geo="united_states"):
        if self._raise:
            raise self._raise
        return self._keywords


def test_fetch_reddit_hot_dedupes_by_title_and_caps():
    posts = [
        RedditPost("Fed cuts rates", "stocks", 100, "u1"),
        RedditPost("fed   cuts  rates", "investing", 50, "u2"),  # 대소문자/공백만 다른 중복
        RedditPost("GME to the moon", "wallstreetbets", 10, "u3"),
        RedditPost("Tech rally continues", "stocks", 5, "u4"),
    ]
    out = fetch_reddit_hot(["stocks"], FakeRedditClient(posts), max_results=2)
    assert [p.title for p in out] == ["Fed cuts rates", "GME to the moon"]


def test_fetch_reddit_hot_graceful_on_error():
    out = fetch_reddit_hot(["stocks"], FakeRedditClient(raise_exc=RuntimeError("boom")))
    assert out == []


def test_fetch_reddit_hot_empty_subreddits_skips_client():
    out = fetch_reddit_hot([], FakeRedditClient(raise_exc=AssertionError("불려선 안 됨")))
    assert out == []


def test_fetch_trends_maps_keywords_to_ranked_topics():
    out = fetch_trends(FakeTrendsClient(["Nvidia", "Fed", "GME"]))
    assert out == [
        TrendTopic("Nvidia", 1),
        TrendTopic("Fed", 2),
        TrendTopic("GME", 3),
    ]


def test_fetch_trends_dedupes_and_caps():
    out = fetch_trends(
        FakeTrendsClient(["Nvidia", "nvidia", "Fed", "GME"]), max_results=2
    )
    assert [t.keyword for t in out] == ["Nvidia", "Fed"]
    assert [t.rank for t in out] == [1, 2]


def test_fetch_trends_graceful_on_error():
    out = fetch_trends(FakeTrendsClient(raise_exc=RuntimeError("boom")))
    assert out == []


def test_derive_interested_topics_merges_and_caps():
    watchlist = ["AAPL", "TSLA"]
    trends = [
        TrendTopic("Nvidia", 1),
        TrendTopic("aapl", 2),  # 워치리스트와 대소문자만 다른 중복 -> 제외되어야 함
        TrendTopic("Fed", 3),
    ]
    reddit_posts = [
        RedditPost("Stocks rally as fed signals cut", "stocks", 100, "u1"),
        RedditPost("Tech rally continues into July", "stocks", 50, "u2"),
        RedditPost("GME to the moon again", "wallstreetbets", 20, "u3"),
    ]

    out = derive_interested_topics(reddit_posts, trends, watchlist, max_topics=6)

    assert out == ["AAPL", "TSLA", "Nvidia", "Fed", "rally", "stocks"]


def test_derive_interested_topics_watchlist_always_kept_first():
    watchlist = ["AAPL"]
    trends = [TrendTopic(f"kw{i}", i) for i in range(1, 10)]
    out = derive_interested_topics([], trends, watchlist, max_topics=3)
    assert out[0] == "AAPL"
    assert len(out) == 3


def test_praw_redditclient_returns_empty_without_praw_installed():
    client = PrawRedditClient(client_id="id", client_secret="secret", user_agent="ua")
    assert client.fetch_hot(["stocks"], limit=5) == []


def test_praw_redditclient_missing_credentials_returns_empty():
    client = PrawRedditClient(client_id="", client_secret="", user_agent="ua")
    assert client.fetch_hot(["stocks"], limit=5) == []


def test_pytrendsclient_returns_empty_without_pytrends_installed():
    client = PytrendsClient()
    assert client.fetch_trending(geo="united_states") == []
