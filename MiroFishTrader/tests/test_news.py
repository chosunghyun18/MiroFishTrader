"""news (GDELT) 단위 테스트 (HTTP 없이 FakeClient)."""
from __future__ import annotations

import requests

from src.sources.news import (
    Headline,
    build_query,
    dedupe,
    fetch_headlines,
    parse_article,
)

RAW_ARTICLES = [
    {
        "title": "Fed signals possible rate cut in July",
        "url": "https://ex.com/a",
        "domain": "ex.com",
        "seendate": "20260628T120000Z",
    },
    {
        "title": "Semiconductor stocks rally on AI demand",
        "url": "https://ex.com/b",
        "domain": "ex.com",
        "seendate": "20260628T110000Z",
    },
    {  # 제목 중복(대소문자/공백 차이) → 제거 대상
        "title": "Fed   signals  possible rate cut in July",
        "url": "https://ex.com/dup",
        "domain": "ex.com",
        "seendate": "20260628T100000Z",
    },
]


class FakeGdelt:
    def __init__(self, articles=None, raise_exc=None):
        self._articles = articles if articles is not None else RAW_ARTICLES
        self._raise = raise_exc

    def fetch_articles(self, query, *, max_records=20, timespan="1d"):
        if self._raise:
            raise self._raise
        return self._articles


def test_build_query_quotes_multiword():
    q = build_query(["fed", "interest rates", "  "])
    assert q == '(fed OR "interest rates") sourcelang:english'


def test_build_query_empty():
    assert build_query(["", "  "]) == ""


def test_parse_article():
    h = parse_article(RAW_ARTICLES[0])
    assert h.title == "Fed signals possible rate cut in July"
    assert h.domain == "ex.com"


def test_dedupe_removes_duplicates_and_caps():
    headlines = [parse_article(a) for a in RAW_ARTICLES]
    out = dedupe(headlines, max_results=10)
    assert len(out) == 2  # 중복 제거됨
    out2 = dedupe(headlines, max_results=1)
    assert len(out2) == 1


def test_dedupe_drops_empty_titles():
    out = dedupe([Headline("", "u", "d", "s")], max_results=5)
    assert out == []


def test_fetch_headlines_happy_path():
    out = fetch_headlines(["fed", "ai"], FakeGdelt(), max_records=10)
    assert [h.title for h in out] == [
        "Fed signals possible rate cut in July",
        "Semiconductor stocks rally on AI demand",
    ]


def test_fetch_headlines_no_keywords_skips_network():
    # 키워드 없으면 빈 쿼리 → 클라이언트 호출 없이 빈 결과
    out = fetch_headlines(["  "], FakeGdelt(raise_exc=AssertionError("불려선 안 됨")))
    assert out == []


def test_fetch_headlines_graceful_on_network_error():
    out = fetch_headlines(["fed"], FakeGdelt(raise_exc=requests.RequestException("x")))
    assert out == []


def test_fetch_headlines_graceful_on_parse_error():
    out = fetch_headlines(["fed"], FakeGdelt(raise_exc=ValueError("bad json")))
    assert out == []
