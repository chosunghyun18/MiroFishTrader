"""analysis.synthetic — 손으로 만든 짧은 봉에서 진입·청산 시각·가격, 상한, 누수 방지, 결정성을 확인."""

import math

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src.analysis.features import compute_features
from src.analysis.synthetic import (
    MAX_EXPOSURE,
    MAX_LEVERAGE,
    Params,
    entry_signals,
    generate_run,
    inverse_pnl,
    param_grid,
    position_size,
    roundtrips_to_fills,
)
from src.shared.schema import (
    BARS_1M,
    FILLS,
    ROUNDTRIPS,
    empty_frame,
    validate_fills,
    validate_roundtrips,
)

T0 = pd.Timestamp("2020-03-12 00:00", tz="UTC")
B = 10000.0  # 기준가. 숏 시나리오는 롱 시나리오를 B 기준으로 뒤집어 만든다.


def ts(i):
    return T0 + pd.Timedelta(minutes=i)


def _bars(rows, symbol="XBTUSD", times=None):
    """rows: 숫자(평평한 봉) 또는 (open, high, low, close)."""
    ohlc = [(float(r),) * 4 if np.isscalar(r) else tuple(float(x) for x in r) for r in rows]
    n = len(ohlc)
    if times is None:
        times = [ts(i) for i in range(n)]
    df = pd.DataFrame({
        "ts": pd.Series(times, dtype="datetime64[ns, UTC]"),
        "symbol": [symbol] * n,
        "open": [r[0] for r in ohlc],
        "high": [r[1] for r in ohlc],
        "low": [r[2] for r in ohlc],
        "close": [r[3] for r in ohlc],
        "volume": [10] * n,
        "volume_xbt": [0.1] * n,
        "trade_count": [1] * n,
        "buy_volume": [5] * n,
        "sell_volume": [5] * n,
    })
    return df.astype(BARS_1M.dtypes)


def _mirror(rows):
    """B 기준 가격 반전: 롱 시나리오 → 같은 모양의 숏 시나리오(high↔low 교환)."""
    out = []
    for r in rows:
        if np.isscalar(r):
            out.append(2 * B - r)
        else:
            o, h, lo, c = r
            out.append((2 * B - o, 2 * B - lo, 2 * B - h, 2 * B - c))
    return out


def _random_bars(n=1500, seed=11):
    rng = np.random.default_rng(seed)
    close = 8000.0 * np.exp(np.cumsum(rng.normal(0, 0.0015, n)))
    open_ = np.r_[close[0], close[:-1]] * np.exp(rng.normal(0, 0.0003, n))
    spread = np.abs(rng.normal(0, 4.0, (2, n)))
    high = np.maximum(open_, close) + spread[0]
    low = np.minimum(open_, close) - spread[1]
    return _bars(list(zip(open_, high, low, close)))


def h1(**kw):
    base = dict(trigger="h1", n=3, stop_pct=1, tp_r=2, max_hold=100, risk_pct=1)
    base.update(kw)
    return Params(**base)


# H1 n=3: 봉 0~2 평평(워밍업), 봉 3 마감 돌파 신호, 봉 4 open(≠ close[3]) 진입.
WARM = [B, B, B]
SIG_LONG = (B, B + 10, B, B + 10)
ENTRY_BAR = (B + 20, B + 30, B + 15, B + 25)


# ---------------------------------------------------------------- 진입·누수 방지

def test_entry_next_bar_open():
    bars = _bars(WARM + [SIG_LONG, ENTRY_BAR, B + 25, B + 25])
    rt = generate_run(bars, h1()).roundtrips
    first = rt.iloc[0]
    assert first["side"] == "long"
    assert first["signal_ts"] == ts(3)
    assert first["entry_ts"] == ts(4)
    assert first["entry_price"] == B + 20
    assert first["entry_price"] != bars["close"].iloc[3]  # 신호 봉 close 체결이 아님
    assert first["entry_reason"] == "h1_breakout"
    assert (rt["entry_ts"] == rt["signal_ts"] + pd.Timedelta(minutes=1)).all()


def test_signal_on_last_bar_is_dropped():
    res = generate_run(_bars(WARM + [SIG_LONG]), h1())
    assert len(res.roundtrips) == 0
    assert res.skipped_min_qty == 0
    validate_roundtrips(res.roundtrips)


def test_prefix_invariance_no_lookahead():
    bars = _random_bars()
    p = h1(n=15, stop_pct=0.5, tp_r=2, max_hold=30, risk_pct=2)
    full = generate_run(bars, p).roundtrips
    assert len(full) > 10
    for m in (200, 517, 1000):
        part = generate_run(bars.iloc[:m].copy(), p).roundtrips
        closed = part[part["exit_reason"] != "end_of_data"]
        assert len(closed) > 0
        pdt.assert_frame_equal(closed.reset_index(drop=True),
                               full.iloc[:len(closed)].reset_index(drop=True))


def test_changing_bars_after_signal_keeps_entry():
    rows = WARM + [SIG_LONG, ENTRY_BAR, B + 25, B + 25, B + 25]
    a = generate_run(_bars(rows), h1()).roundtrips.iloc[0]
    rows2 = rows[:5] + [(B + 25, B + 900, B - 900, B - 500), B - 500, B - 500]
    b = generate_run(_bars(rows2), h1()).roundtrips.iloc[0]
    for col in ("signal_ts", "entry_ts", "entry_price", "qty", "stop_price", "tp_price"):
        assert a[col] == b[col]


# ---------------------------------------------------------------- 청산 4종

@pytest.mark.parametrize("side", ["long", "short"])
def test_stop_loss_and_reentry_after_stop(side):
    rows = WARM + [SIG_LONG, ENTRY_BAR, (B + 25, B + 30, B - 100, B - 50)] + [B - 50] * 3
    if side == "short":
        rows = _mirror(rows)
    res = generate_run(_bars(rows), h1())
    rt = res.roundtrips
    first = rt.iloc[0]
    entry = B + 20 if side == "long" else B - 20
    stop = entry * 0.99 if side == "long" else entry * 1.01
    assert first["side"] == side
    assert first["entry_price"] == entry
    assert first["stop_price"] == pytest.approx(stop)
    assert first["exit_reason"] == "stop"
    assert first["exit_ts"] == ts(5)
    assert first["exit_price"] == pytest.approx(stop)
    assert pd.isna(first["exit_signal_ts"])
    assert first["holding_min"] == 1.0
    # 사이징: 절단 없이 손절 손실 = 1%·E (인버스 공식)
    assert not first["size_capped"]
    assert first["gross_pnl_xbt"] == pytest.approx(-0.01, rel=1e-3)
    assert first["gross_ret"] == pytest.approx(first["gross_pnl_xbt"] / 1.0)
    # P3: 손절 봉(5) 마감 반대 방향 돌파 신호 → 봉 6 진입 = exit_ts + 1m
    second = rt.iloc[1]
    assert second["signal_ts"] == ts(5)
    assert second["entry_ts"] == first["exit_ts"] + pd.Timedelta(minutes=1)
    assert second["side"] == ("short" if side == "long" else "long")
    # 복리
    assert second["equity_before"] == 1.0 + first["gross_pnl_xbt"]
    # 마지막 봉 X4
    assert second["exit_reason"] == "end_of_data"
    assert second["exit_ts"] == ts(8)
    assert second["exit_signal_ts"] == ts(8)
    assert second["exit_price"] == rows[8]
    assert len(rt) == 2
    assert not res.halted


@pytest.mark.parametrize("side", ["long", "short"])
def test_take_profit(side):
    rows = WARM + [SIG_LONG, ENTRY_BAR, (B + 25, B + 450, B + 20, B + 400)]
    if side == "short":
        rows = _mirror(rows)
    first = generate_run(_bars(rows), h1(tp_r=2)).roundtrips.iloc[0]
    entry = B + 20 if side == "long" else B - 20
    tp = entry * 1.02 if side == "long" else entry * 0.98
    assert first["exit_reason"] == "take_profit"
    assert first["exit_ts"] == ts(5)
    assert first["exit_price"] == pytest.approx(tp)
    assert first["tp_price"] == pytest.approx(tp)
    assert first["gross_pnl_xbt"] > 0


@pytest.mark.parametrize("side", ["long", "short"])
def test_stop_wins_when_both_touched(side):
    rows = WARM + [SIG_LONG, ENTRY_BAR, (B + 25, B + 600, B - 600, B)]
    if side == "short":
        rows = _mirror(rows)
    first = generate_run(_bars(rows), h1(tp_r=1)).roundtrips.iloc[0]
    assert first["exit_reason"] == "stop"
    assert first["exit_price"] == pytest.approx(first["stop_price"])


@pytest.mark.parametrize("side", ["long", "short"])
def test_gap_fills_at_open(side):
    stop_gap = WARM + [SIG_LONG, ENTRY_BAR, (B - 200, B - 150, B - 250, B - 200)]
    tp_gap = WARM + [SIG_LONG, ENTRY_BAR, (B + 500, B + 550, B + 450, B + 500)]
    if side == "short":
        stop_gap, tp_gap = _mirror(stop_gap), _mirror(tp_gap)
    a = generate_run(_bars(stop_gap), h1()).roundtrips.iloc[0]
    assert a["exit_reason"] == "stop"
    assert a["exit_price"] == stop_gap[5][0]
    assert a["gross_pnl_xbt"] < -0.01  # 갭 손절은 예정 손실을 넘을 수 있다
    b = generate_run(_bars(tp_gap), h1(tp_r=2)).roundtrips.iloc[0]
    assert b["exit_reason"] == "take_profit"
    assert b["exit_price"] == tp_gap[5][0]


def test_stop_inside_entry_bar_without_gap_rule():
    rows = WARM + [SIG_LONG, (B + 20, B + 20, B - 100, B - 50), B - 50]
    first = generate_run(_bars(rows), h1()).roundtrips.iloc[0]
    assert first["exit_reason"] == "stop"
    assert first["exit_ts"] == ts(4)
    assert first["exit_price"] == pytest.approx((B + 20) * 0.99)
    assert first["holding_min"] == 0.0


def test_time_exit_discards_signal_on_x3_bar_then_end_of_data():
    flat = (B + 20, B + 20, B + 20, B + 20)
    rows = WARM + [SIG_LONG, flat, flat,
                   (B + 20, B + 100, B + 20, B + 100),   # 6 = e+2: X3 신호 봉, 돌파 신호는 버림
                   (B + 30, B + 30, B + 30, B + 30),     # 7: X3 체결(open)
                   (B + 30, B + 200, B + 30, B + 200),   # 8: 새 돌파 신호
                   (B + 210, B + 220, B + 205, B + 215)]  # 9: 진입 봉이자 마지막 봉 → X4
    res = generate_run(_bars(rows), h1(tp_r=None, max_hold=3))
    rt = res.roundtrips
    assert len(rt) == 2
    first, second = rt.iloc[0], rt.iloc[1]
    assert first["exit_reason"] == "time"
    assert first["exit_signal_ts"] == ts(6)
    assert first["exit_ts"] == ts(7)
    assert first["exit_price"] == B + 30
    assert first["holding_min"] == 3.0
    assert pd.isna(first["tp_price"])
    assert ts(6) not in set(rt["signal_ts"])
    assert second["signal_ts"] == ts(8)
    assert second["entry_ts"] == ts(9)
    assert second["exit_reason"] == "end_of_data"
    assert second["exit_ts"] == ts(9)
    assert second["exit_signal_ts"] == ts(9)
    assert second["exit_price"] == B + 215
    assert second["holding_min"] == 0.0


def test_time_exit_open_beyond_stop_is_time_and_signal_on_exec_bar():
    flat = (B + 20, B + 20, B + 20, B + 20)
    rows = WARM + [SIG_LONG, flat, flat, flat,
                   (B - 300, B + 200, B - 300, B + 200),  # 7: X3 체결 open(손절가 밖), 마감 돌파
                   B + 250, B + 250]
    rt = generate_run(_bars(rows), h1(tp_r=None, max_hold=3)).roundtrips
    first = rt.iloc[0]
    assert first["exit_reason"] == "time"
    assert first["exit_price"] == B - 300
    # P3: X3 체결 봉 마감 신호는 유효
    assert rt.iloc[1]["signal_ts"] == ts(7)
    assert rt.iloc[1]["entry_ts"] == ts(8)


def test_x3_signal_on_last_bar_is_end_of_data():
    flat = (B + 20, B + 20, B + 20, B + 20)
    rows = WARM + [SIG_LONG, flat, flat, (B + 20, B + 20, B + 20, B + 40)]
    first = generate_run(_bars(rows), h1(tp_r=None, max_hold=3)).roundtrips.iloc[0]
    assert first["exit_reason"] == "end_of_data"
    assert first["exit_price"] == B + 40


# ---------------------------------------------------------------- 상한·사이징

def test_no_overlap_and_caps_on_random_bars():
    bars = _random_bars()
    p = h1(n=3, stop_pct=0.5, tp_r=1, max_hold=5, risk_pct=5)
    rt = generate_run(bars, p).roundtrips
    assert len(rt) > 30
    assert (rt["entry_ts"].iloc[1:].to_numpy() > rt["exit_ts"].iloc[:-1].to_numpy()).all()
    assert (rt["entry_ts"] <= rt["exit_ts"]).all()
    assert rt["size_capped"].all()
    expected_qty = [math.floor(MAX_EXPOSURE * e * px)
                    for e, px in zip(rt["equity_before"], rt["entry_price"])]
    assert rt["qty"].tolist() == expected_qty
    assert (rt["leverage"] <= MAX_EXPOSURE + 1e-12).all()
    assert (rt["leverage"] > 3.99).all()
    assert MAX_EXPOSURE <= MAX_LEVERAGE


def test_uncapped_sizing_risk_1_stop_2():
    rt = generate_run(_random_bars(), h1(n=15, stop_pct=2, risk_pct=1)).roundtrips
    assert len(rt) > 0
    assert not rt["size_capped"].any()
    assert (rt["leverage"] < 0.6).all()


@pytest.mark.parametrize("side", ["long", "short"])
def test_position_size_planned_loss_is_risk(side):
    qty, capped = position_size(1.0, 10007.0, side, stop_pct=2, risk_pct=1)
    factor = 0.98 if side == "long" else 1.02
    expected = math.floor(0.01 * 10007.0 * factor / 0.02)
    assert qty == expected and not capped
    stop = 10007.0 * (0.98 if side == "long" else 1.02)
    assert -inverse_pnl(side, qty, 10007.0, stop) == pytest.approx(0.01, rel=1e-3)


def test_position_size_capped():
    qty, capped = position_size(1.0, 10007.0, "long", stop_pct=0.5, risk_pct=5)
    assert capped
    assert qty == math.floor(4 * 10007.0)


def test_hard_guard_raises():
    with pytest.raises(ValueError, match="R4"):
        position_size(1.0, 10007.0, "long", stop_pct=10, risk_pct=50)
    with pytest.raises(ValueError, match="R4"):
        position_size(1.0, 10007.0, "short", stop_pct=10, risk_pct=50)
    bars = _bars(WARM + [SIG_LONG, ENTRY_BAR, B + 25])
    with pytest.raises(ValueError, match="R4"):
        generate_run(bars, h1(stop_pct=10, risk_pct=50))


def test_inverse_pnl():
    assert inverse_pnl("long", 100, 100.0, 200.0) == pytest.approx(0.5)
    assert inverse_pnl("short", 100, 100.0, 50.0) == pytest.approx(1.0)


def test_skipped_min_qty_keeps_evaluating():
    rows = WARM + [SIG_LONG, ENTRY_BAR, (B + 25, B + 30, B - 100, B - 50)] + [B - 50] * 3
    res = generate_run(_bars(rows), h1(), initial_equity=1e-6)
    # 신호 봉 3·4(돌파)·5(하향 돌파) 모두 qty < 1 로 건너뜀
    assert res.skipped_min_qty == 3
    assert len(res.roundtrips) == 0
    pdt.assert_frame_equal(res.roundtrips, empty_frame(ROUNDTRIPS))


def test_halt_when_equity_exhausted():
    rows = WARM + [SIG_LONG, ENTRY_BAR, (1000, 1000, 1000, 1000),
                   (1000, 5000, 1000, 5000), 5000, 5000]
    res = generate_run(_bars(rows), h1(stop_pct=0.5, risk_pct=5))
    assert res.halted
    assert len(res.roundtrips) == 1
    first = res.roundtrips.iloc[0]
    assert first["exit_price"] == 1000.0
    assert first["equity_before"] + first["gross_pnl_xbt"] <= 0


def test_empty_bars():
    res = generate_run(_bars([]), h1())
    pdt.assert_frame_equal(res.roundtrips, empty_frame(ROUNDTRIPS))
    assert res.skipped_min_qty == 0 and not res.halted


# ---------------------------------------------------------------- 트리거 H2·H3

def _fake(n, **cols):
    return pd.DataFrame({f"{k}_{n}": np.asarray(v, dtype="float64") for k, v in cols.items()})


def test_h2_signals_and_sigma_zero():
    p = Params(trigger="h2", n=3, k=2, stop_pct=1, tp_r=None, max_hold=10, risk_pct=1)
    feats = _fake(3, mom=[np.nan, 0.03, -0.03, 0.01, 0.05, -0.05],
                  sigma=[np.nan, 0.01, 0.01, 0.01, 0.0, np.nan])
    long, short = entry_signals(None, feats, p)
    assert long.tolist() == [False, True, False, False, False, False]
    assert short.tolist() == [False, False, True, False, False, False]


def test_h3_signals():
    p = Params(trigger="h3", n=3, k=1.5, stop_pct=1, tp_r=None, max_hold=10, risk_pct=1)
    feats = _fake(3, z=[np.nan, -1.5, 1.5, -1.4, 1.4, -3.0])
    long, short = entry_signals(None, feats, p)
    assert long.tolist() == [False, True, False, False, False, True]
    assert short.tolist() == [False, False, True, False, False, False]


def test_h1_signals_against_features():
    bars = _random_bars(400)
    p = h1(n=15)
    feats = compute_features(bars, windows=(15,))
    long, short = entry_signals(bars, feats, p)
    close = bars["close"].to_numpy()
    assert not long[:15].any() and not short[:15].any()
    j = int(np.flatnonzero(long)[0])
    assert close[j] > bars["high"].iloc[j - 15:j].max()


@pytest.mark.parametrize("trigger,reason", [("h2", "h2_momentum"), ("h3", "h3_meanrev")])
def test_h2_h3_end_to_end(trigger, reason):
    bars = _random_bars()
    p = Params(trigger=trigger, n=15, k=1.5, stop_pct=0.5, tp_r=2, max_hold=30, risk_pct=2)
    rt = generate_run(bars, p).roundtrips
    assert len(rt) > 0
    assert (rt["entry_reason"] == reason).all()
    assert (rt["strategy_id"] == f"syn-v1-{trigger}").all()
    long, short = entry_signals(bars, compute_features(bars, windows=(15,)), p)
    sig = pd.Series(long | short, index=bars["ts"])
    assert sig.loc[rt["signal_ts"]].all()
    # 첫 거래는 첫 신호 봉에서 나온다(워밍업 NaN 구간 신호 없음)
    assert rt["signal_ts"].iloc[0] == bars["ts"].iloc[int(np.flatnonzero(long | short)[0])]
    assert int(np.flatnonzero(long | short)[0]) >= 14


# ---------------------------------------------------------------- 식별자·그리드

def test_param_id_and_strategy_id():
    p = Params(trigger="h1", n=60, stop_pct=1, tp_r=2, max_hold=240, risk_pct=2)
    assert p.param_id == "max_hold=240;n=60;risk_pct=2;stop_pct=1;tp_r=2;trigger=h1"
    assert p.strategy_id == "syn-v1-h1"
    q = Params(trigger="h2", n=15, k=1.5, stop_pct=0.5, tp_r=None, max_hold=60, risk_pct=5)
    assert q.param_id == "k=1.5;max_hold=60;n=15;risk_pct=5;stop_pct=0.5;tp_r=none;trigger=h2"
    assert Params(trigger="h3", n=15, k=2.0, stop_pct=2.0, tp_r=3.0, max_hold=1440,
                  risk_pct=1.0).param_id == \
        "k=2;max_hold=1440;n=15;risk_pct=1;stop_pct=2;tp_r=3;trigger=h3"


def test_param_grid_counts():
    assert len(param_grid("h1")) == 324
    assert len(param_grid("h2")) == 972
    assert len(param_grid("h3")) == 972
    grid = param_grid()
    assert len(grid) == 2268
    keys = [(g.strategy_id, g.param_id) for g in grid]
    assert len(set(keys)) == 2268
    assert keys == sorted(keys)
    assert param_grid() == grid


@pytest.mark.parametrize("kw", [
    dict(trigger="h4"),
    dict(n=1),
    dict(n=3.0),
    dict(k=1.5),                      # h1 에 k
    dict(trigger="h2"),               # h2 에 k 없음
    dict(trigger="h3", k=0),
    dict(stop_pct=0),
    dict(stop_pct=100),
    dict(tp_r=0),
    dict(max_hold=0),
    dict(risk_pct=-1),
])
def test_params_invalid(kw):
    with pytest.raises(ValueError):
        h1(**kw)


# ---------------------------------------------------------------- 입력 오류

def test_input_not_continuous_raises():
    times = [ts(0), ts(1), ts(3), ts(4), ts(5)]
    with pytest.raises(ValueError):
        generate_run(_bars([B] * 5, times=times), h1())


def test_input_multiple_symbols_raises():
    a = _bars([B] * 5)
    b = _bars([B] * 5, symbol="ETHUSD")
    with pytest.raises(ValueError):
        generate_run(pd.concat([a, b], ignore_index=True), h1())


def test_initial_equity_invalid():
    with pytest.raises(ValueError):
        generate_run(_bars([B] * 5), h1(), initial_equity=0)


# ---------------------------------------------------------------- 스키마·fills·결정성

def test_schema_and_fills():
    rows = WARM + [SIG_LONG, ENTRY_BAR, (B + 25, B + 30, B - 100, B - 50)] + [B - 50] * 3
    p = h1()
    rt = generate_run(_bars(rows), p).roundtrips
    validate_roundtrips(rt)
    assert list(rt.columns) == ROUNDTRIPS.column_names
    assert rt["trade_id"].tolist() == [0, 1]
    assert (rt["notional_usd"] == rt["qty"]).all()
    assert (rt["leverage"] == (rt["qty"] / rt["entry_price"]) / rt["equity_before"]).all()
    assert (rt["risk_pct"] == 1.0).all()

    fills = roundtrips_to_fills(rt)
    validate_fills(fills)
    assert list(fills.columns) == FILLS.column_names
    assert len(fills) == 2 * len(rt)
    assert (fills["source"] == "synthetic").all()
    assert (fills["strategy_id"] == "syn-v1-h1").all()
    assert fills["fee"].isna().all() and fills["fee_currency"].isna().all()
    pre = f"syn-v1-h1:{p.param_id}"
    by_id = fills.set_index("source_id")
    e0, x0 = by_id.loc[f"{pre}:0:entry"], by_id.loc[f"{pre}:0:exit"]
    assert (e0["side"], x0["side"]) == ("buy", "sell")          # 롱
    assert e0["ts"] == rt["entry_ts"].iloc[0] and x0["ts"] == rt["exit_ts"].iloc[0]
    assert e0["price"] == rt["entry_price"].iloc[0] and x0["price"] == rt["exit_price"].iloc[0]
    e1, x1 = by_id.loc[f"{pre}:1:entry"], by_id.loc[f"{pre}:1:exit"]
    assert (e1["side"], x1["side"]) == ("sell", "buy")          # 숏
    assert (fills["ts"].diff().dropna() >= pd.Timedelta(0)).all()


def test_fills_empty():
    pdt.assert_frame_equal(roundtrips_to_fills(empty_frame(ROUNDTRIPS)), empty_frame(FILLS))


def test_deterministic_and_input_unchanged():
    bars = _random_bars()
    before = bars.copy()
    p = h1(n=15, stop_pct=0.5, tp_r=None, max_hold=60, risk_pct=2)
    a = generate_run(bars, p)
    b = generate_run(bars, p)
    pdt.assert_frame_equal(a.roundtrips, b.roundtrips)
    assert (a.skipped_min_qty, a.halted) == (b.skipped_min_qty, b.halted)
    pdt.assert_frame_equal(bars, before)
    pdt.assert_frame_equal(roundtrips_to_fills(a.roundtrips), roundtrips_to_fills(b.roundtrips))
