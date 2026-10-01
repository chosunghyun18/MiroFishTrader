"""backtest.costs — 문서 손 계산 예시 A·B, 유동성 매핑·슬리피지 방향, 0 프로필, 입력 불변, 0행, 잘못된 입력.

기대값은 phase3-backtest.md "손 계산 예시" 표의 12자리 숫자 리터럴이다.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.backtest.costs import PROFILE_KEYS, PROFILES, apply_costs, profile_from_config
from src.shared import schema as sc
from src.shared.schema import SchemaError

T0 = pd.Timestamp("2020-01-01T00:00:00Z")
ROOT = Path(__file__).resolve().parents[1]
ZERO = {"taker_fee": 0.0, "maker_fee": 0.0, "slippage_bps": 0.0}
NET_COLS = ["entry_liquidity", "exit_liquidity", "entry_fill_price", "exit_fill_price",
            "fee_xbt", "slippage_xbt", "net_pnl_xbt", "net_ret"]


def gross_pnl(side, qty, p_in, p_out):
    return qty * (1 / p_in - 1 / p_out) if side == "long" else qty * (1 / p_out - 1 / p_in)


def make_rt(rows):
    """rows: dict 목록(side, qty, entry, exit, reason, 선택 equity). gross 는 가격에서 계산."""
    recs = []
    for i, r in enumerate(rows):
        entry_ts = T0 + pd.Timedelta(hours=i)
        exit_ts = entry_ts + pd.Timedelta(minutes=30)
        eq = r.get("equity", 1.0)
        g = gross_pnl(r["side"], r["qty"], r["entry"], r["exit"])
        recs.append({
            "strategy_id": "syn-v1-h1", "param_id": "p", "trade_id": i, "symbol": "XBTUSD",
            "side": r["side"], "signal_ts": entry_ts - pd.Timedelta(minutes=1),
            "entry_ts": entry_ts, "entry_price": r["entry"],
            "exit_signal_ts": pd.NaT, "exit_ts": exit_ts, "exit_price": r["exit"],
            "qty": r["qty"], "stop_price": r["entry"] * 0.99, "tp_price": np.nan,
            "entry_reason": "h1_breakout", "exit_reason": r["reason"],
            "holding_min": 30.0, "equity_before": eq, "notional_usd": float(r["qty"]),
            "leverage": 1.0, "risk_pct": 2.0, "size_capped": False,
            "gross_pnl_xbt": g, "gross_ret": g / eq,
        })
    df = pd.DataFrame(recs)
    df["exit_signal_ts"] = pd.to_datetime(df["exit_signal_ts"], utc=True)
    return df.astype(sc.ROUNDTRIPS.dtypes)


EX_A = dict(side="long", qty=20000, entry=10000.0, exit=10100.0, reason="take_profit", equity=1.0)
EX_B = dict(side="short", qty=10000, entry=10000.0, exit=10100.0, reason="stop", equity=0.5)

MANY = [
    dict(side="long", qty=1500, entry=9000.0, exit=9200.0, reason="take_profit"),
    dict(side="long", qty=800, entry=9100.0, exit=9000.0, reason="stop", equity=0.9),
    dict(side="short", qty=1200, entry=9500.0, exit=9300.0, reason="take_profit", equity=1.1),
    dict(side="short", qty=40, entry=60000.0, exit=61000.0, reason="time", equity=0.3),
    dict(side="long", qty=3, entry=3500.5, exit=3499.0, reason="end_of_data", equity=0.01),
    dict(side="short", qty=999, entry=12000.0, exit=11999.5, reason="end_of_data"),
]


def close(a, b):
    assert abs(a - b) <= 1e-12, (a, b)


def test_example_a_long_taker_maker():
    rt = make_rt([EX_A])
    close(rt["gross_pnl_xbt"].iloc[0], 0.019801980198)
    r = apply_costs(rt, "default").iloc[0]
    assert (r["entry_liquidity"], r["exit_liquidity"]) == ("taker", "maker")
    close(r["entry_fill_price"], 10002.0)
    close(r["exit_fill_price"], 10100.0)
    close(r["fee_xbt"], 0.001195879636)
    close(r["slippage_xbt"], 0.000399920016)
    close(r["net_pnl_xbt"], 0.018206180546)
    close(r["net_ret"], 0.018206180546)


def test_example_b_short_taker_taker():
    rt = make_rt([EX_B])
    close(rt["gross_pnl_xbt"].iloc[0], -0.009900990099)
    close(rt["gross_ret"].iloc[0], -0.019801980198)
    r = apply_costs(rt, "default").iloc[0]
    assert (r["entry_liquidity"], r["exit_liquidity"]) == ("taker", "taker")
    close(r["entry_fill_price"], 9998.0)
    close(r["exit_fill_price"], 10102.02)
    close(r["fee_xbt"], 0.000796040428)
    close(r["slippage_xbt"], 0.000398020214)
    close(r["net_pnl_xbt"], -0.011095050741)
    close(r["net_ret"], -0.022190101482)


def test_example_b_bybit():
    r = apply_costs(make_rt([EX_B]), "bybit").iloc[0]
    close(r["fee_xbt"], 0.001094555588)
    close(r["net_pnl_xbt"], -0.011393565901)
    close(r["net_ret"], -0.022787131803)
    close(r["slippage_xbt"], 0.000398020214)


@pytest.mark.parametrize("reason,liq", [
    ("take_profit", "maker"), ("stop", "taker"), ("time", "taker"), ("end_of_data", "taker"),
])
@pytest.mark.parametrize("side", ["long", "short"])
def test_exit_liquidity_and_fill_direction(reason, liq, side):
    rt = make_rt([dict(side=side, qty=1000, entry=10000.0, exit=10000.0, reason=reason)])
    r = apply_costs(rt, "default").iloc[0]
    assert r["entry_liquidity"] == "taker"
    assert r["exit_liquidity"] == liq
    # 불리한 쪽: 롱은 비싸게 사고 싸게 판다, 숏은 싸게 팔고 비싸게 산다.
    if side == "long":
        assert r["entry_fill_price"] == pytest.approx(10002.0)
        exp_exit = 10000.0 if liq == "maker" else 9998.0
    else:
        assert r["entry_fill_price"] == pytest.approx(9998.0)
        exp_exit = 10000.0 if liq == "maker" else 10002.0
    assert r["exit_fill_price"] == pytest.approx(exp_exit)
    rate_exit = 0.0002 if liq == "maker" else 0.0004
    exp_fee = 1000 / r["entry_fill_price"] * 0.0004 + 1000 / r["exit_fill_price"] * rate_exit
    assert r["fee_xbt"] == pytest.approx(exp_fee, rel=1e-12)


def test_zero_profile_net_equals_gross():
    rt = make_rt(MANY)
    net = apply_costs(rt, ZERO)
    assert (net["entry_fill_price"] == rt["entry_price"]).all()
    assert (net["exit_fill_price"] == rt["exit_price"]).all()
    assert (net["fee_xbt"] == 0).all()
    assert np.allclose(net["slippage_xbt"], 0.0, rtol=0, atol=1e-15)
    assert np.allclose(net["net_pnl_xbt"], rt["gross_pnl_xbt"], rtol=1e-12, atol=1e-15)
    assert np.allclose(net["net_ret"], rt["gross_ret"], rtol=1e-12, atol=1e-15)


@pytest.mark.parametrize("profile", ["default", "bybit"])
def test_invariants(profile):
    net = apply_costs(make_rt(MANY), profile)
    assert (net["fee_xbt"] > 0).all()
    assert (net["slippage_xbt"] >= -1e-15).all()
    assert np.allclose(net["net_pnl_xbt"],
                       net["gross_pnl_xbt"] - net["slippage_xbt"] - net["fee_xbt"],
                       rtol=1e-12, atol=1e-15)
    assert np.allclose(net["net_ret"], net["net_pnl_xbt"] / net["equity_before"], rtol=1e-12)
    # 슬리피지가 없는 maker 청산 + taker 진입이라도 슬리피지 비용은 양수
    assert (net["slippage_xbt"] > 0).all()


def test_input_unchanged_and_gross_preserved():
    rt = make_rt(MANY)
    rt.index = [10, 3, 7, 0, 5, 1]   # 비기본 인덱스도 그대로 유지
    before = rt.copy(deep=True)
    net = apply_costs(rt, "default")
    pd.testing.assert_frame_equal(rt, before)
    assert list(rt.columns) == sc.ROUNDTRIPS.column_names
    assert list(net.columns) == sc.ROUNDTRIPS_NET.column_names
    assert list(net.columns[-8:]) == NET_COLS
    pd.testing.assert_frame_equal(net[sc.ROUNDTRIPS.column_names], before)
    assert sc.validate_roundtrips_net(net) is net
    assert net is not rt


def test_zero_rows():
    net = apply_costs(sc.empty_frame(sc.ROUNDTRIPS), "default")
    assert len(net) == 0
    assert list(net.columns) == sc.ROUNDTRIPS_NET.column_names
    sc.validate_roundtrips_net(net)


@pytest.mark.parametrize("profile", [
    "nope",
    {**PROFILES["default"], "taker_fee": -0.0001},
    {**PROFILES["default"], "slippage_bps": float("nan")},
    {**PROFILES["default"], "maker_fee": float("inf")},
    {"taker_fee": 0.0004, "maker_fee": 0.0002},
    {**PROFILES["default"], "maker_fee": "0.0002"},
    {**PROFILES["default"], "slippage_bps": True},
    None,
    3,
])
def test_bad_profile_raises(profile):
    with pytest.raises(ValueError):
        apply_costs(make_rt([EX_A]), profile)


def test_profile_extra_keys_ignored_and_int_converted():
    prof = {"taker_fee": 0.0004, "maker_fee": 0.0002, "slippage_bps": 2, "note": "x"}
    a = apply_costs(make_rt([EX_A]), prof)
    b = apply_costs(make_rt([EX_A]), "default")
    pd.testing.assert_frame_equal(a, b)


def test_bad_input_frame_raises_schema_error():
    rt = make_rt([EX_A])
    with pytest.raises(SchemaError):
        apply_costs(rt.drop(columns=["qty"]), "default")
    net = apply_costs(rt, "default")
    with pytest.raises(SchemaError):   # 이중 적용 방지(strict)
        apply_costs(net, "default")


def test_profiles_match_doc():
    assert PROFILE_KEYS == ("taker_fee", "maker_fee", "slippage_bps")
    assert PROFILES == {
        "default": {"taker_fee": 0.0004, "maker_fee": 0.0002, "slippage_bps": 2.0},
        "bybit": {"taker_fee": 0.00055, "maker_fee": 0.0002, "slippage_bps": 2.0},
    }


def test_profile_from_config():
    cfg = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    prof = profile_from_config(cfg)
    assert prof == PROFILES["default"]
    assert all(isinstance(v, float) for v in prof.values())
    assert prof is not PROFILES["default"]
    assert profile_from_config({}) == PROFILES["default"]
    assert profile_from_config({"backtest": None}) == PROFILES["default"]
    assert profile_from_config({"backtest": {"taker_fee": 0.00055}}) == PROFILES["bybit"]
    with pytest.raises(ValueError):
        profile_from_config({"backtest": {"maker_fee": -1}})
    with pytest.raises(ValueError):
        profile_from_config({"backtest": {"slippage_bps": "2"}})
