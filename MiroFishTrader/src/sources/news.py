"""GDELT 2.0 DOC API에서 최신 뉴스 헤드라인 조회.

무료·키 불필요. 금융 관련 키워드로 최근 기사를 시간 역순(최신 우선)으로 가져온다.
네트워크/파싱 실패 시 빈 리스트를 돌려 시드 생성이 degrade 하도록 한다.
(폴리마켓 모듈과 동일한 DI 패턴: Protocol 클라이언트 주입 → 테스트 시 HTTP 없이.)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

import requests

logger = logging.getLogger(__name__)

GDELT_DOC_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"


@dataclass
class Headline:
    title: str
    url: str
    domain: str
    seendate: str  # GDELT 포맷 예: "20260628T120000Z"


def build_query(keywords: List[str]) -> str:
    """키워드를 GDELT 쿼리 문자열로. 여러 단어 키워드는 따옴표로 묶는다.

    예: ["fed", "interest rates"] -> '(fed OR "interest rates") sourcelang:english'
    """
    terms: List[str] = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        terms.append(f'"{kw}"' if " " in kw else kw)
    if not terms:
        return ""
    joined = " OR ".join(terms)
    return f"({joined}) sourcelang:english"


def parse_article(raw: Dict[str, Any]) -> Headline:
    return Headline(
        title=str(raw.get("title", "")).strip(),
        url=str(raw.get("url", "")).strip(),
        domain=str(raw.get("domain", "")).strip(),
        seendate=str(raw.get("seendate", "")).strip(),
    )


def dedupe(headlines: List[Headline], *, max_results: int) -> List[Headline]:
    """빈 제목 제거 + 제목 기준 중복 제거(입력 순서=최신순 유지)."""
    out: List[Headline] = []
    seen: set[str] = set()
    for h in headlines:
        key = re.sub(r"\s+", " ", h.title.lower()).strip()
        if not h.title or key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= max_results:
            break
    return out


class SupportsFetchArticles(Protocol):
    def fetch_articles(
        self, query: str, *, max_records: int, timespan: str
    ) -> List[Dict[str, Any]]: ...


class GdeltClient:
    """GDELT DOC API에서 기사 목록(ArtList, JSON)을 시간 역순으로 가져온다."""

    def __init__(
        self,
        base_url: str = GDELT_DOC_BASE,
        timeout: int = 30,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._session = session or requests.Session()

    def fetch_articles(
        self, query: str, *, max_records: int = 20, timespan: str = "1d"
    ) -> List[Dict[str, Any]]:
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": max_records,
            "timespan": timespan,
            "sort": "DateDesc",
        }
        resp = self._session.get(self.base_url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("articles") if isinstance(data, dict) else None
        return articles if isinstance(articles, list) else []


def fetch_headlines(
    keywords: List[str],
    client: SupportsFetchArticles,
    *,
    max_records: int = 10,
    timespan: str = "1d",
) -> List[Headline]:
    """키워드 관련 최신 헤드라인 조회. 실패 시 빈 리스트 (graceful)."""
    query = build_query(keywords)
    if not query:
        return []
    try:
        # GDELT는 결과를 넉넉히 받아 중복 제거 후 잘라낸다.
        raw = client.fetch_articles(
            query, max_records=max_records * 2, timespan=timespan
        )
    except (requests.RequestException, ValueError) as exc:
        logger.warning("GDELT 헤드라인 조회 실패: %s", exc)
        return []
    headlines = [parse_article(a) for a in raw if isinstance(a, dict)]
    return dedupe(headlines, max_results=max_records)
