"""price_history (Yahoo 일봉 → 주·월봉 재집계 → SQLite) 단위 테스트. HTTP 없이 가짜 응답으로 검증."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.sources.price_history import _sql_literal, load, mysql_sync_script, parse_chart, resample

NY = ZoneInfo("America/New_York")
DAYS = ["2024-01-29", "2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02", "2024-02-05"]


def ts(d: str) -> int:
    return int(datetime.fromisoformat(d + "T09:30").replace(tzinfo=NY).timestamp())


def payload(null_at=None):
    n = len(DAYS)
    close = [float(10 + i) for i in range(n)]
    opens = [c - 0.5 for c in close]
    if null_at is not None:
        close[null_at] = None
    return {"chart": {"error": None, "result": [{
        "meta": {"exchangeTimezoneName": "America/New_York"},
        "timestamp": [ts(d) for d in DAYS],
        "indicators": {
            "quote": [{"open": opens, "high": [c + 1 if c else None for c in close],
                       "low": [o - 1 for o in opens], "close": close,
                       "volume": [100] * n}],
            "adjclose": [{"adjclose": [c * 0.9 if c else None for c in close]}],
        },
        "events": {
            "splits": {"x": {"date": ts("2024-02-01"), "numerator": 2.0, "denominator": 1.0}},
            "dividends": {"y": {"date": ts("2024-01-31"), "amount": 0.25}},
        },
    }]}}


def test_parse_dates_split_factor_and_events():
    bars, events = parse_chart("QQQ", payload())
    assert [b.date for b in bars] == DAYS
    # 분할일(02-01) 이전 거래일만 누적 배수 2
    assert [b.split_factor for b in bars] == [2.0, 2.0, 2.0, 1.0, 1.0, 1.0]
    assert [(e.date, e.kind, e.value) for e in events] == [
        ("2024-01-31", "dividend", 0.25), ("2024-02-01", "split", 2.0)]


def test_parse_skips_null_rows_and_raises_on_error():
    bars, _ = parse_chart("QQQ", payload(null_at=2))
    assert "2024-01-31" not in [b.date for b in bars] and len(bars) == 5
    with pytest.raises(ValueError):
        parse_chart("QQQ", {"chart": {"error": {"code": "Not Found"}, "result": None}})


def test_resample_weekly_and_monthly():
    bars, _ = parse_chart("QQQ", payload())
    weekly = resample(bars, "weekly")
    assert [(w["period_start"], w["date"], w["trading_days"]) for w in weekly] == [
        ("2024-01-29", "2024-02-02", 5), ("2024-02-05", "2024-02-05", 1)]
    w = weekly[0]
    assert (w["open"], w["close"], w["high"], w["low"], w["volume"]) == (9.5, 14.0, 15.0, 8.5, 500)
    monthly = resample(bars, "monthly")
    assert [(m["period_start"], m["date"]) for m in monthly] == [
        ("2024-01-29", "2024-01-31"), ("2024-02-01", "2024-02-05")]


def test_load_is_idempotent(tmp_path):
    db = tmp_path / "p.db"
    fetch = lambda sym, s, e: payload()
    for _ in range(2):
        report = load(["QQQ", "TQQQ"], db, fetch=fetch)
    assert report["QQQ"] == {"daily": 6, "weekly": 2, "monthly": 2,
                             "first": "2024-01-29", "last": "2024-02-05"}
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0] == 12
    assert conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM load_log").fetchone()[0] == 4


def test_mysql_sync_script_replaces_all_tables_in_one_transaction(tmp_path):
    db = tmp_path / "p.db"
    load(["QQQ"], db, fetch=lambda sym, s, e: payload())
    sql = mysql_sync_script(db, batch=4)
    assert sql.index("START TRANSACTION;") < sql.index("DELETE FROM prices_daily;") < sql.rindex("COMMIT;")
    assert sql.count("INSERT INTO prices_daily ") == 2  # 6행을 4행씩 배치
    assert "('QQQ','2024-01-29',9.5,11.0,8.5,10.0,9.0,100,2.0)" in sql


def test_sql_literal_escapes():
    assert _sql_literal(None) == "NULL"
    assert _sql_literal("O'Neil\\x") == "'O''Neil\\\\x'"
    assert _sql_literal(3) == "3" and _sql_literal(0.1) == "0.1"
