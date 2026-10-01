"""analysis.run `--jobs N` — 순차와 산출물 바이트 동일, 정렬 순서·윈도우 상한, 워커 예외 전파, 인자 검사.

워커는 spawn 이라 테스트의 monkeypatch 가 보이지 않는다. 워커 쪽 실패는 실제 잘못된 입력(`close` 열 누락)으로 낸다.
"""

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

from src.analysis import run as ar
from src.ingest.store import load_bars
from src.shared import parallel as shared_parallel
from tests.test_analysis_run import D1, D2, SYM, write_days

# 트리거 2개 × 4 = 8 run > WINDOW_PER_JOB × 2 → 윈도우 재충전·트리거별 싱크 파일 분기가 실제로 돈다
GRID = {"trigger": ["h1", "h2"], "n": [15], "k": [2], "stop_pct": [0.5], "tp_r": [2],
        "max_hold": [60, 240], "risk_pct": [1, 2]}


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


def _argv(norm, out, grid, jobs):
    return ["--start", str(D1), "--end", str(D2), "--symbol", SYM, "--data-dir", str(norm), "--out", str(out),
            "--grid", str(grid), "--jobs", str(jobs)]


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


def test_grid_size_exercises_window(grid_file):
    _, params = ar.load_grid(grid_file)
    assert len(params) > shared_parallel.WINDOW_PER_JOB * 2 and len(GRID["trigger"]) >= 2


def test_cli_jobs1_vs_jobs2_identical(norm, grid_file, tmp_path):
    o1, o2 = tmp_path / "o1", tmp_path / "o2"
    assert ar.main(_argv(norm, o1, grid_file, 1)) == 0
    assert ar.main(_argv(norm, o2, grid_file, 2)) == 0
    files = _files(o1)
    assert files == _files(o2)
    assert any(f.suffix == ".parquet" for f in files) and any(f.suffix == ".json" for f in files) \
        and any(f.suffix == ".md" for f in files)
    for f in files:
        assert (o1 / f).read_bytes() == (o2 / f).read_bytes(), f
    assert not list(o2.rglob("*.tmp"))
    report = json.loads(next((o1 / "distributions").glob("*.json")).read_text())
    assert any(r["n_trades"] > 0 for r in report["runs"])  # 거래가 있는 run 이 있어야 비교가 공허하지 않다


def test_run_grid_order_and_window(norm, grid_file, monkeypatch):
    _, params = ar.load_grid(grid_file)
    bars = load_bars(D1, D2, SYM, out_dir=norm)
    seq_sink = _ListSink()
    seq = ar.run_grid(bars, params, seq_sink)

    monkeypatch.setattr(shared_parallel, "ProcessPoolExecutor", _CountingPool)
    _CountingPool.submitted = 0
    outstanding = []
    par_sink = _ListSink(on_write=lambda: outstanding.append(_CountingPool.submitted - len(par_sink.calls)))
    par = ar.run_grid(bars, list(reversed(params)), par_sink, jobs=2)

    assert json.dumps(ar.to_jsonable(par)) == json.dumps(ar.to_jsonable(seq))  # 섞인 입력에도 정렬 순서·같은 값
    assert [(s["strategy_id"], s["param_id"]) for s in par] == [(s["strategy_id"], s["param_id"]) for s in seq]
    assert par_sink.calls == seq_sink.calls
    assert [c[2] for c in par_sink.calls] == [s["n_trades"] for s in par]
    assert _CountingPool.submitted == len(params)
    # 결과를 1개 받을 때마다 아직 받지 않은(미완료) run 은 윈도우 이하 — run 수와 무관한 상한
    assert max(outstanding) <= shared_parallel.WINDOW_PER_JOB * 2
    assert not any(isinstance(v, pd.DataFrame) for s in par for v in s.values())


def test_worker_exception_propagates(norm, grid_file, tmp_path):
    _, params = ar.load_grid(grid_file)
    bars = load_bars(D1, D2, SYM, out_dir=norm).drop(columns=["close"])  # 워커 안 generate_run 에서 실패
    with pytest.raises(Exception) as seq_err:
        ar.run_grid(bars, params, _ListSink())
    out = tmp_path / "out"
    paths = ar.output_paths(out, D1, D2, sorted({p.strategy_id for p in params}))
    with pytest.raises(type(seq_err.value)) as par_err:
        with ar.open_sink(paths) as sink:
            ar.run_grid(bars, params, sink, jobs=2)
    assert type(par_err.value.__cause__).__name__ == "_RemoteTraceback"  # 워커 프로세스에서 난 예외
    assert _files(out) == []  # .tmp 포함 아무 파일도 남지 않는다


@pytest.mark.parametrize("jobs", ["0", "-1"])
def test_jobs_below_1_exit_2(jobs, norm, grid_file, tmp_path):
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as e:
        ar.main(_argv(norm, out, grid_file, jobs))
    assert e.value.code == 2
    assert _files(out) == []


def test_run_grid_jobs_0_value_error(grid_file):
    _, params = ar.load_grid(grid_file)
    with pytest.raises(ValueError):
        ar.run_grid(pd.DataFrame(), params, _ListSink(), jobs=0)
