"""seed 단위 테스트 (HTTP 없이 FakeClient)."""
from __future__ import annotations

from src.seed import (
    DEFAULT_WATCHLIST,
    build_seed_markdown,
    build_simulation_requirement,
    generate_seed,
)
from src.polymarket import PolymarketMarket


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
