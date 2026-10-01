"""ingest.bars — 정규화 체결 → 1분봉 리샘플을 손 계산 기대값과 비교."""

import datetime

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src.ingest.bars import last_close, resample_1m
from src.shared.schema import BARS_1M, TRADES, SchemaError, empty_frame, validate_bars_1m

D0 = "2020-03-12"


def _trades(rows, symbol="XBTUSD"):
    """rows: (ts 문자열, side, size, price) → 정규화 trades 프레임. home_notional = size/price."""
    recs = []
    for i, (ts, side, size, price) in enumerate(rows):
        recs.append({
            "ts": pd.Timestamp(ts) if isinstance(ts, pd.Timestamp) else pd.Timestamp(ts, tz="UTC"),
            "symbol": symbol,
            "side": side,
            "size": size,
            "price": float(price),
            "tick_direction": "PlusTick",
            "trd_match_id": f"m-{i}",
            "gross_value": size * 1000,
            "home_notional": size / price,
            "foreign_notional": float(size),
            "trd_type": None,
            "pool": None,
        })
    if not recs:
        return empty_frame(TRADES)
    df = pd.DataFrame(recs, columns=TRADES.column_names)
    df["ts"] = df["ts"].astype("datetime64[ns, UTC]")
    return df.astype(TRADES.dtypes)


def _bars(rows, symbol="XBTUSD"):
    """rows: (ts, o, h, l, c, volume, volume_xbt, trade_count, buy, sell) → 기대 bars_1m."""
    cols = ["ts", "open", "high", "low", "close", "volume", "volume_xbt",
            "trade_count", "buy_volume", "sell_volume"]
    df = pd.DataFrame(rows, columns=cols)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).astype("datetime64[ns, UTC]")
    df.insert(1, "symbol", symbol)
    return validate_bars_1m(df[BARS_1M.column_names].astype(BARS_1M.dtypes))


def _flat(start, n, price, symbol="XBTUSD"):
    ts = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return _bars([(t, price, price, price, price, 0, 0.0, 0, 0, 0) for t in ts], symbol)


def test_basic_aggregation():
    t = _trades([
        (f"{D0} 00:00:10", "buy", 100, 100),
        (f"{D0} 00:00:20", "sell", 50, 105),
        (f"{D0} 00:00:30", "buy", 30, 95),
        (f"{D0} 00:01:05", "sell", 20, 101),
    ])
    out = resample_1m(t, symbol="XBTUSD")
    head = out.iloc[:2].reset_index(drop=True)
    expected = _bars([
        (f"{D0} 00:00", 100.0, 105.0, 95.0, 95.0, 180, 100 / 100 + 50 / 105 + 30 / 95, 3, 130, 50),
        (f"{D0} 00:01", 101.0, 101.0, 101.0, 101.0, 20, 20 / 101, 1, 0, 20),
    ])
    pdt.assert_frame_equal(head, expected)
    assert (out["buy_volume"] + out["sell_volume"] == out["volume"]).all()


def test_minute_boundary_left_closed_left_label():
    t = _trades([
        (f"{D0} 00:00:59.999999999", "buy", 1, 10),
        (f"{D0} 00:01:00.000000000", "sell", 2, 20),
    ])
    out = resample_1m(t, symbol="XBTUSD")
    assert out.loc[0, "ts"] == pd.Timestamp(f"{D0} 00:00", tz="UTC")
    assert (out.loc[0, "close"], out.loc[0, "volume"]) == (10.0, 1)
    assert out.loc[1, "ts"] == pd.Timestamp(f"{D0} 00:01", tz="UTC")
    assert (out.loc[1, "open"], out.loc[1, "volume"]) == (20.0, 2)


def test_same_nanosecond_keeps_input_order():
    ts = f"{D0} 00:00:05.000000001"
    t = _trades([(ts, "buy", 1, 50), (ts, "sell", 1, 70), (ts, "buy", 1, 60)])
    out = resample_1m(t, symbol="XBTUSD")
    assert (out.loc[0, "open"], out.loc[0, "high"], out.loc[0, "low"], out.loc[0, "close"]) == (50, 70, 50, 60)


def test_empty_minutes_filled_with_prev_close():
    t = _trades([
        (f"{D0} 00:00:01", "buy", 10, 100),
        (f"{D0} 00:00:02", "sell", 5, 102),
        (f"{D0} 00:03:00", "buy", 7, 110),
    ])
    out = resample_1m(t, symbol="XBTUSD").iloc[:4].reset_index(drop=True)
    expected = _bars([
        (f"{D0} 00:00", 100.0, 102.0, 100.0, 102.0, 15, 10 / 100 + 5 / 102, 2, 10, 5),
        (f"{D0} 00:01", 102.0, 102.0, 102.0, 102.0, 0, 0.0, 0, 0, 0),
        (f"{D0} 00:02", 102.0, 102.0, 102.0, 102.0, 0, 0.0, 0, 0, 0),
        (f"{D0} 00:03", 110.0, 110.0, 110.0, 110.0, 7, 7 / 110, 1, 7, 0),
    ])
    pdt.assert_frame_equal(out, expected)


def test_start_boundary_without_and_with_prev_close():
    t = _trades([(f"{D0} 10:30:15", "buy", 3, 200)])

    out = resample_1m(t, symbol="XBTUSD")
    assert out["ts"].iloc[0] == pd.Timestamp(f"{D0} 10:30", tz="UTC")
    assert len(out) == 24 * 60 - (10 * 60 + 30)

    out = resample_1m(t, symbol="XBTUSD", prev_close=190.0)
    assert len(out) == 1440
    assert out["ts"].iloc[0] == pd.Timestamp(f"{D0} 00:00", tz="UTC")
    pdt.assert_frame_equal(out.iloc[:630].reset_index(drop=True), _flat(f"{D0} 00:00", 630, 190.0))
    assert out.loc[630, "open"] == 200.0 and out.loc[630, "trade_count"] == 1
    # 체결 이후 분은 체결가로 평탄
    assert (out.iloc[631:]["close"] == 200.0).all()


def test_end_boundary_is_2359_of_last_trade_day():
    t = _trades([
        (f"{D0} 23:58:00", "buy", 1, 10),
        ("2020-03-13 00:00:30", "sell", 1, 11),
    ])
    out = resample_1m(t, symbol="XBTUSD")
    assert out["ts"].iloc[-1] == pd.Timestamp("2020-03-13 23:59", tz="UTC")
    assert len(out) == 2 + 1440
    assert out.loc[1, "ts"] == pd.Timestamp(f"{D0} 23:59", tz="UTC") and out.loc[1, "close"] == 10.0


def _three_days():
    rng = np.random.default_rng(7)
    rows = []
    days = ["2020-03-12", "2020-03-13", "2020-03-14"]
    for d in days:
        base = pd.Timestamp(d, tz="UTC")
        offsets = np.sort(rng.integers(0, 86_400 * 10**9, size=400))
        if d == "2020-03-13":  # 둘째 날 06:00~18:00 장시간 무체결
            offsets = offsets[(offsets < 6 * 3600 * 10**9) | (offsets >= 18 * 3600 * 10**9)]
        for off in offsets:
            side = "buy" if rng.random() < 0.5 else "sell"
            rows.append((base + pd.Timedelta(int(off), "ns"), side, int(rng.integers(1, 5000)),
                         float(rng.integers(7000, 8000)) + 0.5))
    # 첫 체결이 자정이 아니도록 첫날 앞부분 제거
    rows = [r for r in rows if r[0] >= pd.Timestamp("2020-03-12 03:00", tz="UTC")]
    return days, rows


def test_daily_concat_equals_batch():
    days, rows = _three_days()
    t = _trades(rows)
    batch = resample_1m(t, symbol="XBTUSD")

    parts, prev = [], None
    for d in days:
        lo = pd.Timestamp(d, tz="UTC")
        sub = t[(t["ts"] >= lo) & (t["ts"] < lo + pd.Timedelta(days=1))].reset_index(drop=True)
        bars = resample_1m(sub, symbol="XBTUSD", day=d, prev_close=prev)
        parts.append(bars)
        prev = last_close(bars)
    daily = validate_bars_1m(pd.concat(parts, ignore_index=True))

    pdt.assert_frame_equal(daily, batch, check_exact=True)
    assert batch["ts"].diff().dropna().eq(pd.Timedelta(minutes=1)).all()
    assert not batch["ts"].duplicated().any()
    assert batch["ts"].iloc[-1] == pd.Timestamp("2020-03-14 23:59", tz="UTC")
    gap = batch[(batch["ts"] >= "2020-03-13 06:00") & (batch["ts"] < "2020-03-13 18:00")]
    assert len(gap) == 720 and (gap["trade_count"] == 0).all()
    assert int(batch["trade_count"].sum()) == len(t)
    assert int(batch["volume"].sum()) == int(t["size"].sum())


def test_empty_input():
    empty = _trades([])
    out = resample_1m(empty, symbol="XBTUSD", prev_close=123.5, day=datetime.date(2020, 3, 12))
    pdt.assert_frame_equal(out, _flat(f"{D0} 00:00", 1440, 123.5))

    out = resample_1m(empty, symbol="XBTUSD")
    assert len(out) == 0
    pdt.assert_frame_equal(out, empty_frame(BARS_1M))
    validate_bars_1m(out)
    assert last_close(out) is None


def test_errors():
    t = _trades([(f"{D0} 00:00:01", "buy", 1, 10)])
    mixed = pd.concat([t, _trades([(f"{D0} 00:00:02", "buy", 1, 10)], symbol="ETHUSD")
                       .assign(trd_match_id=lambda d: d["trd_match_id"] + "-e")],
                      ignore_index=True)
    with pytest.raises(ValueError, match="한 심볼"):
        resample_1m(mixed, symbol="XBTUSD")
    with pytest.raises(ValueError, match="symbol 불일치"):
        resample_1m(t, symbol="ETHUSD")
    with pytest.raises(ValueError, match="밖 체결"):
        resample_1m(t, symbol="XBTUSD", day="2020-03-13")
    with pytest.raises(ValueError, match="day 필요"):
        resample_1m(_trades([]), symbol="XBTUSD", prev_close=1.0)
    with pytest.raises(ValueError, match="자정"):
        resample_1m(t, symbol="XBTUSD", day=pd.Timestamp(f"{D0} 12:00", tz="UTC"))
    naive = t.assign(ts=t["ts"].dt.tz_localize(None))
    with pytest.raises(SchemaError):
        resample_1m(naive, symbol="XBTUSD")


def test_day_argument_forms_equivalent():
    t = _trades([(f"{D0} 05:00:00", "buy", 1, 10)])
    a = resample_1m(t, symbol="XBTUSD", day=D0, prev_close=9.0)
    b = resample_1m(t, symbol="XBTUSD", day=datetime.date(2020, 3, 12), prev_close=9.0)
    c = resample_1m(t, symbol="XBTUSD", day=pd.Timestamp(D0, tz="UTC"), prev_close=9.0)
    pdt.assert_frame_equal(a, b)
    pdt.assert_frame_equal(a, c)
    assert len(a) == 1440
