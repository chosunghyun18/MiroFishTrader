"""backtest.run.run_walkforward — 합성 폴드·합성 1분봉 로더로 실데이터 없이 워크포워드 엔진 검증."""

import math

import numpy as np
import pandas as pd
import pytest

from src.analysis.run import expand_grid, to_jsonable
from src.analysis.synthetic import Params
from src.backtest import run as br
from src.backtest.metrics import GATE_VERDICTS, SUMMARY_KEYS
from src.backtest.walkforward import (N_TRIALS, OOS_START, SAMPLE_START, Fold, deflated_sharpe, make_folds,
                                      make_selection, selection_sha256, sr_variance)
from src.shared.schema import BARS_1M

SYM = "XBTUSD"
T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
ORIGIN = T("2020-03-01")
SAMPLE = (T("2020-03-01"), T("2020-03-07"))
FOLDS = [
    Fold(T("2020-03-01"), T("2020-03-03"), T("2020-03-03"), T("2020-03-05")),
    Fold(T("2020-03-01"), T("2020-03-05"), T("2020-03-05"), T("2020-03-07")),
]
GRID = {"trigger": ["h1", "h2"], "n": [15], "k": [2], "stop_pct": [0.5], "tp_r": [2],
        "max_hold": [60], "risk_pct": [1, 2]}
LOOSE = {"min_trades": 1, "min_sharpe": 0.0, "max_drawdown": 1.0}
_, PARAMS = expand_grid(GRID)

_N_MIN = 1440 * 7
_CLOSE = 8000.0 * np.exp(np.cumsum(np.random.default_rng(11).normal(0, 0.0015, _N_MIN)))


def _bars(start, close):
    n = len(close)
    rng = np.random.default_rng(int(start.timestamp()) // 60)
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 3.0, (2, n)))
    df = pd.DataFrame({
        "ts": pd.date_range(start, periods=n, freq="1min"),
        "symbol": SYM,
        "open": open_,
        "high": np.maximum(open_, close) + spread[0],
        "low": np.minimum(open_, close) - spread[1],
        "close": close,
        "volume": 10, "volume_xbt": 0.1, "trade_count": 1, "buy_volume": 5, "sell_volume": 5,
    })
    return df.astype(BARS_1M.dtypes)


class Loader:
    """[start, end) 결정적 랜덤워크(같은 분은 같은 값). 호출 구간을 기록한다."""

    def __init__(self):
        self.calls = []

    def __call__(self, start, end):
        self.calls.append((start, end))
        i0 = int((start - ORIGIN) / pd.Timedelta(minutes=1))
        i1 = int((end - ORIGIN) / pd.Timedelta(minutes=1))
        return _bars(start, _CLOSE[i0:i1])


class FirstDaysLoader(Loader):
    """기본 표본 테스트용: 요청 구간의 첫 2일만 돌려준다(빠르게)."""

    def __call__(self, start, end):
        self.calls.append((start, end))
        rng = np.random.default_rng(int(start.timestamp()) // 86400)
        close = 8000.0 * np.exp(np.cumsum(rng.normal(0, 0.0015, 2880)))
        return _bars(start, close)


def _run(loader=None, gate=LOOSE, folds=FOLDS, sample=SAMPLE, params=PARAMS):
    return br.run_walkforward(folds, loader or Loader(), params, gate=gate, sample=sample)


@pytest.fixture(scope="module")
def report():
    loader = Loader()
    rep = _run(loader)
    return rep, loader


def test_report_keys_without_real_data(report):
    rep, _ = report
    assert set(rep) == {"gate", "n_trials", "sample", "n_runs", "folds", "stitched", "dsr", "verdict",
                        "bybit_verdict", "full_sample"}
    assert rep["n_trials"] == N_TRIALS == 756
    assert rep["sample"] == ["2020-03-01", "2020-03-07"]
    assert rep["n_runs"] == 4
    assert rep["gate"]["min_dsr"] == 0.95 and rep["gate"]["min_trades"] == 1
    assert len(rep["folds"]) == 2
    for f in rep["folds"]:
        assert f["selection"] is not None  # 합성 데이터 + 완화 게이트 → 선택이 나와야 테스트가 의미 있다
        assert set(f["selection"]) == {"strategy_id", "param_id", "train_sharpe", "train_n_trades", "train_mdd"}
        assert set(f["test"]) == {"default", "bybit"}
        for prof in ("default", "bybit"):
            assert tuple(f["test"][prof]) == SUMMARY_KEYS
            assert f["test"][prof]["param_id"] == f["selection"]["param_id"]
    for prof in ("default", "bybit"):
        st = rep["stitched"][prof]
        assert (st["strategy_id"], st["param_id"]) == ("walkforward", "stitched")
        assert (st["start"], st["end"]) == ("2020-03-03", "2020-03-07")
        assert st["n_trades"] == sum(f["test"][prof]["n_trades"] for f in rep["folds"])
        assert st["n_trades"] > 0
    assert set(rep["dsr"]) == {"var_sr", "n_var_runs", "n_var_finite", "sr0", "default", "bybit"}
    assert rep["verdict"] in GATE_VERDICTS and rep["bybit_verdict"] in GATE_VERDICTS
    assert rep["full_sample"]["selected"] is not None
    assert rep["full_sample"]["selection"] is None  # 기본 표본 전체가 아님


def test_train_never_sees_test_bars(report):
    rep, loader = report
    f1, f2 = FOLDS
    assert loader.calls == [(f1.train_start, f1.train_end), (f1.test_start, f1.test_end),
                            (f2.train_start, f2.train_end), (f2.test_start, f2.test_end), SAMPLE]
    for f in FOLDS:
        assert f.train_end <= f.test_start


def test_test_load_only_after_selection(monkeypatch):
    loader = Loader()
    seen = []
    orig = br.select_params

    def spy(summaries, gate=None):
        seen.append(len(loader.calls))
        return orig(summaries, gate)

    monkeypatch.setattr(br, "select_params", spy)
    _run(loader)
    # 폴드 k 선택 시점의 로더 호출 수: 폴드1 = 학습1 만, 폴드2 = 학습1·검증1·학습2
    assert seen[:2] == [1, 3]


def test_loader_leak_rejected():
    class Leaky(Loader):
        def __call__(self, start, end):
            bars = super().__call__(start, end + pd.Timedelta(minutes=5))
            return bars

    with pytest.raises(ValueError, match="밖 바"):
        _run(Leaky())


def test_empty_loader_rejected():
    with pytest.raises(ValueError, match="0행"):
        _run(lambda s, e: _bars(s, _CLOSE[:2])[:0])


def test_no_selection_means_cash():
    loader = Loader()
    rep = _run(loader, gate={**LOOSE, "min_trades": 10 ** 9})
    assert loader.calls == [(FOLDS[0].train_start, FOLDS[0].train_end),
                            (FOLDS[1].train_start, FOLDS[1].train_end), SAMPLE]
    for f in rep["folds"]:
        assert f["selection"] is None
        for prof in ("default", "bybit"):
            assert f["test"][prof]["n_trades"] == 0
            assert f["test"][prof]["total_net_ret"] == 0.0
            assert tuple(f["test"][prof]) == SUMMARY_KEYS
    assert rep["stitched"]["default"]["n_trades"] == 0
    assert rep["verdict"] == "insufficient" and rep["bybit_verdict"] == "insufficient"
    assert rep["full_sample"] == {"selected": None, "selection": None}


def test_mixed_folds(monkeypatch):
    n = {"calls": 0}
    orig = br.select_params

    def second_none(summaries, gate=None):
        n["calls"] += 1
        return None if n["calls"] == 2 else orig(summaries, gate)

    monkeypatch.setattr(br, "select_params", second_none)
    loader = Loader()
    rep = _run(loader)
    f1, f2 = rep["folds"]
    assert f1["selection"] is not None and f2["selection"] is None
    assert (FOLDS[1].test_start, FOLDS[1].test_end) not in loader.calls
    assert f1["test"]["default"]["n_trades"] > 0
    assert f2["test"]["default"]["n_trades"] == 0
    assert rep["stitched"]["default"]["n_trades"] == f1["test"]["default"]["n_trades"]


def _s(sid, pid, sr):
    return {"strategy_id": sid, "param_id": pid, "sr_daily": sr}


def test_representative_var_uses_risk1_only():
    ps = [Params(trigger="h1", n=n, stop_pct=0.5, tp_r=2.0, max_hold=60, risk_pct=r)
          for n in (15, 60, 240) for r in (1.0, 2.0)]
    vals = {15: 0.1, 60: float("nan"), 240: 0.3}
    sums = [_s(p.strategy_id, p.param_id, vals[p.n] if p.risk_pct == 1.0 else 50.0 * p.n) for p in ps]
    rv = br.representative_var(sums, ps)
    assert rv["n_runs"] == 3 and rv["n_finite"] == 2
    assert rv["var_sr"] == pytest.approx(np.var([0.1, 0.3], ddof=1))


def test_representative_var_without_risk1_is_nan():
    ps = [Params(trigger="h1", n=n, stop_pct=0.5, tp_r=2.0, max_hold=60, risk_pct=2.0) for n in (15, 60)]
    rv = br.representative_var([_s(p.strategy_id, p.param_id, 0.1 * p.n) for p in ps], ps)
    assert rv["n_runs"] == 0 and rv["n_finite"] == 0 and math.isnan(rv["var_sr"])


def test_dsr_matches_full_sample_risk1(report):
    rep, _ = report
    bars = Loader()(*SAMPLE)
    full = br.run_grid(bars, PARAMS, "default", SAMPLE[0], SAMPLE[1], rep["gate"], br._DiscardSink())
    risk1 = {(p.strategy_id, p.param_id) for p in PARAMS if p.risk_pct == 1.0}
    want_v = sr_variance([s for s in full if (s["strategy_id"], s["param_id"]) in risk1])
    rv = br.representative_var(full, PARAMS)
    assert rep["dsr"]["n_var_runs"] == 2
    for got, want in ((rep["dsr"]["var_sr"], want_v), (rv["var_sr"], want_v)):
        assert (math.isnan(got) and math.isnan(want)) or got == pytest.approx(want)
    for prof in ("default", "bybit"):
        st = rep["stitched"][prof]
        want = deflated_sharpe(st["sr_daily"], 756, rep["dsr"]["var_sr"], st["n_days"], st["skew_daily"],
                               st["kurt_daily"])
        got = rep["dsr"][prof]
        assert (math.isnan(got) and math.isnan(want)) or got == pytest.approx(want)


def test_full_sample_selection_on_default_sample():
    loader = FirstDaysLoader()
    folds = make_folds()
    rep = br.run_walkforward(folds, loader, PARAMS, gate=LOOSE)
    assert rep["sample"] == ["2018-03-01", "2022-01-01"]
    assert len(rep["folds"]) == 6
    assert loader.calls[-1] == (SAMPLE_START, OOS_START)
    sel = rep["full_sample"]["selected"]
    assert sel is not None
    assert rep["full_sample"]["selection"] == make_selection(sel, LOOSE)
    assert rep["full_sample"]["selection"]["sha256"] == selection_sha256(rep["full_sample"]["selection"])
    assert rep["stitched"]["default"]["start"] == "2019-03-01"
    assert rep["stitched"]["default"]["end"] == "2022-01-01"


def test_deterministic():
    a = to_jsonable(_run())
    b = to_jsonable(_run())
    assert a == b


@pytest.mark.parametrize("folds", [
    [FOLDS[0], Fold(T("2020-03-01"), T("2020-03-05"), T("2020-03-06"), T("2020-03-07"))],  # 빈틈
    [FOLDS[0], Fold(T("2020-03-01"), T("2020-03-04"), T("2020-03-04"), T("2020-03-07"))],  # 검증 겹침
    [Fold(T("2020-03-01"), T("2020-03-04"), T("2020-03-03"), T("2020-03-05"))],             # 학습·검증 겹침
    [Fold(T("2020-03-01"), T("2020-03-05"), T("2020-03-05"), T("2020-03-09"))],             # 표본 밖
    [Fold(T("2021-12-01"), T("2021-12-15"), T("2021-12-15"), T("2022-01-15"))],             # OOS
    [],
])
def test_bad_folds_rejected(folds):
    loader = Loader()
    with pytest.raises(ValueError):
        _run(loader, folds=folds)
    assert loader.calls == []
