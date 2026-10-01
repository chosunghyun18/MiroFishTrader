"""정규화 체결 `trades` → 1분봉 `bars_1m` 리샘플.

입력: 한 심볼의 정규화 체결(`src.shared.schema.TRADES`, validate_trades 통과).
출력: `src.shared.schema.BARS_1M` 을 따르는 DataFrame(컬럼·dtype 은 schema 모듈에서만 가져온다).
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase1-ingest-schema.md` "1분봉 스키마"

    from src.ingest.bars import resample_1m, last_close
    bars = resample_1m(trades, symbol="XBTUSD")                      # 일괄
    day1 = resample_1m(t1, symbol="XBTUSD", day="2020-03-12")         # 일별
    day2 = resample_1m(t2, symbol="XBTUSD", day="2020-03-13", prev_close=last_close(day1))

- 구간은 UTC 기준 왼쪽 닫힘 `[ts, ts+1m)`, 라벨은 분 시작 시각.
- 빈 분은 직전 close 로 OHLC 를 채우고 수량 열(volume·volume_xbt·trade_count·buy/sell_volume)은 0.
- 시작: `prev_close` 가 없으면 첫 체결 분부터, 있으면 첫 체결(또는 `day`)의 UTC 00:00 부터.
- 끝: 마지막 체결(또는 `day`)이 속한 UTC 일의 23:59 분까지. 그래서 일별 결과를 이어 붙이면 일괄 결과와 같다.
- 파일 I/O·저장·원본 일 파일 연속성 판정은 하지 않는다(T-20261002-05 범위).
"""

from __future__ import annotations

import math

import pandas as pd

from src.shared.schema import BARS_1M, STRING, UTC_NS, empty_frame, validate_bars_1m, validate_trades

ONE_DAY = pd.Timedelta(days=1)
ONE_MIN = pd.Timedelta(minutes=1)
_QTY_COLUMNS = ("volume", "volume_xbt", "trade_count", "buy_volume", "sell_volume")


def _day_start(day) -> pd.Timestamp:
    """`day`(datetime.date / "YYYY-MM-DD" 문자열 / pd.Timestamp)를 UTC 자정 Timestamp 로 정규화."""
    ts = pd.Timestamp(day)
    ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    if ts != ts.floor("D"):
        raise ValueError(f"day 는 UTC 자정이어야 함: {day!r}")
    return ts.as_unit("ns")


def resample_1m(trades: pd.DataFrame, *, symbol: str, prev_close: float | None = None,
                day=None) -> pd.DataFrame:
    """한 심볼의 정규화 체결을 1분봉으로 리샘플한다.

    prev_close: 직전 일의 마지막 close. 주면 시작 일 00:00 부터 첫 체결 전 분을 이 값으로 채운다.
    day: UTC 일(datetime.date, "YYYY-MM-DD", pd.Timestamp 자정). 주면 범위를 그날 00:00~23:59 로
        고정하고, 그날 밖 체결이 있으면 ValueError. 체결 0건이고 prev_close 가 있으면 1440분 평탄 봉.
    """
    validate_trades(trades)
    if prev_close is not None and not math.isfinite(prev_close):
        raise ValueError(f"prev_close 가 유한한 수가 아님: {prev_close!r}")
    start_of_day = _day_start(day) if day is not None else None

    if len(trades):
        symbols = trades["symbol"].unique()
        if len(symbols) != 1:
            raise ValueError(f"한 심볼만 받는다: {sorted(map(str, symbols))}")
        if symbols[0] != symbol:
            raise ValueError(f"symbol 불일치: 입력 {symbols[0]!r}, 인자 {symbol!r}")

    t = trades.sort_values("ts", kind="stable")

    if len(t) == 0:
        if prev_close is None:
            return empty_frame(BARS_1M)
        if start_of_day is None:
            raise ValueError("체결 0건에 prev_close 만 있으면 범위를 정할 수 없음: day 필요")
        start, end = start_of_day, start_of_day + ONE_DAY - ONE_MIN
    else:
        first, last = t["ts"].iloc[0], t["ts"].iloc[-1]
        if start_of_day is not None and (first < start_of_day or last >= start_of_day + ONE_DAY):
            raise ValueError(f"day={start_of_day.date()} 밖 체결 포함: {first} ~ {last}")
        start = first.floor("D") if prev_close is not None else first.floor("1min")
        end = last.floor("D") + ONE_DAY - ONE_MIN

    minute = t["ts"].dt.floor("1min")
    g = t.groupby(minute, sort=True)
    price = g["price"]
    size = t["size"]
    agg = pd.DataFrame({
        "open": price.first(),
        "high": price.max(),
        "low": price.min(),
        "close": price.last(),
        "volume": g["size"].sum(),
        "volume_xbt": g["home_notional"].sum(),
        "trade_count": g.size(),
        "buy_volume": size.where(t["side"] == "buy", 0).groupby(minute, sort=True).sum(),
        "sell_volume": size.where(t["side"] == "sell", 0).groupby(minute, sort=True).sum(),
    })

    index = pd.date_range(start, end, freq="1min").astype(UTC_NS)
    agg = agg.reindex(index)

    for c in _QTY_COLUMNS:
        agg[c] = agg[c].fillna(0)
    close = agg["close"].ffill()
    if prev_close is not None:
        close = close.fillna(float(prev_close))
    agg["close"] = close
    empty = agg["trade_count"] == 0
    for c in ("open", "high", "low"):
        agg[c] = agg[c].where(~empty, close)

    out = agg.rename_axis("ts").reset_index()
    out.insert(1, "symbol", pd.array([symbol] * len(out), dtype=STRING))
    out = out[BARS_1M.column_names].astype(BARS_1M.dtypes)
    return validate_bars_1m(out)


def last_close(bars: pd.DataFrame) -> float | None:
    """일별 연쇄용: 봉 프레임의 마지막 close(0행이면 None)."""
    if len(bars) == 0:
        return None
    return float(bars["close"].iloc[-1])

