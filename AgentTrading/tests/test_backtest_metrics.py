"""backtest.metrics — 문서 예시(Sharpe 10.770132901309, MDD 0.2), 게이트 판정 규칙·기본값, 경계 입력, 결정성.

기대값은 phase3-backtest.md "게이트 지표 / 예시" 의 리터럴이다.
"""

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.backtest.costs import apply_costs
from src.backtest.metrics import (
    DEFAULT_GATE,
    GATE_KEYS,
    GATE_VERDICTS,
    SUMMARY_KEYS,
    _n_liq_breach,
    daily_moments,
    daily_returns,
    equity_curve,
    gate_verdict,
    max_drawdown,
    sharpe,
    summarize_net_run,
)
from src.shared import schema as sc
from src.shared.schema import SchemaError

ROOT = Path(__file__).resolve().parents[1]
ZERO = {"taker_fee": 0.0, "maker_fee": 0.0, "slippage_bps": 0.0}
D0 = pd.Timestamp("2020-01-01T00:00:00Z")
DOC_KEYS = ("strategy_id", "param_id", "start", "end", "n_trades", "sharpe", "mdd",
            "total_net_ret", "total_gross_ret", "n_liq_breach", "sr_daily", "skew_daily",
            "kurt_daily", "n_days", "gate")


def make_net(rows, strategy="syn-v1-h1", param="p"):
    """rows: dict 목록(entry_ts, exit_ts, net_ret, 선택 side·entry·exit·trade_id).

    gross 프레임을 만들고 ZERO 프로필로 net 화한 뒤 net_ret 을 지정값으로 덮는다(스키마 유효).
    """
    recs = []
    for i, r in enumerate(rows):
        side = r.get("side", "long")
        p_in, p_out = r.get("entry", 10000.0), r.get("exit", 10000.0)
        qty = 1000
        g = qty * (1 / p_in - 1 / p_out) if side == "long" else qty * (1 / p_out - 1 / p_in)
        recs.append({
            "strategy_id": strategy, "param_id": param, "trade_id": r.get("trade_id", i),
            "symbol": "XBTUSD", "side": side,
            "signal_ts": r["entry_ts"] - pd.Timedelta(minutes=1),
            "entry_ts": r["entry_ts"], "entry_price": p_in,
            "exit_signal_ts": pd.NaT, "exit_ts": r["exit_ts"], "exit_price": p_out,
            "qty": qty, "stop_price": p_in * 0.99, "tp_price": np.nan,
            "entry_reason": "h1_breakout", "exit_reason": "time",
            "holding_min": 30.0, "equity_before": 1.0, "notional_usd": float(qty),
            "leverage": 1.0, "risk_pct": 2.0, "size_capped": False,
            "gross_pnl_xbt": g, "gross_ret": r.get("gross_ret", g),
        })
    if not recs:
        return apply_costs(sc.empty_frame(sc.ROUNDTRIPS), ZERO)
    df = pd.DataFrame(recs)
    df["exit_signal_ts"] = pd.to_datetime(df["exit_signal_ts"], utc=True)
    net = apply_costs(df.astype(sc.ROUNDTRIPS.dtypes), ZERO)
    net["net_ret"] = [float(r["net_ret"]) for r in rows]
    return sc.validate_roundtrips_net(net)


def day_trades(rets, day0=D0):
    """날마다 1건(그날 01:00 진입, 02:00 청산)."""
    return make_net([
        {"entry_ts": day0 + pd.Timedelta(days=i, hours=1),
         "exit_ts": day0 + pd.Timedelta(days=i, hours=2), "net_ret": r}
        for i, r in enumerate(rets)
    ])


def summ(**kw):
    base = {"n_trades": 150, "sharpe": 2.0, "mdd": 0.1}
    base.update(kw)
    return base


# 1. 함수 존재 / 출력 키 ----------------------------------------------------------------------------

def test_public_names_and_keys():
    assert GATE_VERDICTS == ("pass", "fail", "insufficient")
    assert GATE_KEYS == ("min_trades", "min_sharpe", "max_drawdown")
    assert SUMMARY_KEYS == DOC_KEYS
    s = summarize_net_run(day_trades([0.01, 0.02]), "2020-01-01", "2020-01-03")
    assert tuple(s) == DOC_KEYS
    assert s["start"] == "2020-01-01" and s["end"] == "2020-01-03"
    assert (s["strategy_id"], s["param_id"]) == ("syn-v1-h1", "p")
    assert type(s["n_trades"]) is int and type(s["n_days"]) is int and type(s["n_liq_breach"]) is int
    for k in ("sharpe", "mdd", "total_net_ret", "total_gross_ret", "sr_daily", "skew_daily", "kurt_daily"):
        assert type(s[k]) is float, k


# 2. config 기본값 ---------------------------------------------------------------------------------

def test_default_gate_matches_spec_and_config():
    assert DEFAULT_GATE == {"min_trades": 100, "min_sharpe": 1.0, "max_drawdown": 0.30}
    cfg = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text())
    gate_cfg = cfg["backtest"]["gate"]
    assert {k: gate_cfg[k] for k in GATE_KEYS} == DEFAULT_GATE


def test_gate_missing_keys_default_and_extra_ignored():
    s = summ(n_trades=100, sharpe=1.0, mdd=0.30)
    assert gate_verdict(s) == gate_verdict(s, None) == gate_verdict(s, {}) == "pass"
    assert gate_verdict(s, {"min_trades": 101}) == "insufficient"   # 나머지는 기본값
    assert gate_verdict(s, {"min_sharpe": 1.5}) == "fail"
    assert gate_verdict(s, {"max_drawdown": 0.29}) == "fail"
    assert gate_verdict(s, {"min_dsr": 0.99, "whatever": "x"}) == "pass"


@pytest.mark.parametrize("bad", [
    {"min_trades": True}, {"min_trades": -1}, {"min_trades": 1.5}, {"min_trades": "100"},
    {"min_sharpe": float("nan")}, {"min_sharpe": float("inf")}, {"min_sharpe": False},
    {"max_drawdown": -0.1}, {"max_drawdown": None}, [("min_trades", 1)],
])
def test_gate_invalid_values(bad):
    with pytest.raises(ValueError):
        gate_verdict(summ(), bad)


# 3. 손 계산 일치 -----------------------------------------------------------------------------------

DOC_RETS = [0.01, -0.005, 0.0, 0.02]
DOC_SHARPE = 10.770132901309


def test_sharpe_doc_example_direct():
    assert sharpe(DOC_RETS) == pytest.approx(DOC_SHARPE, abs=1e-9)
    assert sharpe(pd.Series(DOC_RETS)) == pytest.approx(DOC_SHARPE, abs=1e-9)
    assert np.std(DOC_RETS, ddof=1) == pytest.approx(0.011086778913, abs=1e-12)


def test_sharpe_doc_example_via_trades():
    rt = day_trades(DOC_RETS)
    d = daily_returns(equity_curve(rt, D0), "2020-01-01", "2020-01-05")
    assert len(d) == 4
    np.testing.assert_allclose(d.to_numpy(), DOC_RETS, atol=1e-12)
    s = summarize_net_run(rt, "2020-01-01", "2020-01-05")
    assert s["sharpe"] == pytest.approx(DOC_SHARPE, abs=1e-9)
    assert s["n_days"] == 4 and s["n_trades"] == 4
    assert s["total_net_ret"] == pytest.approx(1.01 * 0.995 * 1.0 * 1.02 - 1, abs=1e-12)
    assert s["sr_daily"] == pytest.approx(DOC_SHARPE / math.sqrt(365), abs=1e-9)


DOC_EQ = [1.0, 1.1, 0.99, 1.045, 0.88, 1.2]


def test_mdd_doc_example_direct():
    assert max_drawdown(DOC_EQ) == pytest.approx(0.2, abs=1e-12)
    curve = pd.DataFrame({"ts": pd.date_range(D0, periods=6, freq="h"), "equity": DOC_EQ})
    assert max_drawdown(curve) == pytest.approx(0.2, abs=1e-12)


def test_mdd_doc_example_via_trades():
    rets = [DOC_EQ[k] / DOC_EQ[k - 1] - 1 for k in range(1, len(DOC_EQ))]
    rt = day_trades(rets)
    curve = equity_curve(rt, D0)
    np.testing.assert_allclose(curve["equity"].to_numpy(), DOC_EQ, atol=1e-12)
    assert curve["ts"].iloc[0] == D0
    s = summarize_net_run(rt, "2020-01-01", "2020-01-08")
    assert s["mdd"] == pytest.approx(0.2, abs=1e-12)


def test_moments_hand_computed():
    r = [0.01, -0.02, 0.03, 0.0]
    # mean 0.005, 편차 [0.005, −0.025, 0.025, −0.005]
    m2 = (0.005**2 + 0.025**2 + 0.025**2 + 0.005**2) / 4          # 0.000325
    m3 = (0.005**3 - 0.025**3 + 0.025**3 - 0.005**3) / 4          # 0
    m4 = (0.005**4 + 0.025**4 + 0.025**4 + 0.005**4) / 4
    m = daily_moments(r)
    assert m["n_days"] == 4
    assert m["skew_daily"] == pytest.approx(m3 / m2**1.5, abs=1e-12)
    assert m["kurt_daily"] == pytest.approx(m4 / m2**2, abs=1e-12)
    assert m["skew_daily"] == pytest.approx(0.0, abs=1e-12)
    assert m["kurt_daily"] == pytest.approx(313 / 169, abs=1e-12)   # 1.95625e-7 / 1.05625e-7
    assert m["sr_daily"] == pytest.approx(0.005 / np.std(r, ddof=1), abs=1e-12)
    # 비대칭 시퀀스: [0, 0, 0, 1] → m2 = 3/16, m3 = 3/32, m4 = 21/256 → skew = 2/√3, kurt = 7/3
    m = daily_moments([0.0, 0.0, 0.0, 1.0])
    assert m["skew_daily"] == pytest.approx(2 / math.sqrt(3), abs=1e-12)
    assert m["kurt_daily"] == pytest.approx(7 / 3, abs=1e-12)


# 4. 거래 수 우선 -----------------------------------------------------------------------------------

def test_insufficient_overrides_other_metrics():
    assert gate_verdict(summ(n_trades=99, sharpe=5.0, mdd=0.0)) == "insufficient"
    assert gate_verdict(summ(n_trades=99, sharpe=float("nan"), mdd=0.9)) == "insufficient"
    assert gate_verdict(summ(n_trades=99, sharpe=5.0, mdd=0.0), {"min_trades": 99}) == "pass"
    assert gate_verdict(summ(n_trades=0, sharpe=5.0, mdd=0.0), {"min_trades": 0}) == "pass"


def test_fail_and_boundaries():
    assert gate_verdict(summ(sharpe=float("nan"))) == "fail"
    assert gate_verdict(summ(mdd=0.31)) == "fail"
    assert gate_verdict(summ(sharpe=0.99)) == "fail"
    assert gate_verdict(summ(sharpe=1.0, mdd=0.30)) == "pass"


def test_summary_insufficient_on_few_trades():
    s = summarize_net_run(day_trades(DOC_RETS), "2020-01-01", "2020-01-05")
    assert s["sharpe"] > 1 and s["mdd"] < 0.3
    assert s["gate"] == "insufficient"
    assert summarize_net_run(day_trades(DOC_RETS), "2020-01-01", "2020-01-05",
                             gate={"min_trades": 4})["gate"] == "pass"


# 5. 경계 입력 --------------------------------------------------------------------------------------

def test_zero_trades():
    rt = make_net([])
    s = summarize_net_run(rt, "2020-01-01", "2020-01-11")
    assert s["n_trades"] == 0 and s["mdd"] == 0.0
    assert s["total_net_ret"] == 0.0 and s["total_gross_ret"] == 0.0 and s["n_liq_breach"] == 0
    assert math.isnan(s["sharpe"]) and math.isnan(s["sr_daily"])
    assert math.isnan(s["skew_daily"]) and math.isnan(s["kurt_daily"])
    assert s["n_days"] == 10
    assert s["strategy_id"] is None and s["param_id"] is None
    assert s["gate"] == "insufficient"
    assert summarize_net_run(rt, "2020-01-01", "2020-01-11", {"min_trades": 0})["gate"] == "fail"
    curve = equity_curve(rt, D0)
    assert len(curve) == 1 and curve["equity"].iloc[0] == 1.0 and curve["ts"].iloc[0] == D0
    assert max_drawdown(curve) == 0.0
    with pytest.raises(ValueError):
        equity_curve(rt)  # 0건 + start 없음


def test_zero_variance_returns_nan_sharpe():
    assert math.isnan(sharpe([0.01] * 5))
    assert math.isnan(sharpe(pd.Series([0.0] * 30)))
    m = daily_moments([0.01] * 5)
    assert math.isnan(m["sr_daily"]) and math.isnan(m["skew_daily"]) and math.isnan(m["kurt_daily"])
    # 거래는 있지만 모든 net_ret = 0 → 모든 r_d = 0
    s = summarize_net_run(day_trades([0.0] * 5), "2020-01-01", "2020-01-06")
    assert math.isnan(s["sharpe"]) and s["gate"] == "insufficient"
    assert summarize_net_run(day_trades([0.0] * 5), "2020-01-01", "2020-01-06",
                             {"min_trades": 1})["gate"] == "fail"


def test_short_series_nan():
    assert math.isnan(sharpe([]))
    assert math.isnan(sharpe([0.05]))
    assert math.isnan(sharpe([0.01, float("nan"), 0.02]))
    s = summarize_net_run(day_trades([0.05]), "2020-01-01", "2020-01-02")
    assert s["n_days"] == 1 and math.isnan(s["sharpe"]) and math.isnan(s["sr_daily"])
    assert daily_moments([])["n_days"] == 0


def test_same_day_trades_and_empty_days():
    rt = make_net([
        {"entry_ts": D0 + pd.Timedelta(hours=1), "exit_ts": D0 + pd.Timedelta(hours=2), "net_ret": 0.1},
        {"entry_ts": D0 + pd.Timedelta(hours=3), "exit_ts": D0 + pd.Timedelta(hours=4), "net_ret": -0.5},
        # 3일째 진입, 정확히 4일째 00:00 청산 → 4일째로 간다
        {"entry_ts": D0 + pd.Timedelta(days=2, hours=23), "exit_ts": D0 + pd.Timedelta(days=3),
         "net_ret": 0.2},
    ])
    d = daily_returns(equity_curve(rt, D0), "2020-01-01", "2020-01-06")
    np.testing.assert_allclose(d.to_numpy(), [1.1 * 0.5 - 1, 0.0, 0.0, 0.2, 0.0], atol=1e-12)
    assert list(d.index) == list(pd.date_range(D0, periods=5, freq="D"))


def test_invalid_inputs():
    rt = day_trades([0.01, 0.02])
    with pytest.raises(ValueError):
        summarize_net_run(rt, "2020-01-01T12:00", "2020-01-05")   # 자정 아님
    with pytest.raises(ValueError):
        summarize_net_run(rt, "2020-01-05", "2020-01-05")         # end ≤ start
    with pytest.raises(ValueError):
        summarize_net_run(rt, "2020-01-01", "2020-01-02")         # 2번째 청산이 구간 밖
    with pytest.raises(ValueError):
        summarize_net_run(rt, "2020-01-02", "2020-01-05")         # 1번째 청산이 start 전
    mixed = pd.concat([day_trades([0.01]), make_net(
        [{"entry_ts": D0 + pd.Timedelta(hours=5), "exit_ts": D0 + pd.Timedelta(hours=6),
          "net_ret": 0.0}], param="q")], ignore_index=True)
    with pytest.raises(ValueError, match="한 run"):
        summarize_net_run(mixed, "2020-01-01", "2020-01-05")
    with pytest.raises(SchemaError):
        summarize_net_run(rt.drop(columns=["net_ret"]), "2020-01-01", "2020-01-05")


def test_liq_breach_thresholds():
    p = 10000.0
    rows = [
        {"side": "long", "entry": p, "exit": p / 1.095},           # 경계 포함
        {"side": "long", "entry": p, "exit": p / 1.095 + 0.01},    # 제외
        {"side": "short", "entry": p, "exit": p / 0.905},          # 경계 포함
        {"side": "short", "entry": p, "exit": p / 0.905 - 0.01},   # 제외
        {"side": "long", "entry": p, "exit": p * 0.98},            # 정상 손절
    ]
    rt = make_net([dict(r, entry_ts=D0 + pd.Timedelta(hours=2 * i),
                        exit_ts=D0 + pd.Timedelta(hours=2 * i + 1), net_ret=0.0)
                   for i, r in enumerate(rows)])
    assert _n_liq_breach(rt) == 2
    assert summarize_net_run(rt, "2020-01-01", "2020-01-02")["n_liq_breach"] == 2


def test_total_gross_ret():
    rt = make_net([
        {"entry_ts": D0 + pd.Timedelta(hours=1), "exit_ts": D0 + pd.Timedelta(hours=2),
         "net_ret": 0.0, "gross_ret": 0.1},
        {"entry_ts": D0 + pd.Timedelta(hours=3), "exit_ts": D0 + pd.Timedelta(hours=4),
         "net_ret": 0.0, "gross_ret": -0.2},
    ])
    s = summarize_net_run(rt, "2020-01-01", "2020-01-02")
    assert s["total_gross_ret"] == pytest.approx(1.1 * 0.8 - 1, abs=1e-12)
    assert s["total_net_ret"] == 0.0


# 6. 결정성·순서 무관 -------------------------------------------------------------------------------

def _rand_run(seed=7, n=60):
    rng = np.random.default_rng(seed)
    rows, t = [], D0
    for _ in range(n):
        t = t + pd.Timedelta(minutes=int(rng.integers(30, 600)))
        e = t + pd.Timedelta(minutes=int(rng.integers(1, 300)))
        rows.append({"entry_ts": t, "exit_ts": e, "net_ret": float(rng.normal(0.002, 0.02)),
                     "side": "long" if rng.random() < 0.5 else "short"})
        t = e
    return make_net(rows)


def _same(a, b):
    assert list(a) == list(b)
    for k in a:
        if isinstance(a[k], float) and math.isnan(a[k]):
            assert isinstance(b[k], float) and math.isnan(b[k]), k
        else:
            assert a[k] == b[k], k


def test_order_independent_deterministic_and_input_unchanged():
    rt = _rand_run()
    end = (rt["exit_ts"].max().normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    before = rt.copy()
    s1 = summarize_net_run(rt, "2020-01-01", end)
    s2 = summarize_net_run(rt, "2020-01-01", end)
    shuffled = rt.sample(frac=1, random_state=3)
    s3 = summarize_net_run(shuffled, "2020-01-01", end)
    pd.testing.assert_frame_equal(rt, before)
    _same(s1, s2)
    _same(s1, s3)
    assert s1["n_trades"] == 60 and s1["n_days"] == len(pd.date_range("2020-01-01", end, inclusive="left"))
    pd.testing.assert_frame_equal(equity_curve(rt, D0), equity_curve(shuffled, D0))


def test_tie_on_entry_ts_sorted_by_trade_id():
    t = D0 + pd.Timedelta(hours=1)
    rt = make_net([
        {"entry_ts": t, "exit_ts": t + pd.Timedelta(hours=2), "net_ret": 0.5, "trade_id": 2},
        {"entry_ts": t, "exit_ts": t + pd.Timedelta(hours=1), "net_ret": -0.5, "trade_id": 1},
    ])
    curve = equity_curve(rt)
    assert curve["ts"].iloc[0] == t                       # start 없으면 첫 entry_ts
    np.testing.assert_allclose(curve["equity"].to_numpy(), [1.0, 0.5, 0.75], atol=1e-12)
    assert list(curve["ts"].iloc[1:]) == [t + pd.Timedelta(hours=1), t + pd.Timedelta(hours=2)]
    assert max_drawdown(curve) == pytest.approx(0.5, abs=1e-12)
