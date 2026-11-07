"""ETF 장기 가격 이력(일·주·월봉) 다운로드 → 로컬 SQLite 적재.

Yahoo Finance chart API(키 불필요)에서 일봉을 받고, 주봉·월봉은 일봉에서 재집계한다.
신규 필수 의존성 없음(requests + 표준 sqlite3). market.py 와 같은 DI 패턴:
fetch 함수를 주입하면 테스트에서 HTTP 없이 돈다.

가격 규약 (Yahoo 그대로):
- open/high/low/close/volume: 분할 반영(split-adjusted) 값
- adj_close: 분할 + 배당 반영 값
- split_factor: 이후 분할 누적 배수. 당시 원가격 = 가격 × split_factor
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
DEFAULT_SYMBOLS: List[str] = ["QQQ", "QLD", "TQQQ"]
DEFAULT_START = date(2000, 1, 1)
DEFAULT_DB = Path(__file__).resolve().parents[2] / "shared" / "prices.db"

FetchFn = Callable[[str, date, date], Dict[str, Any]]


@dataclass
class Bar:
    symbol: str
    date: str  # 거래소 현지 날짜 YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int
    split_factor: float = 1.0


@dataclass
class Event:
    symbol: str
    date: str
    kind: str  # "split" | "dividend"
    value: float  # split: 배수(2:1 → 2.0), dividend: 주당 금액


def fetch_yahoo(symbol: str, start: date, end: date, timeout: float = 30.0) -> Dict[str, Any]:
    p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    resp = requests.get(
        YAHOO_CHART.format(symbol=symbol),
        params={"period1": p1, "period2": p2, "interval": "1d",
                "events": "div,split", "includeAdjustedClose": "true"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def parse_chart(symbol: str, payload: Dict[str, Any]) -> Tuple[List[Bar], List[Event]]:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ValueError(f"{symbol}: Yahoo error {chart['error']}")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise ValueError(f"{symbol}: 빈 응답")
    tz = ZoneInfo(result.get("meta", {}).get("exchangeTimezoneName") or "America/New_York")

    def to_date(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz).date().isoformat()

    q = result["indicators"]["quote"][0]
    adj = result["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    bars: Dict[str, Bar] = {}
    for i, ts in enumerate(result.get("timestamp") or []):
        vals = (q["open"][i], q["high"][i], q["low"][i], q["close"][i], adj[i])
        if any(v is None for v in vals):
            continue  # Yahoo 가 휴장·결측 행을 null 로 주는 경우
        d = to_date(ts)
        bars[d] = Bar(symbol, d, *(float(v) for v in vals), int(q["volume"][i] or 0))

    events: List[Event] = []
    ev = result.get("events") or {}
    for s in (ev.get("splits") or {}).values():
        events.append(Event(symbol, to_date(s["date"]), "split", s["numerator"] / s["denominator"]))
    for dv in (ev.get("dividends") or {}).values():
        events.append(Event(symbol, to_date(dv["date"]), "dividend", float(dv["amount"])))
    events.sort(key=lambda e: (e.date, e.kind))

    ordered = [bars[d] for d in sorted(bars)]
    splits = [e for e in events if e.kind == "split"]
    for b in ordered:
        f = 1.0
        for s in splits:
            if s.date > b.date:
                f *= s.value
        b.split_factor = f
    return ordered, events


def resample(bars: Iterable[Bar], freq: str) -> List[Dict[str, Any]]:
    """일봉 → 주봉(ISO 주, 월~금) 또는 월봉. 기간 라벨은 그 기간의 마지막 거래일."""
    if freq not in ("weekly", "monthly"):
        raise ValueError(freq)
    groups: Dict[Tuple[str, Tuple[int, int]], List[Bar]] = {}
    for b in bars:
        d = date.fromisoformat(b.date)
        key = d.isocalendar()[:2] if freq == "weekly" else (d.year, d.month)
        groups.setdefault((b.symbol, key), []).append(b)
    out = []
    for (symbol, _), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[1][0].date)):
        g.sort(key=lambda b: b.date)
        last = g[-1]
        out.append({
            "symbol": symbol, "date": last.date, "period_start": g[0].date,
            "open": g[0].open, "high": max(b.high for b in g), "low": min(b.low for b in g),
            "close": last.close, "adj_close": last.adj_close,
            "volume": sum(b.volume for b in g), "split_factor": last.split_factor,
            "trading_days": len(g),
        })
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    symbol TEXT NOT NULL, date TEXT NOT NULL, {extra}
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
    close REAL NOT NULL, adj_close REAL NOT NULL, volume INTEGER NOT NULL,
    split_factor REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (symbol, date)
);
"""
PERIOD_EXTRA = "period_start TEXT NOT NULL, trading_days INTEGER NOT NULL,"


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        SCHEMA.format(t="prices_daily", extra="")
        + SCHEMA.format(t="prices_weekly", extra=PERIOD_EXTRA)
        + SCHEMA.format(t="prices_monthly", extra=PERIOD_EXTRA)
        + """
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY (symbol, date, kind)
);
CREATE TABLE IF NOT EXISTS load_log (
    symbol TEXT NOT NULL, loaded_at TEXT NOT NULL, source TEXT NOT NULL,
    first_date TEXT, last_date TEXT, daily_rows INTEGER
);
"""
    )


def store(conn: sqlite3.Connection, symbol: str, bars: List[Bar], events: List[Event],
          source: str = "yahoo") -> Dict[str, int]:
    """종목 단위로 교체 저장 (재실행해도 결과 동일)."""
    cols = "symbol,date,open,high,low,close,adj_close,volume,split_factor"
    pcols = cols + ",period_start,trading_days"
    with conn:
        for t in ("prices_daily", "prices_weekly", "prices_monthly", "corporate_actions"):
            conn.execute(f"DELETE FROM {t} WHERE symbol = ?", (symbol,))
        conn.executemany(
            f"INSERT INTO prices_daily ({cols}) VALUES (?,?,?,?,?,?,?,?,?)",
            [(b.symbol, b.date, b.open, b.high, b.low, b.close, b.adj_close, b.volume,
              b.split_factor) for b in bars])
        counts = {"daily": len(bars)}
        for freq in ("weekly", "monthly"):
            rows = resample(bars, freq)
            conn.executemany(
                f"INSERT INTO prices_{freq} ({pcols}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [tuple(r[c] for c in pcols.split(",")) for r in rows])
            counts[freq] = len(rows)
        conn.executemany(
            "INSERT INTO corporate_actions VALUES (?,?,?,?)",
            [(e.symbol, e.date, e.kind, e.value) for e in events])
        conn.execute(
            "INSERT INTO load_log VALUES (?,?,?,?,?,?)",
            (symbol, datetime.now(timezone.utc).isoformat(timespec="seconds"), source,
             bars[0].date if bars else None, bars[-1].date if bars else None, len(bars)))
    return counts


def load(symbols: Iterable[str], db_path: Path, start: date = DEFAULT_START,
         end: Optional[date] = None, fetch: FetchFn = fetch_yahoo) -> Dict[str, Dict[str, int]]:
    end = end or date.today()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        report = {}
        for sym in symbols:
            bars, events = parse_chart(sym, fetch(sym, start, end))
            if not bars:
                raise ValueError(f"{sym}: 일봉 0행")
            report[sym] = store(conn, sym, bars, events)
            report[sym]["first"], report[sym]["last"] = bars[0].date, bars[-1].date
            logger.info("%s %s", sym, report[sym])
        return report
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ETF 일·주·월봉 → SQLite")
    ap.add_argument("symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = load(args.symbols, args.db, args.start)
    for sym, r in report.items():
        print(f"{sym}: {r['first']} ~ {r['last']}  일 {r['daily']} / 주 {r['weekly']} / 월 {r['monthly']}")
    print(f"DB: {args.db} ({args.db.stat().st_size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
