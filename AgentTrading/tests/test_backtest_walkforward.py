"""backtest.walkforward — 설계 폴드 표, 표본/OOS 가드, 학습 전용 선택, DSR 기대값, 선택 파일·접근 로그.

기대값은 phase3-backtest.md "워크포워드" 의 리터럴이다.
"""

import inspect
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src.backtest import walkforward as wf
from src.backtest.metrics import summarize_net_run
from src.backtest.walkforward import (
    Fold,
    authorize_oos,
    check_oos_range,
    check_sample_range,
    deflated_sharpe,
    evaluate_oos,
    expected_max_sr,
    make_folds,
    make_selection,
    resolve_gate,
    select_params,
    selection_sha256,
    sr_variance,
    stitch_test_roundtrips,
    walkforward_verdict,
)
from tests.test_backtest_metrics import make_net

ROOT = Path(__file__).resolve().parents[1]


def utc(s):
    return pd.Timestamp(s, tz="UTC")


DOC_FOLDS = [
    ("2018-03-01", "2019-03-01", "2019-03-01", "2019-09-01", 184),
    ("2018-03-01", "2019-09-01", "2019-09-01", "2020-03-01", 182),
    ("2018-03-01", "2020-03-01", "2020-03-01", "2020-09-01", 184),
    ("2018-03-01", "2020-09-01", "2020-09-01", "2021-03-01", 181),
    ("2018-03-01", "2021-03-01", "2021-03-01", "2021-09-01", 184),
    ("2018-03-01", "2021-09-01", "2021-09-01", "2022-01-01", 122),
]


def check_invariants(folds, start, end):
    assert folds
    for f in folds:
        assert f.train_start == utc(start)                       # 앵커드
        assert f.train_start < f.train_end == f.test_start < f.test_end   # 비겹침·검증이 뒤
        assert utc(start) <= f.train_start and f.test_end <= utc(end)
    for a, b in zip(folds, folds[1:]):
        assert a.test_end == b.test_start                        # 빈틈·겹침 없음
    assert folds[-1].test_end == utc(end)


# 1. 폴드 ------------------------------------------------------------------------------------------

def test_default_folds_match_doc_table():
    folds = make_folds()
    assert len(folds) == 6
    for f, (ts, te, vs, ve, days) in zip(folds, DOC_FOLDS):
        assert f == Fold(utc(ts), utc(te), utc(vs), utc(ve))
        assert (f.test_end - f.test_start).days == days
    assert sum((f.test_end - f.test_start).days for f in folds) == 1037
    check_invariants(folds, "2018-03-01", "2022-01-01")


@pytest.mark.parametrize("args", [(6, 3, 3), (12, 6, 6), (18, 3, 3), (24, 12, 12), (12, 3, 6)])
def test_fold_invariants_other_args(args):
    tr, te, st = args
    folds = make_folds(train_months=tr, test_months=te, step_months=st)
    for f in folds:
        assert f.train_start < f.train_end == f.test_start < f.test_end
        assert f.test_end <= utc("2022-01-01")
    if te == st:
        check_invariants(folds, "2018-03-01", "2022-01-01")


def test_fold_bad_args():
    for kw in ({"train_months": 0}, {"test_months": -1}, {"step_months": 0}, {"train_months": 1.5}):
        with pytest.raises(ValueError):
            make_folds(**kw)
    with pytest.raises(ValueError, match="검증 구간"):
        make_folds("2018-03-01", "2019-01-01", train_months=12)


# 2. 표본 경계 --------------------------------------------------------------------------------------

def test_no_fold_outside_sample():
    for args in [(12, 6, 6), (6, 3, 3), (3, 1, 1)]:
        for f in make_folds(train_months=args[0], test_months=args[1], step_months=args[2]):
            for t in (f.train_start, f.train_end, f.test_start, f.test_end):
                assert utc("2018-03-01") <= t <= utc("2022-01-01")
            assert f.test_end <= utc("2022-01-01")       # 반열린: 2022-01-01 미포함


@pytest.mark.parametrize("start,end", [("2018-03-01", "2022-02-01"), ("2018-02-01", "2022-01-01"),
                                       ("2018-03-01", "2025-06-01")])
def test_make_folds_rejects_outside_sample(start, end):
    with pytest.raises(ValueError):
        make_folds(start, end)


# 3. OOS 가드 ---------------------------------------------------------------------------------------

def test_check_sample_range():
    assert check_sample_range("2018-03-01", "2022-01-01") == (utc("2018-03-01"), utc("2022-01-01"))
    with pytest.raises(ValueError, match="OOS"):
        check_sample_range("2021-06-01", "2022-01-02")
    with pytest.raises(ValueError, match="OOS"):
        check_sample_range("2022-01-01", "2022-06-01")
    with pytest.raises(ValueError, match="2025-01-01"):
        check_sample_range("2024-06-01", "2025-02-01")
    with pytest.raises(ValueError):
        check_sample_range("2018-02-28", "2019-01-01")
    with pytest.raises(ValueError):
        check_sample_range("2019-01-01", "2019-01-01")
    with pytest.raises(ValueError, match="자정"):
        check_sample_range("2019-01-01T01:00", "2019-02-01")


def test_check_oos_range():
    assert check_oos_range("2022-01-01", "2025-01-01") == (utc("2022-01-01"), utc("2025-01-01"))
    for s, e in [("2021-12-01", "2022-06-01"), ("2019-01-01", "2020-01-01"), ("2024-06-01", "2025-01-02")]:
        with pytest.raises(ValueError):
            check_oos_range(s, e)


def summary_full(strategy="syn-v1-h1", param="p7", **kw):
    s = {"strategy_id": strategy, "param_id": param, "start": "2018-03-01", "end": "2022-01-01",
         "n_trades": 300, "sharpe": 1.5, "mdd": 0.2}
    s.update(kw)
    return s


def oos_rt(strategy="syn-v1-h1", param="p7", n=3):
    d0 = utc("2022-02-01")
    return make_net([{"entry_ts": d0 + pd.Timedelta(days=i, hours=1),
                      "exit_ts": d0 + pd.Timedelta(days=i, hours=2), "net_ret": 0.01}
                     for i in range(n)], strategy=strategy, param=param)


def test_selection_sha_deterministic_and_gate_normalized():
    a = make_selection(summary_full(), {"min_sharpe": 1})
    b = make_selection(summary_full(), {"min_sharpe": 1.0, "max_drawdown": 0.3, "extra": 9})
    assert a == b and a["sha256"] == selection_sha256(a)
    assert a["gate"] == {"min_trades": 100, "min_sharpe": 1.0, "max_drawdown": 0.3, "min_dsr": 0.95}
    assert a["selected_on"] == ["2018-03-01", "2022-01-01"] and a["fee_profile"] == "default"
    assert make_selection(summary_full(param="p8"))["sha256"] != a["sha256"]
    with pytest.raises(ValueError, match="기본 표본 전체"):
        make_selection(summary_full(end="2021-09-01"))


def test_authorize_oos(tmp_path):
    log = tmp_path / "oos_access.jsonl"
    sel = make_selection(summary_full())
    assert authorize_oos(sel, log) == (utc("2022-01-01"), utc("2025-01-01"))
    assert not log.exists()                                    # 쓰기 없음
    bad = dict(sel, param_id="p8")                             # sha256 그대로 → 변조
    with pytest.raises(ValueError, match="sha256"):
        authorize_oos(bad, log)
    with pytest.raises(ValueError, match="키"):
        authorize_oos({k: v for k, v in sel.items() if k != "gate"}, log)
    # 다른 sha256 이 로그에 있으면 거부, 같은 sha256 은 허용
    log.write_text(json.dumps({"sha256": sel["sha256"]}) + "\n")
    authorize_oos(sel, log)
    other = make_selection(summary_full(param="p8"))
    with pytest.raises(PermissionError):
        authorize_oos(other, log)
    log.write_text("not json\n")
    with pytest.raises(PermissionError):
        authorize_oos(sel, log)


def test_evaluate_oos_logs_and_guards(tmp_path):
    log = tmp_path / "sub" / "oos_access.jsonl"
    sel = make_selection(summary_full())
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    s = evaluate_oos(sel, oos_rt(), log, now=now)
    assert (s["start"], s["end"], s["n_days"]) == ("2022-01-01", "2025-01-01", 1096)
    assert s["n_trades"] == 3 and s["gate"] == "insufficient"
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"ts": now.isoformat(), "sha256": sel["sha256"],
                                    "strategy_id": "syn-v1-h1", "param_id": "p7", "gate": "insufficient"}
    # 같은 선택 재실행 허용, 한 줄 더
    evaluate_oos(sel, oos_rt(), log, now=now)
    assert len(log.read_text().splitlines()) == 2
    # 선택과 다른 run 입력 거부(로그 미기록)
    with pytest.raises(ValueError, match="선택"):
        evaluate_oos(sel, oos_rt(param="p8"), log, now=now)
    assert len(log.read_text().splitlines()) == 2
    # 다른 선택은 거부
    with pytest.raises(PermissionError):
        evaluate_oos(make_selection(summary_full(param="p8")), oos_rt(param="p8"), log, now=now)
    # 0건 입력: run 키는 선택값
    z = evaluate_oos(sel, oos_rt(n=0), log, now=now)
    assert (z["strategy_id"], z["param_id"], z["n_trades"], z["gate"]) == ("syn-v1-h1", "p7", 0, "insufficient")


def test_evaluate_oos_rejects_sample_window_trades(tmp_path):
    sel = make_selection(summary_full())
    d0 = utc("2021-12-01")
    rt = make_net([{"entry_ts": d0, "exit_ts": d0 + pd.Timedelta(hours=1), "net_ret": 0.0}],
                  strategy="syn-v1-h1", param="p7")
    with pytest.raises(ValueError):
        evaluate_oos(sel, rt, tmp_path / "log.jsonl")


def test_default_log_path_constant():
    assert wf.OOS_LOG_PATH == Path("data/backtest/oos_access.jsonl")
    assert not any(n.startswith(("clear", "reset", "delete")) for n in dir(wf))


# 4. 선택 함수 --------------------------------------------------------------------------------------

def s_(sid, pid, sharpe, n=150, mdd=0.1, **kw):
    d = {"strategy_id": sid, "param_id": pid, "n_trades": n, "sharpe": sharpe, "mdd": mdd}
    d.update(kw)
    return d


def test_select_params_rules():
    cands = [
        s_("a", "p1", 5.0, n=99),             # 거래 수 미달
        s_("a", "p2", 4.0, mdd=0.31),         # MDD 초과
        s_("a", "p3", float("nan")),          # NaN sharpe
        s_("b", "p2", 2.0),
        s_("a", "p9", 2.0),                   # 동률 → (a, p9) 사전순 먼저
        s_("c", "p1", 1.0),
    ]
    assert select_params(cands)["param_id"] == "p9"
    assert select_params(cands)["strategy_id"] == "a"
    for perm in (cands[::-1], cands[2:] + cands[:2]):
        assert select_params(perm) == select_params(cands)
    assert select_params(cands, {"min_trades": 90})["param_id"] == "p1"
    assert select_params(cands[:3]) is None
    assert select_params([]) is None
    # 연환산 Sharpe 가 1 미만이어도 후보(선택은 min_sharpe 를 보지 않음)
    assert select_params([s_("a", "p1", 0.5)])["param_id"] == "p1"


def test_select_params_uses_train_only():
    params = list(inspect.signature(select_params).parameters)
    assert params == ["train_summaries", "gate"]
    train = [s_("a", "p1", 1.2), s_("a", "p2", 2.5), s_("b", "p1", 2.0)]
    # 검증 지표 키를 섞어도 선택 불변
    noisy = [dict(t, test_sharpe=10.0 - i, test_mdd=0.9) for i, t in enumerate(train)]
    assert select_params(noisy)["param_id"] == select_params(train)["param_id"] == "p2"

    # 워크포워드 경로: 학습 요약으로 고른 뒤 검증 roundtrips 값을 바꿔도 선택 동일
    def run_fold(test_rets):
        chosen = select_params(train)
        d0 = utc("2019-03-01")
        test_rt = make_net([{"entry_ts": d0 + pd.Timedelta(days=i, hours=1),
                             "exit_ts": d0 + pd.Timedelta(days=i, hours=2), "net_ret": r}
                            for i, r in enumerate(test_rets)],
                           strategy=chosen["strategy_id"], param=chosen["param_id"])
        st = stitch_test_roundtrips([test_rt])
        return chosen, summarize_net_run(st, "2019-03-01", "2019-09-01")

    c1, v1 = run_fold([0.05, 0.03, 0.04])
    c2, v2 = run_fold([-0.2, -0.1, -0.3])
    assert c1 == c2
    assert v1["total_net_ret"] != v2["total_net_ret"]


# 검증 곡선 이어 붙이기 ------------------------------------------------------------------------------

def test_stitch_renumbers_colliding_trade_ids():
    d1, d2 = utc("2019-03-01"), utc("2019-09-01")
    f1 = make_net([{"entry_ts": d1 + pd.Timedelta(days=1), "exit_ts": d1 + pd.Timedelta(days=1, hours=1),
                    "net_ret": 0.1, "trade_id": 0},
                   {"entry_ts": d1, "exit_ts": d1 + pd.Timedelta(hours=1), "net_ret": 0.2, "trade_id": 1}],
                  strategy="a", param="p1")
    f2 = make_net([{"entry_ts": d2, "exit_ts": d2 + pd.Timedelta(hours=1), "net_ret": -0.1, "trade_id": 0}],
                  strategy="b", param="p2")
    before = f1.copy()
    st = stitch_test_roundtrips([f1, None, f2.iloc[0:0], f2])
    pd.testing.assert_frame_equal(f1, before)                  # 입력 불변
    assert list(st["trade_id"]) == [0, 1, 2]
    assert list(st["net_ret"]) == [0.2, 0.1, -0.1]             # 폴드 순 → entry_ts 순
    assert set(st["strategy_id"]) == {"walkforward"} and set(st["param_id"]) == {"stitched"}
    s = summarize_net_run(st, "2019-03-01", "2022-01-01")
    assert s["n_trades"] == 3 and s["n_days"] == 1037
    assert s["total_net_ret"] == pytest.approx(1.2 * 1.1 * 0.9 - 1)
    assert len(stitch_test_roundtrips([None, None])) == 0


# 5. DSR ------------------------------------------------------------------------------------------

def test_dsr_doc_example():
    from statistics import NormalDist
    nd = NormalDist()
    assert nd.inv_cdf(1 - 1 / 756) == pytest.approx(3.006182388364, abs=1e-9)
    assert nd.inv_cdf(1 - 1 / (756 * math.e)) == pytest.approx(3.298154398507, abs=1e-9)
    assert expected_max_sr(756, 0.0025) == pytest.approx(0.158735660317, abs=1e-9)
    for sr, want in [(0.15, 0.394461594218), (0.08, 0.006651661982), (0.30, 0.999974023203)]:
        assert deflated_sharpe(sr, 756, 0.0025, 1037, -0.5, 6.0) == pytest.approx(want, abs=1e-9)
    assert wf.N_TRIALS == 756 and wf.EULER_GAMMA == 0.5772156649


def test_dsr_nan_edges():
    ok = dict(sr=0.15, n_trials=756, var_sr=0.0025, t=1037, skew=-0.5, kurt=6.0)
    for k, v in [("t", 1), ("var_sr", float("nan")), ("var_sr", -0.1), ("n_trials", 1),
                 ("sr", float("inf")), ("skew", float("nan"))]:
        assert math.isnan(deflated_sharpe(**dict(ok, **{k: v})))
    # 분모 안 1 − g3·SR + (g4 − 1)/4·SR² ≤ 0
    assert math.isnan(deflated_sharpe(1.0, 756, 0.0025, 1037, 2.0, 1.0))


def test_sr_variance():
    xs = [{"sr_daily": v} for v in (0.1, 0.2, 0.3, float("nan"))]
    assert sr_variance(xs) == pytest.approx(0.01)
    assert math.isnan(sr_variance(xs[:1] + xs[3:]))


def test_walkforward_verdict():
    good = {"n_trades": 150, "sharpe": 2.0, "mdd": 0.1}
    assert walkforward_verdict(good, 0.96) == "pass"
    assert walkforward_verdict(good, 0.95) == "pass"
    assert walkforward_verdict(good, 0.94) == "fail"
    assert walkforward_verdict(good, float("nan")) == "fail"
    assert walkforward_verdict(good, 0.5, {"min_dsr": 0.4}) == "pass"
    assert walkforward_verdict(dict(good, sharpe=0.5), 0.99) == "fail"
    assert walkforward_verdict(dict(good, n_trades=10), 0.99) == "insufficient"
    with pytest.raises(ValueError):
        resolve_gate({"min_dsr": "x"})


# 6. config ---------------------------------------------------------------------------------------

def test_config_example_has_min_dsr():
    cfg = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text())
    assert cfg["backtest"]["gate"]["min_dsr"] == 0.95
    assert resolve_gate(cfg["backtest"]["gate"])["min_dsr"] == 0.95
