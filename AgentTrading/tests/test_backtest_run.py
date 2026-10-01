"""backtest.run — tmp 합성 parquet 로 CLI 끝까지, 결정성, 수수료 프로필, 결측 일, 구간 거부, 0건 run."""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src.analysis.run import load_grid, to_jsonable
from src.analysis.synthetic import generate_run
from src.backtest import run as br
from src.backtest.costs import PROFILES, apply_costs
from src.backtest.metrics import GATE_VERDICTS, SUMMARY_KEYS, summarize_net_run
from src.backtest.walkforward import resolve_gate
from src.ingest.normalize import bars_path, write_parquet_atomic
from src.ingest.store import load_bars
from src.shared.schema import BARS_1M, ROUNDTRIPS_NET, validate_roundtrips_net

SYM = "XBTUSD"
D1, D2 = date(2020, 3, 12), date(2020, 3, 13)
START, END = "2020-03-12", "2020-03-14"  # 반열린: D1·D2 이틀
SPAN = "20200312_20200314"
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


def _argv(norm, out, grid, start=START, end=END, fee=None):
    argv = ["--start", start, "--end", end, "--symbol", SYM, "--data-dir", str(norm),
            "--out", str(out), "--grid", str(grid)]
    return argv + ["--fee-profile", fee] if fee else argv


def _report(out, fee="default"):
    return json.loads((out / "summary" / fee / f"{SPAN}.json").read_text())


def test_cli_end_to_end(norm, grid_file, tmp_path):
    out = tmp_path / "out"
    assert br.main(_argv(norm, out, grid_file)) == 0

    js = out / "summary" / "default" / f"{SPAN}.json"
    md = out / "summary" / "default" / f"{SPAN}.md"
    assert js.is_file() and md.is_file()
    _, params = load_grid(grid_file)
    assert [p.strategy_id for p in params] == ["syn-v1-h1", "syn-v1-h2"]

    report = json.loads(js.read_text())
    m = report["meta"]
    assert m["start"] == START and m["end"] == END and m["interval"] == f"[{START}, {END})"
    assert m["symbol"] == SYM and m["n_runs"] == 2 and m["n_bars"] == 2880
    assert m["first_ts"] == "2020-03-12T00:00:00+00:00" and m["last_ts"] == "2020-03-13T23:59:00+00:00"
    assert m["grid"]["k"] == [2.0] and m["grid"]["trigger"] == ["h1", "h2"]
    assert m["fee_profile"] == "default" and m["fee"] == PROFILES["default"]
    assert set(m["gate"]) == {"min_trades", "min_sharpe", "max_drawdown", "min_dsr"}
    assert m["gate"] == resolve_gate(None)
    assert m["n_pass"] + m["n_fail"] + m["n_insufficient"] == 2

    runs = report["runs"]
    assert [(r["strategy_id"], r["param_id"]) for r in runs] == [(p.strategy_id, p.param_id) for p in params]
    bars = load_bars(D1, D2, SYM, out_dir=norm)
    for p, got in zip(params, runs):
        assert set(got) == set(SUMMARY_KEYS)
        assert len(got) == 15 and got["gate"] in GATE_VERDICTS
        net = apply_costs(generate_run(bars, p).roundtrips, "default")
        want = summarize_net_run(net, START, END, resolve_gate(None))
        want["strategy_id"], want["param_id"] = p.strategy_id, p.param_id
        assert got == json.loads(json.dumps(to_jsonable(want)))
        rt = validate_roundtrips_net(pd.read_parquet(out / "roundtrips_net" / "default" / p.strategy_id
                                                     / f"{SPAN}.parquet"))
        pdt.assert_frame_equal(rt[rt["param_id"] == p.param_id].reset_index(drop=True), net)
    assert runs[0]["n_trades"] > 0 and runs[0]["end"] == END  # h1

    text = md.read_text()
    assert "| strategy_id | param_id | n_trades | sharpe | mdd | total_net_ret | total_gross_ret" in text
    assert all(r["param_id"] in text for r in runs)
    assert f"통과 run {m['n_pass']}/2" in text and "최종 판정 아님" in text


def test_deterministic(norm, grid_file, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert br.main(_argv(norm, a, grid_file)) == 0
    assert br.main(_argv(norm, b, grid_file)) == 0
    for name in (f"{SPAN}.json", f"{SPAN}.md"):
        rel = Path("summary") / "default" / name
        assert (a / rel).read_bytes() == (b / rel).read_bytes()
    for sid in ("syn-v1-h1", "syn-v1-h2"):
        rel = Path("roundtrips_net") / "default" / sid / f"{SPAN}.parquet"
        pdt.assert_frame_equal(pd.read_parquet(a / rel), pd.read_parquet(b / rel))


def test_fee_profile_bybit(norm, grid_file, tmp_path):
    out = tmp_path / "out"
    assert br.main(_argv(norm, out, grid_file)) == 0
    assert br.main(_argv(norm, out, grid_file, fee="bybit")) == 0
    d, b = _report(out, "default"), _report(out, "bybit")
    assert b["meta"]["fee_profile"] == "bybit" and b["meta"]["fee"] == PROFILES["bybit"]
    assert (out / "roundtrips_net" / "bybit" / "syn-v1-h1" / f"{SPAN}.parquet").is_file()
    rd, rb = d["runs"][0], b["runs"][0]  # h1: 거래 있음, 진입은 항상 taker → bybit 가 엄격히 더 비싸다
    assert rd["param_id"] == rb["param_id"] and rd["n_trades"] == rb["n_trades"] > 0
    assert rb["total_net_ret"] < rd["total_net_ret"]
    assert rb["total_gross_ret"] == rd["total_gross_ret"]


def test_missing_day_fails(tmp_path, grid_file):
    norm = tmp_path / "norm"
    write_days(norm, days=(D1,))
    out = tmp_path / "out"
    assert br.main(_argv(norm, out, grid_file)) == 1
    assert not out.exists()  # summary·roundtrips_net 모두 미작성


def test_end_is_exclusive(tmp_path, grid_file):
    norm = tmp_path / "norm"
    write_days(norm, days=(D1,))  # D1 하루만 있어도 [D1, D2) 는 성공
    out = tmp_path / "out"
    assert br.main(_argv(norm, out, grid_file, START, "2020-03-13")) == 0
    rep = json.loads((out / "summary" / "default" / "20200312_20200313.json").read_text())
    assert rep["meta"]["n_bars"] == 1440 and rep["runs"][0]["n_days"] == 1


@pytest.mark.parametrize("start,end", [
    ("2018-02-28", "2018-03-02"),   # 표본 이전
    ("2021-12-31", "2022-01-02"),   # end 가 OOS 로 넘어감
    ("2022-03-01", "2022-03-02"),   # OOS 안
    ("2025-01-01", "2025-01-02"),   # 2025 이후
    ("2020-03-13", "2020-03-12"),   # start > end
    ("2020-03-12", "2020-03-12"),   # start == end (빈 구간)
])
def test_out_of_range_rejected_before_load(tmp_path, grid_file, start, end):
    empty = tmp_path / "empty"  # 데이터가 없어도 로드 전에 거부(종료코드 2, 1 아님)
    with pytest.raises(SystemExit) as e:
        br.main(_argv(empty, tmp_path / "out", grid_file, start, end))
    assert e.value.code == 2
    assert not (tmp_path / "out").exists()


def test_sample_end_boundary_allowed(tmp_path, grid_file):
    # --end 2022-01-01 은 반열린 경계라 구간 검사를 통과 → 데이터 없음으로 종료코드 1
    assert br.main(_argv(tmp_path / "empty", tmp_path / "out", grid_file, "2021-12-31", "2022-01-01")) == 1


def test_bad_grid_exit_2(tmp_path):
    g = tmp_path / "g.json"
    g.write_text(json.dumps({"n": [30]}))
    with pytest.raises(SystemExit) as e:
        br.main(_argv(tmp_path, tmp_path / "out", g))
    assert e.value.code == 2


def test_bad_fee_profile_exit_2(tmp_path, grid_file):
    with pytest.raises(SystemExit) as e:
        br.main(_argv(tmp_path, tmp_path / "out", grid_file, fee="binance"))
    assert e.value.code == 2


def test_zero_trade_runs_keep_ids(tmp_path, grid_file):
    norm = tmp_path / "norm"
    write_days(norm, flat=True)
    out = tmp_path / "out"
    assert br.main(_argv(norm, out, grid_file)) == 0
    _, params = load_grid(grid_file)
    runs = _report(out)["runs"]
    assert [(r["strategy_id"], r["param_id"]) for r in runs] == [(p.strategy_id, p.param_id) for p in params]
    assert all(r["n_trades"] == 0 and r["gate"] == "insufficient" and r["sharpe"] is None for r in runs)
    for sid in ("syn-v1-h1", "syn-v1-h2"):
        df = pd.read_parquet(out / "roundtrips_net" / "default" / sid / f"{SPAN}.parquet")
        assert len(df) == 0 and list(df.columns) == ROUNDTRIPS_NET.column_names
    assert _report(out)["meta"]["n_insufficient"] == 2


def test_module_entrypoint_help():
    r = subprocess.run([sys.executable, "-m", "src.backtest.run", "--help"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "--fee-profile" in r.stdout and "미포함" in r.stdout
