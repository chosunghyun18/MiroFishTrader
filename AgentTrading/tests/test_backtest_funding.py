"""backtest.costs.apply_funding — 설계 손 계산 예시 C·D·E·F, 경계, 격자 밖 정산, 결측 오류, 스키마·불변성.

기대값은 phase3-backtest.md "펀딩 모델" 11항 표의 12자리 숫자 리터럴이다.
"""

import numpy as np
import pandas as pd
import pytest

from src.backtest.costs import apply_costs, apply_funding
from src.shared import schema as sc
from src.shared.schema import SchemaError
from tests.test_backtest_costs import EX_A, EX_B, make_rt

D0 = pd.Timestamp("2021-01-01T00:00:00Z")
DEFAULT_CLOSE = 10000.0
# 설계 예시의 정산 시각 → (요율, 봉 T − 1분 close)
EX_RATES = {
    D0 + pd.Timedelta(hours=4): (0.0001, 10000.0),
    D0 + pd.Timedelta(hours=12): (0.0003, 10200.0),
    D0 + pd.Timedelta(hours=20): (-0.0001, 9900.0),
}
# 예시 D·F1 은 12:00 요율 +0.0001, mark 10050.0
D_RATES = {D0 + pd.Timedelta(hours=12): (0.0001, 10050.0)}


def h(x):
    return D0 + pd.Timedelta(hours=x)


def net_rt(specs):
    """specs: (예시 dict, entry_ts, exit_ts) 목록 → apply_costs(default) 를 거친 net 프레임."""
    rt = make_rt([s[0] for s in specs])
    rt["entry_ts"] = pd.Series([s[1] for s in specs], dtype=sc.UTC_NS)
    rt["exit_ts"] = pd.Series([s[2] for s in specs], dtype=sc.UTC_NS)
    rt["signal_ts"] = rt["entry_ts"] - pd.Timedelta(minutes=1)
    rt["holding_min"] = ((rt["exit_ts"] - rt["entry_ts"]) / pd.Timedelta(minutes=1)).astype(float)
    return apply_costs(rt, "default")


def funding_frame(rates=None, start=D0, end=D0 + pd.Timedelta(days=2), symbol="XBTUSD", extra=()):
    """[start, end) 격자(04·12·20) 전체 + extra 시각. 요율 기본 0.0, rates 로 덮어쓴다."""
    rates = rates or {}
    times = [t for d in pd.date_range(start, end, freq="D") for t in
             (d + pd.Timedelta(hours=4), d + pd.Timedelta(hours=12), d + pd.Timedelta(hours=20))
             if start <= t < end]
    times = sorted(set(times) | set(extra))
    df = pd.DataFrame({
        "ts": pd.Series(times, dtype=sc.UTC_NS),
        "symbol": pd.array([symbol] * len(times), dtype="string"),
        "funding_rate": [rates.get(t, (0.0, None))[0] for t in times],
    })
    return sc.validate_funding(df)


def bars_frame(rates=None, start=D0, end=D0 + pd.Timedelta(days=2), symbol="XBTUSD"):
    """연속 1분봉, close 기본 10000.0, rates 의 각 T 에 대해 봉 T − 1분 close 를 지정."""
    rates = rates or {}
    ts = pd.date_range(start, end, freq="1min", inclusive="left")
    close = np.full(len(ts), DEFAULT_CLOSE)
    pos = pd.Index(ts)
    for t, (_, mark) in rates.items():
        if mark is not None:
            close[pos.get_loc(t - pd.Timedelta(minutes=1))] = mark
    n = len(ts)
    df = pd.DataFrame({
        "ts": pd.Series(ts, dtype=sc.UTC_NS),
        "symbol": pd.array([symbol] * n, dtype="string"),
        "open": close, "high": close, "low": close, "close": close,
        "volume": np.ones(n, dtype="int64"), "volume_xbt": np.ones(n),
        "trade_count": np.ones(n, dtype="int64"),
        "buy_volume": np.ones(n, dtype="int64"), "sell_volume": np.zeros(n, dtype="int64"),
    })
    return sc.validate_bars_1m(df)


def close(a, b):
    assert abs(a - b) <= 1e-12, (a, b)


def run_one(ex, entry, exit_, rates):
    net = net_rt([(ex, entry, exit_)])
    return apply_funding(net, funding_frame(rates), bars_frame(rates)).iloc[0]


# --- 손 계산(설계 11항) ---------------------------------------------------------------

def test_example_c_long_zero_entry_boundary():
    net = net_rt([(EX_A, h(4), h(5))])
    out = apply_funding(net, funding_frame(EX_RATES), bars_frame(EX_RATES))
    r = out.iloc[0]
    assert r["n_funding"] == 0
    assert r["funding_xbt"] == 0.0 and not np.signbit(r["funding_xbt"])
    assert r["net_pnl_xbt"] == net["net_pnl_xbt"].iloc[0]
    close(r["net_pnl_xbt"], 0.018206180546)
    close(r["net_ret"], 0.018206180546)


def test_example_d_long_one_exit_boundary():
    r = run_one(EX_A, h(8), h(12), D_RATES)
    assert r["n_funding"] == 1
    close(r["funding_xbt"], 0.000199004975)
    close(r["net_pnl_xbt"], 0.018007175571)
    close(r["net_ret"], 0.018007175571)


def test_example_e_long_three():
    r = run_one(EX_A, h(3), h(27), EX_RATES)
    assert r["n_funding"] == 3
    close(r["funding_xbt"], 0.000586215092)
    close(r["net_pnl_xbt"], 0.017619965454)
    close(r["net_ret"], 0.017619965454)


def test_example_f1_short_one():
    r = run_one(EX_B, h(8), h(12), D_RATES)
    assert r["n_funding"] == 1
    close(r["funding_xbt"], -0.000099502488)
    close(r["net_pnl_xbt"], -0.010995548253)
    close(r["net_ret"], -0.021991096507)


def test_example_f2_short_three():
    r = run_one(EX_B, h(3), h(27), EX_RATES)
    assert r["n_funding"] == 3
    close(r["funding_xbt"], -0.000293107546)
    close(r["net_pnl_xbt"], -0.010801943195)
    close(r["net_ret"], -0.021603886390)


def test_examples_together_preserve_order_and_index():
    specs = [(EX_A, h(4), h(5)), (EX_A, h(3), h(27)), (EX_B, h(3), h(27)), (EX_A, h(8), h(12))]
    net = net_rt(specs)
    net.index = [10, 3, 7, 1]
    out = apply_funding(net, funding_frame(EX_RATES), bars_frame(EX_RATES))
    assert list(out.index) == [10, 3, 7, 1]
    assert list(out["trade_id"]) == list(net["trade_id"])
    assert list(out["n_funding"]) == [0, 3, 3, 1]
    close(out["funding_xbt"].iloc[1], 0.000586215092)
    close(out["funding_xbt"].iloc[2], -0.000293107546)
    close(out["funding_xbt"].iloc[3], 20000 / 10200.0 * 0.0003)  # 12:00 = +0.0003, mark 10200


# --- 경계 ----------------------------------------------------------------------------

M = pd.Timedelta(minutes=1)


@pytest.mark.parametrize("entry, exit_, n", [
    (h(12), h(13), 0),          # entry = T → 미부과
    (h(12) - M, h(13), 1),      # entry 바로 앞 → 부과
    (h(11), h(12), 1),          # exit = T → 부과
    (h(11), h(12) - M, 0),      # exit 바로 앞 → 미부과
    (h(12) - M, h(12), 1),      # 1분 보유, T 에 청산
    (h(12) + M, h(19), 0),
])
def test_boundaries(entry, exit_, n):
    r = run_one(EX_A, entry, exit_, D_RATES)
    assert r["n_funding"] == n
    expected = 20000 / 10050.0 * 0.0001 if n else 0.0
    close(r["funding_xbt"], expected)


# --- 격자 밖 정산(검토 의견 1) ---------------------------------------------------------

def test_off_grid_row_inside_holding_is_charged():
    t8 = h(10)  # 예시 D 보유 구간 (08:00, 12:00] 안
    rates = {**D_RATES, t8: (0.0002, 9800.0)}
    net = net_rt([(EX_A, h(8), h(12))])
    out = apply_funding(net, funding_frame(rates, extra=[t8]), bars_frame(rates)).iloc[0]
    assert out["n_funding"] == 2
    close(out["funding_xbt"], 0.000199004975 + 20000 / 9800.0 * 0.0002)


def test_off_grid_row_outside_holding_ignored():
    t = h(8)  # entry_ts = 08:00 와 같음 → 밖
    rates = {**D_RATES, t: (0.0005, 9000.0)}
    r = apply_funding(net_rt([(EX_A, h(8), h(12))]), funding_frame(rates, extra=[t]),
                      bars_frame(rates)).iloc[0]
    assert r["n_funding"] == 1
    close(r["funding_xbt"], 0.000199004975)


# --- 결측 오류 ----------------------------------------------------------------------

def test_missing_grid_row_raises():
    f = funding_frame(D_RATES)
    f = f[f["ts"] != h(12)].reset_index(drop=True)
    with pytest.raises(ValueError, match="펀딩 결측.*2021-01-01 12:00"):
        apply_funding(net_rt([(EX_A, h(8), h(12))]), f, bars_frame(D_RATES))


def test_other_symbol_funding_only_raises():
    with pytest.raises(ValueError, match="펀딩 결측"):
        apply_funding(net_rt([(EX_A, h(8), h(12))]), funding_frame(D_RATES, symbol="ETHUSD"),
                      bars_frame(D_RATES))


def test_missing_mark_bar_raises():
    b = bars_frame(D_RATES)
    b = b[b["ts"] != h(12) - M].reset_index(drop=True)
    with pytest.raises(ValueError, match="mark 봉 없음"):
        apply_funding(net_rt([(EX_A, h(8), h(12))]), funding_frame(D_RATES), b)


def test_missing_mark_bar_for_off_grid_row_raises():
    t = h(10)
    b = bars_frame(D_RATES)
    b = b[b["ts"] != t - M].reset_index(drop=True)
    with pytest.raises(ValueError, match="mark 봉 없음"):
        apply_funding(net_rt([(EX_A, h(8), h(12))]),
                      funding_frame({**D_RATES, t: (0.0001, None)}, extra=[t]), b)


def test_duplicate_mark_bar_raises():
    b = bars_frame(D_RATES)
    b = pd.concat([b, b[b["ts"] == h(12) - M]], ignore_index=True)
    with pytest.raises(ValueError, match="중복"):
        apply_funding(net_rt([(EX_A, h(8), h(12))]), funding_frame(D_RATES), b)


def test_nan_rate_rejected():
    f = funding_frame(D_RATES)
    f.loc[f["ts"] == h(12), "funding_rate"] = np.nan
    with pytest.raises(ValueError):
        apply_funding(net_rt([(EX_A, h(8), h(12))]), f, bars_frame(D_RATES))


def test_missing_outside_holding_is_ok():
    net = net_rt([(EX_A, h(4), h(5))])  # 정산 0회
    r = apply_funding(net, sc.empty_frame(sc.FUNDING), bars_frame()).iloc[0]
    assert r["n_funding"] == 0 and r["funding_xbt"] == 0.0


def test_bars_missing_columns_raises():
    with pytest.raises(ValueError, match="bars 누락"):
        apply_funding(net_rt([(EX_A, h(4), h(5))]), funding_frame(),
                      bars_frame().drop(columns=["close"]))


# --- 스키마·불변성 -------------------------------------------------------------------

def test_schema_columns_and_unchanged_inputs():
    specs = [(EX_A, h(3), h(27)), (EX_B, h(8), h(12)), (EX_A, h(4), h(5))]
    net = net_rt(specs)
    f, b = funding_frame(EX_RATES), bars_frame(EX_RATES)
    net0, f0, b0 = net.copy(), f.copy(), b.copy()
    out = apply_funding(net, f, b)
    pd.testing.assert_frame_equal(net, net0)
    pd.testing.assert_frame_equal(f, f0)
    pd.testing.assert_frame_equal(b, b0)
    assert sc.validate_roundtrips_net_funding(out, strict=True) is out
    assert list(out.columns) == sc.ROUNDTRIPS_NET.column_names + ["n_funding", "funding_xbt"]
    same = [c for c in sc.ROUNDTRIPS_NET.column_names if c not in ("net_pnl_xbt", "net_ret")]
    pd.testing.assert_frame_equal(out[same], net[same])
    np.testing.assert_array_equal(out["net_pnl_xbt"], net["net_pnl_xbt"] - out["funding_xbt"])


def test_zero_rates_keep_net_exact():
    net = net_rt([(EX_A, h(3), h(27)), (EX_B, h(8), h(12))])
    out = apply_funding(net, funding_frame(), bars_frame())
    assert list(out["n_funding"]) == [3, 1]
    assert (out["funding_xbt"] == 0.0).all()
    pd.testing.assert_series_equal(out["net_pnl_xbt"], net["net_pnl_xbt"])
    pd.testing.assert_series_equal(out["net_ret"], net["net_ret"])


def test_empty_input():
    out = apply_funding(sc.empty_frame(sc.ROUNDTRIPS_NET), sc.empty_frame(sc.FUNDING),
                        sc.empty_frame(sc.BARS_1M))
    assert len(out) == 0
    assert list(out.columns) == sc.ROUNDTRIPS_NET_FUNDING.column_names
    assert dict(out.dtypes) == dict(sc.empty_frame(sc.ROUNDTRIPS_NET_FUNDING).dtypes)


def test_double_application_rejected():
    net = net_rt([(EX_A, h(8), h(12))])
    once = apply_funding(net, funding_frame(D_RATES), bars_frame(D_RATES))
    with pytest.raises(SchemaError, match="예상 밖"):
        apply_funding(once, funding_frame(D_RATES), bars_frame(D_RATES))


def test_gross_roundtrips_rejected():
    with pytest.raises(SchemaError):
        apply_funding(make_rt([EX_A]), funding_frame(), bars_frame())
