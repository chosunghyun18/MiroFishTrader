"""analysis.patterns — 손 계산 픽스처와 분포 지표 비교, 경계 사례(0건·전승·전패·1건), 입력 처리.

기대값은 모두 손으로 계산한 숫자 리터럴이다(넘파이·구현 함수로 다시 계산하지 않음).
설계 근거: phase2-synthetic-strategy.md "분포 지표".
"""

import numpy as np
import pandas as pd
import pytest

from src.analysis.patterns import SUMMARY_KEYS, summarize_run, summarize_runs
from src.shared import schema as sc
from src.shared.schema import SchemaError

T0 = pd.Timestamp("2020-01-01T00:00:00Z")
SID = "syn-v1-h1"
PID = "max_hold=240;n=60;risk_pct=2;stop_pct=1;tp_r=2;trigger=h1"


def approx(x):
    return pytest.approx(x, rel=1e-12, abs=1e-12)


def make_rt(rows, sid=SID, pid=PID):
    """rows: dict 목록(side, exit, hold, lev, capped, ret, 선택 qty·entry·stop). equity_before = 1."""
    recs = []
    for i, r in enumerate(rows):
        entry_ts = T0 + pd.Timedelta(hours=i)
        exit_ts = entry_ts + pd.Timedelta(minutes=r["hold"])
        recs.append({
            "strategy_id": sid, "param_id": pid, "trade_id": i, "symbol": "XBTUSD",
            "side": r["side"],
            "signal_ts": entry_ts - pd.Timedelta(minutes=1), "entry_ts": entry_ts,
            "entry_price": r.get("entry", 10000.0),
            "exit_signal_ts": (exit_ts - pd.Timedelta(minutes=1)
                               if r["exit"] in ("time", "end_of_data") else pd.NaT),
            "exit_ts": exit_ts, "exit_price": 10000.0,
            "qty": r.get("qty", 100), "stop_price": r.get("stop", 9900.0), "tp_price": np.nan,
            "entry_reason": r.get("reason", "h1_breakout"), "exit_reason": r["exit"],
            "holding_min": float(r["hold"]), "equity_before": 1.0,
            "notional_usd": float(r.get("qty", 100)), "leverage": r["lev"], "risk_pct": 2.0,
            "size_capped": r["capped"], "gross_pnl_xbt": r["ret"], "gross_ret": r["ret"],
        })
    if not recs:
        return sc.empty_frame(sc.ROUNDTRIPS)
    df = pd.DataFrame(recs)
    df["exit_signal_ts"] = pd.to_datetime(df["exit_signal_ts"], utc=True)  # 전부 NaT 대비
    return df.astype(sc.ROUNDTRIPS.dtypes)


def R(side, exit, hold, lev, capped, ret, **kw):
    return dict(side=side, exit=exit, hold=hold, lev=lev, capped=capped, ret=ret, **kw)


# 손 계산 픽스처: 9건, 롱 6·숏 3, 손실(≤ 0) 연속 구간 [1..3]·[5..7] 둘 다 길이 3.
# 손절 3건의 예정 손실: #1 500×|1/10000−1/12500| = 1 %, #2 400×2e-5 = 0.8 %, #5 200×1.25e-5 = 0.25 %.
FIXTURE = [
    R("long", "take_profit", 10, 1.0, False, 0.02),
    R("short", "stop", 0, 2.0, False, -0.01, qty=500, entry=10000.0, stop=12500.0),
    R("long", "stop", 5, 4.0, True, -0.01, qty=400, entry=12500.0, stop=10000.0),
    R("short", "time", 60, 0.5, False, 0.0),
    R("long", "take_profit", 20, 1.5, False, 0.03),
    R("long", "stop", 3, 3.0, False, -0.005, qty=200, entry=20000.0, stop=16000.0),
    R("short", "end_of_data", 30, 4.0, True, -0.02),
    R("long", "time", 45, 2.5, False, -0.004),
    R("long", "take_profit", 15, 1.2, False, 0.01),
]


@pytest.fixture
def summary():
    return summarize_run(make_rt(FIXTURE), skipped_min_qty=4)


def test_identity_and_counts(summary):
    assert summary["strategy_id"] == SID and summary["param_id"] == PID
    assert summary["n_trades"] == 9 and summary["skipped_min_qty"] == 4


def test_reason_frequencies(summary):
    assert summary["entry_reason_count"] == {"h1_breakout": {"long": 6, "short": 3}}
    share = summary["entry_reason_share"]["h1_breakout"]
    assert share["long"] == approx(6 / 9) and share["short"] == approx(3 / 9)
    total = sum(v for sides in summary["entry_reason_share"].values() for v in sides.values())
    assert total == approx(1.0)

    assert summary["exit_reason_count"] == {
        "end_of_data": 1, "stop": 3, "take_profit": 3, "time": 2}
    es = summary["exit_reason_share"]
    assert es["stop"] == approx(1 / 3) and es["take_profit"] == approx(1 / 3)
    assert es["time"] == approx(2 / 9) and es["end_of_data"] == approx(1 / 9)
    assert sum(es.values()) == approx(1.0)


def test_holding_quantiles(summary):
    # 정렬 [0,3,5,10,15,20,30,45,60], 위치 = p/100 × 8
    assert summary["holding_min_p10"] == approx(2.4)   # 0 + 0.8 × 3
    assert summary["holding_min_p50"] == approx(15.0)
    assert summary["holding_min_p90"] == approx(48.0)  # 45 + 0.2 × 15
    assert summary["holding_min_mean"] == approx(188 / 9)
    assert summary["holding_min_max"] == approx(60.0)


def test_sizing_and_leverage(summary):
    # 정렬 [0.5,1.0,1.2,1.5,2.0,2.5,3.0,4.0,4.0], 합 19.7
    assert summary["leverage_p10"] == approx(0.9)      # 0.5 + 0.8 × 0.5
    assert summary["leverage_p50"] == approx(2.0)
    assert summary["leverage_p90"] == approx(4.0)
    assert summary["leverage_max"] == approx(4.0)
    assert summary["notional_pct_p10"] == approx(90.0)
    assert summary["notional_pct_p50"] == approx(200.0)
    assert summary["notional_pct_p90"] == approx(400.0)
    assert summary["notional_pct_mean"] == approx(1970 / 9)
    assert summary["size_capped_share"] == approx(2 / 9)


def test_win_payoff_expectancy_total(summary):
    assert summary["win_rate"] == approx(1 / 3)
    # 이긴 평균 0.06/3 = 0.02, 진 평균 −0.049/6 → 0.02 / (0.049/6) = 120/49
    assert summary["payoff_ratio"] == approx(120 / 49)
    assert summary["expectancy_ret"] == approx(1.1 / 9)  # 0.011/9 × 100
    # 1.02·0.99·0.99·1·1.03·0.995·0.98·0.996·1.01 − 1
    assert summary["total_ret"] == approx(1.003786287472376)


def test_stop_overshoot(summary):
    # #1 1 − 1 = 0, #2 1 − 0.8 = 0.2, #5 0.5 − 0.25 = 0.25
    assert summary["stop_overshoot_max"] == approx(0.25)
    assert summary["stop_overshoot_mean"] == approx(0.15)


def test_max_consec_losses_first_of_ties(summary):
    assert summary["max_consec_losses"] == 3
    assert summary["max_consec_loss_start"] == pd.Timestamp("2020-01-01T01:00:00Z")  # #1 entry
    assert summary["max_consec_loss_end"] == pd.Timestamp("2020-01-01T04:00:00Z")    # #3 exit
    assert summary["max_consec_loss_ret"] == approx(-1.99)  # 0.99·0.99·1 − 1


def test_keys_and_python_types(summary):
    assert tuple(summary) == SUMMARY_KEYS
    for k, v in summary.items():
        if k in ("strategy_id", "param_id"):
            assert type(v) is str
        elif k in ("n_trades", "skipped_min_qty", "max_consec_losses"):
            assert type(v) is int, k
        elif k.endswith("_count"):
            assert all(type(c) is int for c in _leaves(v)), k
        elif k.endswith("_share") and isinstance(v, dict):
            assert all(type(c) is float for c in _leaves(v)), k
        elif k in ("max_consec_loss_start", "max_consec_loss_end"):
            assert type(v) is pd.Timestamp and str(v.tz) == "UTC"
        else:
            assert type(v) is float, k


def _leaves(d):
    for v in d.values():
        yield from (_leaves(v) if isinstance(v, dict) else [v])


def test_doc_metric_keys_present():
    doc = {"n_trades", "skipped_min_qty", "entry_reason_count", "entry_reason_share",
           "exit_reason_count", "exit_reason_share", "size_capped_share", "win_rate",
           "payoff_ratio", "expectancy_ret", "max_consec_losses", "max_consec_loss_ret",
           "total_ret"}
    doc |= {f"holding_min_{s}" for s in ("p10", "p50", "p90", "mean", "max")}
    doc |= {f"notional_pct_{s}" for s in ("p10", "p50", "p90", "mean")}
    doc |= {f"leverage_{s}" for s in ("p10", "p50", "p90", "max")}
    doc |= {"stop_overshoot_max", "stop_overshoot_mean",
            "max_consec_loss_start", "max_consec_loss_end"}
    assert doc <= set(SUMMARY_KEYS)


# --- 경계 사례 ---------------------------------------------------------------

def test_zero_trades():
    s = summarize_run(sc.empty_frame(sc.ROUNDTRIPS), skipped_min_qty=7)
    assert tuple(s) == SUMMARY_KEYS
    assert s["n_trades"] == 0 and s["max_consec_losses"] == 0 and s["skipped_min_qty"] == 7
    for k in ("entry_reason_count", "entry_reason_share", "exit_reason_count", "exit_reason_share"):
        assert s[k] == {}
    rest = set(SUMMARY_KEYS) - {"n_trades", "max_consec_losses", "skipped_min_qty",
                                "entry_reason_count", "entry_reason_share",
                                "exit_reason_count", "exit_reason_share"}
    assert all(s[k] is None for k in rest)
    assert summarize_runs(sc.empty_frame(sc.ROUNDTRIPS)) == []


def test_all_wins():
    s = summarize_run(make_rt([R("long", "take_profit", 5, 1.0, False, 0.01),
                               R("short", "take_profit", 7, 2.0, False, 0.02)]))
    assert s["win_rate"] == 1.0 and s["payoff_ratio"] is None
    assert s["max_consec_losses"] == 0
    assert s["max_consec_loss_start"] is None and s["max_consec_loss_end"] is None
    assert s["max_consec_loss_ret"] is None
    assert s["stop_overshoot_max"] is None and s["stop_overshoot_mean"] is None
    assert s["total_ret"] == approx(3.02)  # 1.01·1.02 − 1
    assert s["skipped_min_qty"] is None


def test_all_losses_including_zero():
    s = summarize_run(make_rt([R("long", "stop", 1, 1.0, False, -0.01,
                                 qty=500, entry=10000.0, stop=12500.0),
                               R("short", "time", 60, 1.0, False, 0.0),
                               R("long", "end_of_data", 9, 1.0, False, -0.02)]))
    assert s["win_rate"] == 0.0 and s["payoff_ratio"] is None
    assert s["max_consec_losses"] == 3
    assert s["max_consec_loss_start"] == T0
    assert s["max_consec_loss_end"] == pd.Timestamp("2020-01-01T02:09:00Z")
    assert s["max_consec_loss_ret"] == approx(-2.98)  # 0.99·1·0.98 − 1
    assert s["total_ret"] == approx(-2.98)
    assert s["stop_overshoot_max"] == approx(0.0)


def test_payoff_none_when_losses_are_all_zero():
    s = summarize_run(make_rt([R("long", "take_profit", 5, 1.0, False, 0.01),
                               R("long", "time", 60, 1.0, False, 0.0)]))
    assert s["win_rate"] == 0.5 and s["payoff_ratio"] is None
    assert s["max_consec_losses"] == 1


def test_single_trade():
    s = summarize_run(make_rt([R("short", "stop", 4, 3.0, True, -0.01,
                                 qty=400, entry=12500.0, stop=10000.0)]))
    assert s["n_trades"] == 1
    assert s["holding_min_p10"] == s["holding_min_p90"] == s["holding_min_max"] == 4.0
    assert s["leverage_p50"] == 3.0 and s["notional_pct_p50"] == approx(300.0)
    assert s["size_capped_share"] == 1.0
    assert s["stop_overshoot_max"] == approx(0.2)
    assert s["max_consec_losses"] == 1 and s["max_consec_loss_ret"] == approx(-1.0)


# --- 입력 처리 ---------------------------------------------------------------

def test_shuffled_input_sorted_by_entry_ts(summary):
    rt = make_rt(FIXTURE)
    shuffled = rt.iloc[[4, 8, 0, 6, 2, 7, 1, 5, 3]].reset_index(drop=True)
    assert summarize_run(shuffled, skipped_min_qty=4) == summary


def test_mixed_runs_rejected_and_split():
    a = make_rt(FIXTURE[:3], pid="b")
    b = make_rt(FIXTURE[3:], pid="a")
    mixed = pd.concat([a, b], ignore_index=True)
    with pytest.raises(ValueError, match="run"):
        summarize_run(mixed)
    out = summarize_runs(mixed, skipped_min_qty={(SID, "a"): 2})
    assert [(o["strategy_id"], o["param_id"]) for o in out] == [(SID, "a"), (SID, "b")]
    assert [o["n_trades"] for o in out] == [6, 3]
    assert [o["skipped_min_qty"] for o in out] == [2, None]
    assert out[1]["max_consec_losses"] == 2 and out[0]["max_consec_losses"] == 3


def test_schema_violation():
    rt = make_rt(FIXTURE)
    rt["side"] = pd.array(["buy"] * len(rt), dtype="string")
    with pytest.raises(SchemaError, match="side"):
        summarize_run(rt)


def test_extra_columns_allowed(summary):
    rt = make_rt(FIXTURE).assign(net_ret=0.0)
    assert summarize_run(rt, skipped_min_qty=4) == summary
