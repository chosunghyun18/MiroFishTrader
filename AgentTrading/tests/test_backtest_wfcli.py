"""backtest.run `--walkforward` CLI — 폴드·로더·게이트를 모듈 속성으로 주입해 합성 데이터로 `main` 을 그대로 실행."""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.backtest import run as br
from src.backtest import walkforward as wf
from src.backtest.walkforward import OOS_START, SAMPLE_START, Fold, selection_sha256
from src.ingest.store import MissingDaysError
from src.shared.schema import BARS_1M

SYM = "XBTUSD"
T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
ORIGIN = T("2020-03-01")
FOLDS = [
    Fold(T("2020-03-01"), T("2020-03-03"), T("2020-03-03"), T("2020-03-05")),
    Fold(T("2020-03-01"), T("2020-03-05"), T("2020-03-05"), T("2020-03-07")),
]
GRID = {"trigger": ["h1", "h2"], "n": [15], "k": [2], "stop_pct": [0.5], "tp_r": [2],
        "max_hold": [60], "risk_pct": [1, 2]}
LOOSE = {"min_trades": 1, "min_sharpe": 0.0, "max_drawdown": 1.0}

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


class FakeLoadBars:
    """`store.load_bars(start, end, symbol, *, out_dir)` 대역(종료일 포함). 호출을 기록한다.

    폴드 구간은 2020-03 결정적 랜덤워크, 기본 표본 전체 요청이면 첫 2일만 돌려준다(빠르게).
    `fail_on` 번째 호출(1부터)에서 `MissingDaysError`.
    """

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, start: date, end: date, symbol, *, out_dir):
        self.calls.append((start, end, symbol, out_dir))
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise MissingDaysError([end])
        s, e = T(start), T(end) + pd.Timedelta(days=1)
        if (s, e) == (SAMPLE_START, OOS_START):
            rng = np.random.default_rng(int(s.timestamp()) // 86400)
            return _bars(s, 8000.0 * np.exp(np.cumsum(rng.normal(0, 0.0015, 2880))))
        i0 = int((s - ORIGIN) / pd.Timedelta(minutes=1))
        i1 = int((e - ORIGIN) / pd.Timedelta(minutes=1))
        return _bars(s, _CLOSE[i0:i1])


class FoldsSpy:
    def __init__(self, folds=FOLDS):
        self.calls = []
        self.folds = folds

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return list(self.folds)


def _loose_gate(gate):
    g = wf.resolve_gate(gate)
    g.update(LOOSE)
    return g


def _strict_gate(gate):
    g = wf.resolve_gate(gate)
    g["min_trades"] = 10**9  # 어떤 run 도 후보가 될 수 없다 → 선택 없음
    return g


@pytest.fixture
def grid_file(tmp_path):
    p = tmp_path / "grid.json"
    p.write_text(json.dumps(GRID))
    return p


@pytest.fixture
def env(monkeypatch):
    """gate 로 `br.resolve_gate` 를 바꾼다(기본 완화). (로더, 폴드 spy) 를 돌려준다."""

    def setup(gate=_loose_gate, loader=None, folds=None):
        loader = loader or FakeLoadBars()
        folds = folds or FoldsSpy()
        monkeypatch.setattr(br, "load_bars", loader)
        monkeypatch.setattr(br, "make_folds", folds)
        monkeypatch.setattr(br, "resolve_gate", gate)
        return loader, folds

    return setup


def _argv(out, grid, *extra, data_dir="norm"):
    return ["--walkforward", "--grid", str(grid), "--data-dir", str(data_dir), "--out", str(out), *extra]


def _read(out, name):
    return json.loads((out / "walkforward" / name).read_text(), parse_constant=pytest.fail)


def test_uses_default_folds(env, grid_file, tmp_path):
    _, folds = env()
    assert br.main(_argv(tmp_path / "out", grid_file)) == 0
    assert folds.calls == [((), {})]  # 인자 없이 = 기본 표본 6폴드
    real = wf.make_folds()
    assert len(real) == 6
    assert real[0].train_start == SAMPLE_START and real[-1].test_end == OOS_START


def test_outputs_and_selection(env, grid_file, tmp_path):
    env()
    out = tmp_path / "out"
    assert br.main(_argv(out, grid_file)) == 0
    rep = _read(out, "default.json")  # allow_nan=False 로 썼으므로 NaN 상수 없음
    assert (out / "walkforward" / "default.md").is_file()
    assert len(rep["folds"]) == 2
    assert set(rep["stitched"]) == {"default", "bybit"}
    for k in ("dsr", "verdict", "bybit_verdict"):
        assert k in rep
    assert rep["meta"]["fee"]["bybit"] == br.costs.PROFILES["bybit"]
    assert rep["meta"]["judge_profile"] == "default" and rep["meta"]["symbol"] == SYM
    assert rep["sample"] == ["2018-03-01", "2022-01-01"]

    sel = rep["full_sample"]["selection"]
    assert sel is not None  # 선택이 있어야 이 테스트가 의미 있다
    on_disk = _read(out, "selection.json")
    assert on_disk == sel
    assert selection_sha256(on_disk) == on_disk["sha256"]
    assert on_disk["selected_on"] == ["2018-03-01", "2022-01-01"]
    assert on_disk["gate"] == _loose_gate(None)  # make_selection 이 원본 resolve_gate 로 병합해도 완화값 그대로


def test_no_selection_writes_no_file_and_removes_stale(env, grid_file, tmp_path):
    env(gate=_strict_gate)  # 합성 데이터는 기본 게이트로도 표본 전체 선택이 나올 수 있어 불가능한 게이트를 쓴다
    out = tmp_path / "out"
    stale = out / "walkforward" / "selection.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}\n")
    assert br.main(_argv(out, grid_file)) == 0
    rep = _read(out, "default.json")
    assert rep["full_sample"]["selection"] is None
    assert all(f["selection"] is None for f in rep["folds"])
    assert not stale.exists()
    assert "선택 없음" in (out / "walkforward" / "default.md").read_text()


def _files(root):
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_missing_day_exit_1_no_outputs(env, grid_file, tmp_path):
    loader, _ = env(loader=FakeLoadBars(fail_on=2))
    out = tmp_path / "out"
    assert br.main(_argv(out, grid_file)) == 1
    assert len(loader.calls) == 2
    assert _files(out) == []  # .tmp 포함 아무 파일도 없다


def test_missing_day_real_store_exit_1(monkeypatch, grid_file, tmp_path):
    monkeypatch.setattr(br, "make_folds", FoldsSpy())
    out = tmp_path / "out"
    assert br.main(_argv(out, grid_file, data_dir=tmp_path / "empty")) == 1
    assert _files(out) == []


@pytest.mark.parametrize("extra", [
    ["--start", "2022-01-01", "--end", "2022-07-01"],  # OOS
    ["--start", "2025-01-01", "--end", "2025-01-02"],  # 2025 이후
    ["--start", "2020-03-01", "--end", "2020-04-01"],  # 표본 안이어도 거부
    ["--start", "2020-03-01"],
    ["--end", "2020-04-01"],
    ["--fee-profile", "bybit"],
])
def test_rejected_before_load(env, grid_file, tmp_path, extra):
    loader, folds = env()
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as e:
        br.main(_argv(out, grid_file, *extra))
    assert e.value.code == 2
    assert loader.calls == [] and folds.calls == []
    assert not out.exists()


@pytest.mark.parametrize("extra", [["--start", "2020-03-01"], ["--end", "2020-04-01"], []])
def test_default_mode_requires_start_end(grid_file, tmp_path, extra):
    with pytest.raises(SystemExit) as e:
        br.main(["--grid", str(grid_file), "--data-dir", str(tmp_path / "e"), "--out", str(tmp_path / "out"),
                 *extra])
    assert e.value.code == 2
    assert not (tmp_path / "out").exists()


def test_deterministic(env, grid_file, tmp_path):
    env()
    a, b = tmp_path / "a", tmp_path / "b"
    assert br.main(_argv(a, grid_file)) == 0
    assert br.main(_argv(b, grid_file)) == 0
    for name in ("default.json", "default.md", "selection.json"):
        assert (a / "walkforward" / name).read_bytes() == (b / "walkforward" / name).read_bytes()


def test_markdown_sections(env, grid_file, tmp_path):
    env()
    out = tmp_path / "out"
    assert br.main(_argv(out, grid_file)) == 0
    rep = _read(out, "default.json")
    md = (out / "walkforward" / "default.md").read_text()
    lines = md.splitlines()
    header = "| " + " | ".join(br.WF_FOLD_COLUMNS) + " |"
    i = lines.index(header)
    assert [ln.split(" | ")[0] for ln in lines[i + 2:i + 4]] == ["| 1", "| 2"]
    assert lines[i + 4] == ""  # 폴드 행은 2개
    assert "## 검증 곡선 3기준" in md
    assert any(ln.startswith("| default |") for ln in lines)
    assert any(ln.startswith("| bybit |") for ln in lines)
    assert "## DSR" in md
    assert f"## 최종 판정\n\n- **{rep['verdict']}**" in md
    assert f"## bybit 민감도\n\n- {rep['bybit_verdict']}" in md
    assert rep["full_sample"]["selection"]["sha256"][:12] in md


def test_store_loader_half_open(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(br, "load_bars", lambda s, e, sym, *, out_dir: calls.append((s, e, sym, out_dir)) or "ok")
    load = br.store_loader(SYM, tmp_path)
    assert load(T("2019-03-01"), T("2019-09-01")) == "ok"
    assert calls == [(date(2019, 3, 1), date(2019, 8, 31), SYM, tmp_path)]
