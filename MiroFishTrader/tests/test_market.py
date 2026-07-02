"""market (Stooq/AlphaVantage/Finnhub 시세 + FRED 매크로) 단위 테스트.

HTTP 없이 FakeClient로 검증 (news.py/polymarket.py 테스트와 동일한 스타일).
"""
from __future__ import annotations

from src.sources.market import (
    MacroIndicator,
    Quote,
    fetch_macro,
    fetch_quotes,
    parse_fred_observations,
    parse_stooq_csv,
)

STOOQ_CSV_OK = "Symbol,Date,Open,High,Low,Close,Volume\nSPY.US,2026-07-01,550.00,555.00,549.00,555.00,80000000\n"
STOOQ_CSV_NO_DATA = "Symbol,Date,Open,High,Low,Close,Volume\nBADSYM.US,2026-07-01,N/D,N/D,N/D,N/D,N/D\n"

FRED_OBS_OK = {
    "observations": [
        {"date": "2026-07-01", "value": "5.33"},
        {"date": "2026-06-01", "value": "5.33"},
    ]
}
FRED_OBS_MISSING_VALUE = {
    "observations": [
        {"date": "2026-07-01", "value": "."},
    ]
}
FRED_OBS_EMPTY = {"observations": []}


class FakeQuoteClient:
    def __init__(self, quote=None, raise_exc=None):
        self._quote = quote
        self._raise = raise_exc

    def fetch_quote(self, symbol):
        if self._raise:
            raise self._raise
        return self._quote


class FakeFredClient:
    def __init__(self, data=None, raise_exc=None):
        self._data = data if data is not None else {}
        self._raise = raise_exc

    def fetch_series(self, series_id):
        if self._raise:
            raise self._raise
        return self._data


# --- Stooq CSV 파싱 -----------------------------------------------------


def test_parse_stooq_csv_ok():
    q = parse_stooq_csv(STOOQ_CSV_OK)
    assert q is not None
    assert q.symbol == "SPY.US"
    assert q.price == 555.00
    assert round(q.day_change_pct, 4) == round((555.00 - 550.00) / 550.00 * 100, 4)
    assert q.asof == "2026-07-01"


def test_parse_stooq_csv_no_data_returns_none():
    assert parse_stooq_csv(STOOQ_CSV_NO_DATA) is None


def test_parse_stooq_csv_empty_text_returns_none():
    assert parse_stooq_csv("") is None


# --- fetch_quotes 폴백 체인 ----------------------------------------------


def test_fetch_quotes_falls_back_to_second_client():
    q2 = Quote(symbol="qqq.us", price=480.0, day_change_pct=1.2, asof="2026-07-01")
    res = fetch_quotes(["qqq.us"], [FakeQuoteClient(None), FakeQuoteClient(q2)])
    assert res == [q2]


def test_fetch_quotes_all_fail_skips_symbol():
    res = fetch_quotes(["soxx.us"], [FakeQuoteClient(None), FakeQuoteClient(None)])
    assert res == []


def test_fetch_quotes_uses_first_client_when_available():
    q1 = Quote(symbol="spy.us", price=555.0, day_change_pct=0.9, asof="2026-07-01")
    q2 = Quote(symbol="spy.us", price=999.0, day_change_pct=9.9, asof="2026-07-01")
    res = fetch_quotes(["spy.us"], [FakeQuoteClient(q1), FakeQuoteClient(q2)])
    assert res == [q1]


def test_fetch_quotes_graceful_on_client_exception():
    q2 = Quote(symbol="spy.us", price=555.0, day_change_pct=0.9, asof="2026-07-01")
    res = fetch_quotes(
        ["spy.us"], [FakeQuoteClient(raise_exc=RuntimeError("boom")), FakeQuoteClient(q2)]
    )
    assert res == [q2]


def test_fetch_quotes_all_raise_skips_symbol_without_crashing():
    res = fetch_quotes(
        ["spy.us"],
        [
            FakeQuoteClient(raise_exc=RuntimeError("boom1")),
            FakeQuoteClient(raise_exc=RuntimeError("boom2")),
        ],
    )
    assert res == []


def test_fetch_quotes_multiple_symbols_independent_fallback():
    fallback_quote = Quote(symbol="fallback", price=1.0, day_change_pct=None, asof="2026-07-01")
    # 1차 클라이언트가 항상 실패해도, 심볼마다 독립적으로 2차 클라이언트로 폴백된다.
    res = fetch_quotes(
        ["spy.us", "qqq.us"],
        [FakeQuoteClient(quote=None), FakeQuoteClient(fallback_quote)],
    )
    assert len(res) == 2
    assert all(q == fallback_quote for q in res)


# --- FRED 파싱 -----------------------------------------------------------


def test_parse_fred_observations_ok():
    ind = parse_fred_observations(FRED_OBS_OK, "DFF", "Fed Funds Rate")
    assert ind == MacroIndicator(
        series_id="DFF", name="Fed Funds Rate", value=5.33, asof="2026-07-01"
    )


def test_parse_fred_observations_missing_value_returns_none():
    assert parse_fred_observations(FRED_OBS_MISSING_VALUE, "DFF", "Fed Funds Rate") is None


def test_parse_fred_observations_empty_returns_none():
    assert parse_fred_observations(FRED_OBS_EMPTY, "DFF", "Fed Funds Rate") is None


def test_parse_fred_observations_missing_key_returns_none():
    assert parse_fred_observations({}, "DFF", "Fed Funds Rate") is None


# --- fetch_macro ----------------------------------------------------------


def test_fetch_macro_happy_path():
    res = fetch_macro([("DFF", "Fed Funds Rate")], FakeFredClient(FRED_OBS_OK))
    assert res == [
        MacroIndicator(series_id="DFF", name="Fed Funds Rate", value=5.33, asof="2026-07-01")
    ]


def test_fetch_macro_graceful_on_client_returning_none():
    res = fetch_macro([("DFF", "Fed Funds Rate")], FakeFredClient(data=None))
    assert res == []


def test_fetch_macro_graceful_on_client_exception():
    res = fetch_macro(
        [("DFF", "Fed Funds Rate")], FakeFredClient(raise_exc=RuntimeError("boom"))
    )
    assert res == []


def test_fetch_macro_skips_failed_series_but_keeps_others():
    class MultiSeriesClient:
        def fetch_series(self, series_id):
            if series_id == "DFF":
                return FRED_OBS_OK
            return FRED_OBS_EMPTY

    res = fetch_macro(
        [("DFF", "Fed Funds Rate"), ("CPIAUCSL", "CPI")], MultiSeriesClient()
    )
    assert len(res) == 1
    assert res[0].series_id == "DFF"
