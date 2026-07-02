"""Reddit / Google Trends에서 군중 관심(crowd-attention) 신호 조회.

무료 티어만 사용한다(Reddit API 무료 앱 등록, Google Trends 비공식 pytrends).
praw/pytrends는 선택 의존성이라 모듈 상단에서 import하지 않고, 각 클라이언트
메서드 안에서 지연 import 한다(try/except ImportError → 사용 불가 처리,
경고 로그 후 빈 결과). 두 라이브러리가 설치돼 있지 않아도 이 모듈은 임포트되고
테스트가 통과해야 한다.

시드 생성기에 주입할 crowd-attention 신호와 interested_topics 도출에 쓴다.
(뉴스/폴리마켓 모듈과 동일한 DI 패턴: Protocol 클라이언트 주입 → 테스트 시
네트워크/외부 라이브러리 없이 검증.)
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Protocol

logger = logging.getLogger(__name__)

DEFAULT_SUBREDDITS = ["stocks", "wallstreetbets", "investing"]

# 흔한 불용어(토큰 빈도 집계 시 제외). 완벽할 필요는 없음 — 잡음만 줄이면 충분.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of",
    "in", "on", "for", "and", "or", "but", "with", "at", "by", "from", "this",
    "that", "it", "as", "will", "what", "why", "how", "you", "your", "my",
    "i", "we", "they", "if", "not", "no", "do", "does", "did", "just", "so",
}


@dataclass
class RedditPost:
    title: str
    subreddit: str
    score: int
    url: str


@dataclass
class TrendTopic:
    keyword: str
    rank: int  # 1이 가장 인기(Google Trends 응답 순서 기준)


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.lower()).strip()


def dedupe_posts(posts: List[RedditPost], *, max_results: int) -> List[RedditPost]:
    """빈 제목 제거 + 제목 기준 중복 제거(입력 순서 유지). news.dedupe와 동일 아이디어."""
    out: List[RedditPost] = []
    seen: set[str] = set()
    for p in posts:
        key = _normalize_title(p.title)
        if not p.title or key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= max_results:
            break
    return out


class SupportsFetchRedditHot(Protocol):
    def fetch_hot(self, subreddits: List[str], *, limit: int) -> List[RedditPost]: ...


class PrawRedditClient:
    """PRAW(Python Reddit API Wrapper)로 지정 서브레딧들의 인기 글을 읽기 전용으로 조회.

    praw는 선택 의존성: import 실패 시(미설치) 또는 자격증명 누락 시 빈 결과.
    """

    def __init__(self, client_id: str, client_secret: str, user_agent: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent

    def fetch_hot(self, subreddits: List[str], *, limit: int = 15) -> List[RedditPost]:
        if not subreddits:
            return []
        if not self.client_id or not self.client_secret:
            logger.warning("Reddit 자격증명 누락 - 조회 건너뜀")
            return []
        try:
            import praw  # 지연 import: 선택 의존성
        except ImportError as exc:
            logger.warning("praw 미설치 - Reddit 조회 건너뜀: %s", exc)
            return []
        try:
            reddit = praw.Reddit(
                client_id=self.client_id,
                client_secret=self.client_secret,
                user_agent=self.user_agent,
            )
            reddit.read_only = True
            combined = "+".join(subreddits)
            posts: List[RedditPost] = []
            for submission in reddit.subreddit(combined).hot(limit=limit):
                posts.append(
                    RedditPost(
                        title=str(getattr(submission, "title", "")).strip(),
                        subreddit=str(getattr(submission, "subreddit", "")).strip(),
                        score=int(getattr(submission, "score", 0) or 0),
                        url=str(getattr(submission, "url", "")).strip(),
                    )
                )
            return posts
        except Exception as exc:  # noqa: BLE001 - 어떤 이유든 실패 시 graceful degrade
            logger.warning("Reddit 인기글 조회 실패: %s", exc)
            return []


def fetch_reddit_hot(
    subreddits: List[str],
    client: SupportsFetchRedditHot,
    *,
    limit: int = 15,
    max_results: int = 15,
) -> List[RedditPost]:
    """서브레딧들의 인기 글 조회. 실패 시 빈 리스트 (graceful)."""
    if not subreddits:
        return []
    try:
        raw = client.fetch_hot(subreddits, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 클라이언트 예외 종류를 가리지 않고 degrade
        logger.warning("Reddit 인기글 조회 실패: %s", exc)
        return []
    return dedupe_posts(raw, max_results=max_results)


class SupportsFetchTrends(Protocol):
    def fetch_trending(self, *, geo: str) -> List[str]: ...


class PytrendsClient:
    """pytrends(비공식 Google Trends API)로 일간 급상승 검색어 조회.

    pytrends는 선택 의존성: import 실패 시(미설치) 빈 결과.
    """

    def fetch_trending(self, *, geo: str = "united_states") -> List[str]:
        try:
            from pytrends.request import TrendReq  # 지연 import: 선택 의존성
        except ImportError as exc:
            logger.warning("pytrends 미설치 - Trends 조회 건너뜀: %s", exc)
            return []
        try:
            pytrends = TrendReq()
            df = pytrends.trending_searches(pn=geo)
            return [str(v).strip() for v in df[0].tolist()]
        except Exception as exc:  # noqa: BLE001 - 어떤 이유든 실패 시 graceful degrade
            logger.warning("Google Trends 조회 실패: %s", exc)
            return []


def fetch_trends(
    client: SupportsFetchTrends,
    *,
    geo: str = "united_states",
    max_results: int = 15,
) -> List[TrendTopic]:
    """일간 급상승 검색어 조회. 실패 시 빈 리스트 (graceful)."""
    try:
        keywords = client.fetch_trending(geo=geo)
    except Exception as exc:  # noqa: BLE001 - 클라이언트 예외 종류를 가리지 않고 degrade
        logger.warning("Google Trends 조회 실패: %s", exc)
        return []
    out: List[TrendTopic] = []
    seen: set[str] = set()
    for kw in keywords:
        kw = str(kw).strip()
        key = kw.lower()
        if not kw or key in seen:
            continue
        seen.add(key)
        out.append(TrendTopic(keyword=kw, rank=len(out) + 1))
        if len(out) >= max_results:
            break
    return out


def _tokenize(title: str) -> List[str]:
    return [t for t in re.findall(r"[A-Za-z']+", title.lower()) if len(t) > 2]


def derive_interested_topics(
    reddit_posts: List[RedditPost],
    trends: List[TrendTopic],
    watchlist: List[str],
    *,
    max_topics: int = 8,
) -> List[str]:
    """워치리스트 + 트렌드 + 레딧 제목 빈출 토큰을 합쳐 관심 토픽 목록 생성.

    우선순위: 워치리스트(항상 유지) > 트렌드 키워드(순위순) > 레딧 빈출 토큰(빈도순).
    대소문자 무시 중복 제거, max_topics로 상한.
    순수 함수(네트워크 없음) - 단위 테스트하기 쉽다.
    """
    out: List[str] = []
    seen: set[str] = set()

    def _add(item: str) -> None:
        item = item.strip()
        key = item.lower()
        if not item or key in seen:
            return
        seen.add(key)
        out.append(item)

    for item in watchlist:
        _add(item)

    for trend in sorted(trends, key=lambda t: t.rank):
        if len(out) >= max_topics:
            return out[:max_topics]
        _add(trend.keyword)

    token_counts: Counter[str] = Counter()
    for post in reddit_posts:
        for token in _tokenize(post.title):
            if token in _STOPWORDS:
                continue
            token_counts[token] += 1

    for token, _count in token_counts.most_common():
        if len(out) >= max_topics:
            break
        _add(token)

    return out[:max_topics]
