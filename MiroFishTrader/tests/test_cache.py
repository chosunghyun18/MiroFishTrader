"""cache(TTLCache) 단위 테스트. 실제 sleep 없이 가짜 시계를 주입해 검증한다."""
from __future__ import annotations

import pytest

from src.cache import TTLCache


class FakeClock:
    """테스트용 가짜 시계. `advance()`로 시간을 흐르게 한다."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_set_then_get_returns_value_and_fetched_at(tmp_path):
    clock = FakeClock()
    cache = TTLCache(str(tmp_path), ttl_seconds=100, now=clock)

    cache.set("k1", {"a": 1})
    entry = cache.get("k1")

    assert entry is not None
    assert entry.value == {"a": 1}
    assert entry.fetched_at == clock.t


def test_is_fresh_within_ttl_true_then_false_after_advance(tmp_path):
    clock = FakeClock()
    cache = TTLCache(str(tmp_path), ttl_seconds=100, now=clock)

    cache.set("k1", "v")
    entry = cache.get("k1")
    assert cache.is_fresh(entry) is True

    clock.advance(101)
    assert cache.is_fresh(entry) is False


def test_get_missing_key_returns_none(tmp_path):
    cache = TTLCache(str(tmp_path), ttl_seconds=100, now=FakeClock())
    assert cache.get("missing") is None


def test_get_corrupted_file_returns_none(tmp_path):
    cache = TTLCache(str(tmp_path), ttl_seconds=100, now=FakeClock())
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert cache.get("bad") is None


def test_get_or_fetch_fresh_cache_does_not_call_fetch(tmp_path):
    clock = FakeClock()
    cache = TTLCache(str(tmp_path), ttl_seconds=100, now=clock)
    cache.set("k1", "cached-value")

    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return "new-value"

    value, stale = cache.get_or_fetch("k1", fetch)

    assert value == "cached-value"
    assert stale is False
    assert calls["n"] == 0


def test_get_or_fetch_stale_cache_fetch_succeeds(tmp_path):
    clock = FakeClock()
    cache = TTLCache(str(tmp_path), ttl_seconds=100, now=clock)
    cache.set("k1", "old-value")
    clock.advance(101)

    value, stale = cache.get_or_fetch("k1", lambda: "fresh-value")

    assert value == "fresh-value"
    assert stale is False

    # 캐시가 실제로 갱신됐는지 확인
    entry = cache.get("k1")
    assert entry.value == "fresh-value"
    assert entry.fetched_at == clock.t


def test_get_or_fetch_stale_cache_fetch_raises_returns_stale(tmp_path):
    clock = FakeClock()
    cache = TTLCache(str(tmp_path), ttl_seconds=100, now=clock)
    cache.set("k1", "old-value")
    clock.advance(101)

    def fetch():
        raise RuntimeError("network down")

    value, stale = cache.get_or_fetch("k1", fetch)

    assert value == "old-value"
    assert stale is True


def test_get_or_fetch_no_cache_fetch_raises_propagates(tmp_path):
    cache = TTLCache(str(tmp_path), ttl_seconds=100, now=FakeClock())

    def fetch():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        cache.get_or_fetch("missing", fetch)


def test_key_sanitization_round_trips(tmp_path):
    clock = FakeClock()
    cache = TTLCache(str(tmp_path), ttl_seconds=100, now=clock)
    key = "polymarket:top/2026"

    cache.set(key, {"ok": True})
    entry = cache.get(key)

    assert entry is not None
    assert entry.value == {"ok": True}

    # 파일이 하나만 생겼는지 (안전한 단일 파일로 매핑)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
