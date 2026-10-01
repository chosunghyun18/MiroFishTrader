"""backtest.run `--jobs N` — 병렬(spawn 프로세스 풀) 산출물이 순차와 바이트 동일, 윈도우 상한, 인자 거부, 워커 예외 전파.

워커는 spawn 이라 테스트의 monkeypatch 가 보이지 않는다. 주입(로더·폴드·게이트)은 메인 프로세스 쪽에만 건다.
"""

import json
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd
import pandas.testing as pdt
import pytest

from src.analysis.run import load_grid
from src.backtest import run as br
from src.ingest.store import load_bars
from tests.test_backtest_run import D1, D2, SPAN, SYM, write_days
from tests.test_backtest_wfcli import FakeLoadBars, FoldsSpy, _loose_gate

# 트리거 2개 × 4 = 8 run > WINDOW_PER_JOB × 2 → 윈도우 재충전·트리거별 싱크 파일 분기가 실제로 돈다
GRID = {"trigger": ["h1", "h2"], "n": [15], "k": [2], "stop_pct": [0.5], "tp_r": [2],
        "max_hold": [60, 240], "risk_pct": [1, 2]}
START, END = "2020-03-12", "2020-03-14"
END_D = date(2020, 3, 14)


@pytest.fixture
def norm(tmp_path):
    p = tmp_path / "norm"
    write_days(p)
    return p


@pytest.fixture
def grid_file(tmp_path):
    p = tmp_path / "grid.json"
    p.write_text(json.dumps(GRID))
    return p


def _files(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _base_argv(norm, out, grid, jobs):
    return ["--start", START, "--end", END, "--symbol", SYM, "--data-dir", str(norm), "--out", str(out),
            "--grid", str(grid), "--jobs", str(jobs)]


def test_grid_size_exercises_window():
    n = 1
    for v in GRID.values():
        n *= len(v)
    assert n > br.WINDOW_PER_JOB * 2 and len(GRID["trigger"]) >= 2


def test_default_mode_jobs1_vs_jobs2_identical(norm, grid_file, tmp_path):
    o1, o2 = tmp_path / "o1", tmp_path / "o2"
    assert br.main(_base_argv(norm, o1, grid_file, 1)) == 0
    assert br.main(_base_argv(norm, o2, grid_file, 2)) == 0

    files = _files(o1)
    assert files == _files(o2)
    assert Path("summary/default") / f"{SPAN}.json" in files and Path("summary/default") / f"{SPAN}.md" in files
    pq_files = [f for f in files if f.suffix == ".parquet"]
    assert len(pq_files) == 2  # 트리거별 1개
    for f in files:
        if f.suffix == ".parquet":
            pdt.assert_frame_equal(pd.read_parquet(o1 / f), pd.read_parquet(o2 / f))
        assert (o1 / f).read_bytes() == (o2 / f).read_bytes(), f
    report = json.loads((o1 / "summary" / "default" / f"{SPAN}.json").read_text())
    assert report["meta"]["n_runs"] == 8
    assert any(r["n_trades"] > 0 for r in report["runs"])  # 동일성 비교가 공허하지 않다
    assert not list(o2.rglob("*.tmp"))


def test_walkforward_jobs1_vs_jobs2_identical(monkeypatch, grid_file, tmp_path):
    monkeypatch.setattr(br, "load_bars", FakeLoadBars())
    monkeypatch.setattr(br, "make_folds", FoldsSpy())
    monkeypatch.setattr(br, "resolve_gate", _loose_gate)
    outs = []
    for jobs in (1, 2):
        out = tmp_path / f"wf{jobs}"
        assert br.main(["--walkforward", "--grid", str(grid_file), "--data-dir", "norm", "--out", str(out),
                        "--jobs", str(jobs)]) == 0
        outs.append(out / "walkforward")
    for name in ("default.json", "default.md", "selection.json"):
        assert (outs[0] / name).read_bytes() == (outs[1] / name).read_bytes(), name
    rep = json.loads((outs[0] / "default.json").read_text())
    assert any(f["selection"] is not None and f["test"]["default"]["n_trades"] > 0 for f in rep["folds"])


class _ListSink:
    def __init__(self, on_write=None):
        self.calls = []
        self.on_write = on_write

    def write(self, sid, df):
        assert isinstance(df, pd.DataFrame)
        self.calls.append((sid, df["param_id"].unique().tolist(), len(df)))
        if self.on_write:
            self.on_write()


class _CountingPool(ProcessPoolExecutor):
    submitted = 0

    def submit(self, *a, **k):
        type(self).submitted += 1
        return super().submit(*a, **k)


def test_run_grid_order_and_window(norm, grid_file, monkeypatch):
    _, params = load_grid(grid_file)
    bars = load_bars(D1, D2, SYM, out_dir=norm)
    seq_sink = _ListSink()
    seq = br.run_grid(bars, params, "default", D1, END_D, None, seq_sink)

    monkeypatch.setattr(br, "ProcessPoolExecutor", _CountingPool)
    _CountingPool.submitted = 0
    outstanding = []
    par_sink = _ListSink(on_write=lambda: outstanding.append(_CountingPool.submitted - len(par_sink.calls)))
    shuffled = list(reversed(params))
    par = br.run_grid(bars, shuffled, "default", D1, END_D, None, par_sink, jobs=2)

    assert par == seq  # 섞인 입력에도 정렬 순서·같은 값
    assert [c[0] for c in par_sink.calls] == [c[0] for c in seq_sink.calls]
    assert [c[2] for c in par_sink.calls] == [c[2] for c in seq_sink.calls]
    assert [c[2] for c in par_sink.calls] == [s["n_trades"] for s in par]
    assert _CountingPool.submitted == len(params)
    # 결과를 1개 받을 때마다 아직 받지 않은(미완료) run 은 윈도우 이하 — run 수와 무관한 상한
    assert max(outstanding) <= br.WINDOW_PER_JOB * 2
    assert all(isinstance(s, dict) for s in par)
    assert not any(isinstance(v, pd.DataFrame) for s in par for v in s.values())


def test_discard_sink_gets_summaries_only(norm, grid_file):
    _, params = load_grid(grid_file)
    bars = load_bars(D1, D2, SYM, out_dir=norm)
    seq = br.run_grid(bars, params, "default", D1, END_D, None, br._DiscardSink())
    par = br.run_grid(bars, params, "default", D1, END_D, None, br._DiscardSink(), jobs=2)
    assert par == seq


def test_worker_exception_propagates(norm, grid_file, tmp_path):
    _, params = load_grid(grid_file)
    bars = load_bars(D1, D2, SYM, out_dir=norm).drop(columns=["close"])  # 워커 안 generate_run 에서 실패
    with pytest.raises(Exception) as seq_err:
        br.run_grid(bars, params, "default", D1, END_D, None, _ListSink())
    paths = br.output_paths(tmp_path / "out", "default", D1, END_D, sorted({p.strategy_id for p in params}))
    with pytest.raises(type(seq_err.value)) as par_err:
        with br.RoundtripSink(paths) as sink:
            br.run_grid(bars, params, "default", D1, END_D, None, sink, jobs=2)
    assert type(par_err.value.__cause__).__name__ == "_RemoteTraceback"  # 워커 프로세스에서 난 예외
    assert _files(tmp_path / "out") == []


@pytest.mark.parametrize("jobs", ["0", "-1"])
@pytest.mark.parametrize("mode", ["base", "walkforward", "oos"])
def test_jobs_below_1_exit_2(mode, jobs, norm, grid_file, tmp_path):
    out = tmp_path / "out"
    if mode == "base":
        argv = _base_argv(norm, out, grid_file, jobs)
    elif mode == "walkforward":
        argv = ["--walkforward", "--grid", str(grid_file), "--out", str(out), "--jobs", jobs]
    else:
        argv = ["--oos-final", str(tmp_path / "sel.json"), "--oos-log", str(tmp_path / "log.jsonl"),
                "--grid", str(grid_file), "--out", str(out), "--jobs", jobs]
    with pytest.raises(SystemExit) as e:
        br.main(argv)
    assert e.value.code == 2
    assert _files(out) == [] and not (tmp_path / "log.jsonl").exists()


def test_run_grid_rejects_jobs_below_1(norm, grid_file):
    _, params = load_grid(grid_file)
    with pytest.raises(ValueError):
        br.run_grid(pd.DataFrame(), params, "default", D1, END_D, None, _ListSink(), jobs=0)
