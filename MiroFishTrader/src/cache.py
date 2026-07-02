"""파일 기반 TTL(유효기간) 캐시.

news/market/social 등 각 소스가 외부 호출 전에 이 캐시를 거쳐 가도록 하는
"freshness layer"의 토대. 표준 라이브러리만 사용(json/time/pathlib/dataclasses).

원칙: 캐시 미스나 손상은 절대 예외로 터뜨리지 않고 미스로 취급해 degrade 한다.
(폴리마켓/뉴스 모듈과 동일하게 "graceful degrade" 우선.)
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _safe_key(key: str) -> str:
    """캐시 키를 안전한 파일명으로 변환 (영숫자/-/_ 이외는 전부 `_`)."""
    return _UNSAFE_CHARS.sub("_", key)


@dataclass
class CacheEntry:
    """캐시 한 건. `value`는 JSON 직렬화 가능한 값이어야 한다."""

    value: Any
    fetched_at: float

    def age_seconds(self, *, now: float) -> float:
        """기준 시각(now) 대비 경과 시간(초)."""
        return now - self.fetched_at


class TTLCache:
    """파일 기반 TTL 캐시.

    각 키는 `<cache_dir>/<safe_key>.json` 파일 하나에 `{value, fetched_at}`
    형태로 저장된다. 파일 읽기/쓰기 실패나 JSON 손상은 예외를 올리지 않고
    미스로 취급한다.
    """

    def __init__(
        self,
        cache_dir: str,
        *,
        ttl_seconds: float = 6 * 3600,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self._now = now

    def _path_for(self, key: str) -> Path:
        return self.cache_dir / f"{_safe_key(key)}.json"

    def get(self, key: str) -> Optional[CacheEntry]:
        """캐시 파일을 읽어 `CacheEntry`로 반환. 없거나 손상되면 None.

        TTL은 확인하지 않는다 (fresh/stale 판단은 호출자가 `is_fresh`로 한다).
        """
        path = self._path_for(key)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("캐시 파일 손상/읽기 실패 (%s): %s", path, exc)
            return None
        if not isinstance(raw, dict) or "value" not in raw or "fetched_at" not in raw:
            logger.warning("캐시 파일 형식이 올바르지 않음: %s", path)
            return None
        try:
            fetched_at = float(raw["fetched_at"])
        except (TypeError, ValueError):
            logger.warning("캐시 파일 fetched_at 파싱 실패: %s", path)
            return None
        return CacheEntry(value=raw["value"], fetched_at=fetched_at)

    def is_fresh(self, entry: CacheEntry) -> bool:
        """엔트리가 TTL 이내인지 여부."""
        return (self._now() - entry.fetched_at) < self.ttl_seconds

    def set(self, key: str, value: Any) -> None:
        """값을 JSON으로 캐시 파일에 기록. 디렉터리는 필요시 생성.

        쓰기 실패는 경고 로그만 남기고 조용히 무시한다 (graceful).
        """
        path = self._path_for(key)
        payload = {"value": value, "fetched_at": self._now()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("캐시 파일 쓰기 실패 (%s): %s", path, exc)

    def get_or_fetch(self, key: str, fetch: Callable[[], Any]) -> tuple[Any, bool]:
        """캐시를 우선 사용하고, 없거나 오래됐으면 `fetch()`로 새로 가져온다.

        반환값은 `(value, stale)`.
        - fresh 캐시가 있으면 fetch를 호출하지 않고 그대로 반환 (stale=False).
        - 없거나 오래됐으면 fetch()를 호출한다.
          - 성공 시: 캐시에 기록 후 (새 값, False) 반환.
          - 실패 시(예외 발생):
            - 오래된 캐시라도 있으면 그 값으로 degrade: (예전 값, True) 반환.
            - 캐시가 아예 없으면 예외를 그대로 올려 호출자가 처리하게 한다.
        """
        entry = self.get(key)
        if entry is not None and self.is_fresh(entry):
            return entry.value, False

        try:
            fresh_value = fetch()
        except Exception as exc:  # noqa: BLE001 - 호출자 fetch의 임의 예외를 다룸
            if entry is not None:
                logger.warning(
                    "캐시 갱신 실패, 오래된 값으로 degrade (key=%s): %s", key, exc
                )
                return entry.value, True
            raise

        self.set(key, fresh_value)
        return fresh_value, False
