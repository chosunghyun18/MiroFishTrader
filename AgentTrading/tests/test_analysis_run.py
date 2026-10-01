"""analysis.run — tmp 합성 parquet 로 CLI 끝까지, 결정성, 결측 일, OOS 거부, 그리드 검증."""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src.analysis import run as ar
from src.analysis.patterns import summarize_run
from src.analysis.synthetic import generate_run, param_grid
from src.ingest.normalize import bars_path, write_parquet_atomic
from src.ingest.store import load_bars
from src.shared.schema import BARS_1M, validate_roundtrips

SYM = "XBTUSD"
D1, D2 = date(2020, 3, 12), date(2020, 3, 13)
SPAN = "20200312_20200313"
ROOT = Path(__file__).resolve().parents[1]
SMALL_GRID = {"trigger": ["h1", "h2"], "n": [15], "k": [2], "stop_pct": [0.5], "tp_r": [2],
              "max_hold": [60], "risk_pct": [1]}


def _day_bars(day, close):
    n = len(close)
    rng = np.random.default_rng(day.toordinal())
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 3.0, (2, n)))
    df = pd.DataFrame({
        "ts": pd.date_range(pd.Timestamp(day, tz="UTC"), periods=n, freq="1min"),
        "symbol": SYM,
        "open": open_,
        "high": np.maximum(open_, close) + spread[0],
        "low": np.minimum(open_, close) - spread[1],
        "close": close,
        "volume": 10, "volume_xbt": 0.1, "trade_count": 1, "buy_volume": 5, "sell_volume": 5,
    })
    return df.astype(BARS_1M.dtypes)


def write_days(norm, days=(D1, D2), flat=False):
    rng = np.random.default_rng(7)
    close = np.full(1440 * len(days), 8000.0) if flat else \
        8000.0 * np.exp(np.cumsum(rng.normal(0, 0.0015, 1440 * len(days))))
    for i, d in enumerate(days):
        write_parquet_atomic(_day_bars(d, close[i * 1440:(i + 1) * 1440]), bars_path(norm, SYM, d))


@pytest.fixture
def norm(tmp_path):
    p = tmp_path / "norm"
    write_days(p)
    return p


@pytest.fixture
def grid_file(tmp_path):
    p = tmp_path / "grid.json"
    p.write_text(json.dumps(SMALL_GRID))
    return p


def _argv(norm, out, grid, start="2020-03-12", end="2020-03-13"):
    return ["--start", start, "--end", end, "--symbol", SYM, "--data-dir", str(norm),
            "--out", str(out), "--grid", str(grid)]


def test_cli_end_to_end(norm, grid_file, tmp_path):
    out = tmp_path / "out"
    assert ar.main(_argv(norm, out, grid_file)) == 0

    js = out / "distributions" / f"{SPAN}.json"
    md = out / "distributions" / f"{SPAN}.md"
    assert js.is_file() and md.is_file()
    _, params = ar.load_grid(grid_file)
    assert [p.strategy_id for p in params] == ["syn-v1-h1", "syn-v1-h2"]

    report = json.loads(js.read_text())
    assert report["meta"]["n_runs"] == 2 and report["meta"]["n_bars"] == 2880
    assert report["meta"]["symbol"] == SYM and report["meta"]["start"] == "2020-03-12"
    assert report["meta"]["first_ts"] == "2020-03-12T00:00:00+00:00"
    assert report["meta"]["grid"]["k"] == [2.0] and report["meta"]["grid"]["tp_r"] == [2.0]
    assert [r["param_id"] for r in report["runs"]] == [p.param_id for p in params]

    bars = load_bars(D1, D2, SYM, out_dir=norm)
    for p, got in zip(params, report["runs"]):
        rt_file = out / "roundtrips" / p.strategy_id / f"{SPAN}.parquet"
        rt = validate_roundtrips(pd.read_parquet(rt_file))
        res = generate_run(bars, p)
        pdt.assert_frame_equal(rt[rt["param_id"] == p.param_id].reset_index(drop=True),
                               res.roundtrips)
        want = summarize_run(res.roundtrips, res.skipped_min_qty)
        want["halted"] = res.halted
        assert got == json.loads(json.dumps(ar.to_jsonable(want)))
    assert report["runs"][0]["n_trades"] > 0  # h1
    assert "syn-v1-h1" in md.read_text() and "gross" in md.read_text()


def test_deterministic(norm, grid_file, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert ar.main(_argv(norm, a, grid_file)) == 0
    assert ar.main(_argv(norm, b, grid_file)) == 0
    for name in (f"{SPAN}.json", f"{SPAN}.md"):
        assert (a / "distributions" / name).read_bytes() == (b / "distributions" / name).read_bytes()
    for sid in ("syn-v1-h1", "syn-v1-h2"):
        rel = Path("roundtrips") / sid / f"{SPAN}.parquet"
        pdt.assert_frame_equal(pd.read_parquet(a / rel), pd.read_parquet(b / rel))


def test_missing_day_fails(tmp_path, grid_file):
    norm = tmp_path / "norm"
    write_days(norm, days=(D1,))
    out = tmp_path / "out"
    assert ar.main(_argv(norm, out, grid_file)) == 1
    assert not out.exists()


@pytest.mark.parametrize("start,end", [
    ("2021-12-31", "2022-01-01"),
    ("2022-03-01", "2022-03-02"),
    ("2018-02-28", "2018-03-02"),
    ("2020-03-13", "2020-03-12"),
])
def test_out_of_sample_rejected_before_load(tmp_path, grid_file, start, end):
    empty = tmp_path / "empty"  # 데이터가 없어도 로드 전에 거부(종료코드 2, 1 아님)
    with pytest.raises(SystemExit) as e:
        ar.main(_argv(empty, tmp_path / "out", grid_file, start, end))
    assert e.value.code == 2
    assert not (tmp_path / "out").exists()


def test_bad_date_format_exit_2(tmp_path, grid_file):
    with pytest.raises(SystemExit) as e:
        ar.main(_argv(tmp_path, tmp_path / "out", grid_file, start="2020/03/12"))
    assert e.value.code == 2


def test_check_sample_range_bounds():
    ar.check_sample_range(date(2018, 3, 1), date(2021, 12, 31))
    ar.check_sample_range(date(2020, 1, 1), date(2020, 1, 1))
    for s, e in [(date(2018, 2, 28), date(2019, 1, 1)), (date(2020, 1, 1), date(2022, 1, 1)),
                 (date(2020, 1, 2), date(2020, 1, 1))]:
        with pytest.raises(ValueError):
            ar.check_sample_range(s, e)


def test_bad_grid_exit_2(tmp_path):
    g = tmp_path / "g.json"
    g.write_text(json.dumps({"n": [30]}))
    with pytest.raises(SystemExit) as e:
        ar.main(_argv(tmp_path, tmp_path / "out", g))
    assert e.value.code == 2


@pytest.mark.parametrize("spec", [
    {"n": [30]}, {"trigger": ["h4"]}, {"nn": [15]}, {"n": []}, {"n": 15},
    {"tp_r": [True]}, {"n": ["15"]}, ["h1"],
])
def test_expand_grid_rejects(spec):
    with pytest.raises(ValueError):
        ar.expand_grid(spec)


def test_expand_grid_defaults_and_normalization():
    axes, params = ar.expand_grid({"trigger": ["h1"]})
    assert params == param_grid("h1") and len(params) == 324
    _, full = ar.load_grid(None)
    assert full == param_grid() and len(full) == 2268

    axes, params = ar.expand_grid({"trigger": ["h1"], "n": [15, 15.0], "k": [2], "stop_pct": [1],
                                   "tp_r": [None, 2], "max_hold": [60], "risk_pct": [1]})
    assert axes["n"] == [15] and axes["tp_r"] == [2.0, None] and axes["k"] == [2.0]
    assert [p.tp_r for p in params] == [2.0, None]  # param_id 정렬: tp_r=2 < tp_r=none
    assert all(p.k is None for p in params)  # h1 은 k 축을 무시


def test_zero_trade_runs_keep_ids_and_files(tmp_path, grid_file):
    norm = tmp_path / "norm"
    write_days(norm, flat=True)
    bars = load_bars(D1, D2, SYM, out_dir=norm)
    _, params = ar.load_grid(grid_file)
    rts, summaries = ar.run_grid(bars, params)
    assert sorted(rts) == ["syn-v1-h1", "syn-v1-h2"] and all(len(df) == 0 for df in rts.values())
    assert [(s["strategy_id"], s["param_id"]) for s in summaries] == \
        [(p.strategy_id, p.param_id) for p in params]
    assert all(s["n_trades"] == 0 and s["halted"] is False for s in summaries)

    out = tmp_path / "out"
    assert ar.main(_argv(norm, out, grid_file)) == 0
    for sid in rts:
        assert len(pd.read_parquet(out / "roundtrips" / sid / f"{SPAN}.parquet")) == 0
    report = json.loads((out / "distributions" / f"{SPAN}.json").read_text())
    assert report["runs"][0]["win_rate"] is None


def test_to_jsonable():
    got = ar.to_jsonable({"a": np.float64("nan"), "b": np.int64(3), "c": pd.Timestamp("2020-01-01", tz="UTC"),
                          "d": [np.bool_(True), float("inf")], "e": date(2020, 1, 2)})
    assert got == {"a": None, "b": 3, "c": "2020-01-01T00:00:00+00:00", "d": [True, None],
                   "e": "2020-01-02"}
    json.dumps(got, allow_nan=False)


def test_module_entrypoint_help():
    r = subprocess.run([sys.executable, "-m", "src.analysis.run", "--help"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "--grid" in r.stdout
