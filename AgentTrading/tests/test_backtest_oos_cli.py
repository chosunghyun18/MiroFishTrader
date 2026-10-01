"""backtest.run `--oos-final` CLI — 합성 OOS 바·임시 접근 로그로 `main` 을 그대로 실행.

모든 테스트는 `chdir(tmp_path)` + `--oos-log`·`--out` 을 tmp 아래로 주어 실제 `data/backtest/oos_access.jsonl` 을
건드리지 않는다(`isolated` fixture 가 끝에 기본 상대경로 로그가 생기지 않았음을 확인).
"""

import json
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.run import load_grid
from src.backtest import run as br
from src.backtest import walkforward as wf
from src.backtest.walkforward import OOS_LOG_PATH, OOS_START, selection_sha256
from src.ingest.store import MissingDaysError
from src.shared.schema import BARS_1M

SYM = "XBTUSD"
T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
GRID = {"trigger": ["h1", "h2"], "n": [15], "k": [2], "stop_pct": [0.5], "tp_r": [2],
        "max_hold": [60], "risk_pct": [1, 2]}
LOOSE = {"min_trades": 1, "min_sharpe": 0.0, "max_drawdown": 1.0}
_CLOSE = 30000.0 * np.exp(np.cumsum(np.random.default_rng(22).normal(0, 0.0015, 2880)))


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
    """`store.load_bars(start, end, symbol, *, out_dir)` 대역(종료일 포함). OOS 요청이면 첫 2일 랜덤워크.

    `fail_on` 번째 호출(1부터)에서 `MissingDaysError`. 호출을 기록한다.
    """

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, start: date, end: date, symbol, *, out_dir):
        self.calls.append((start, end, symbol, out_dir))
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise MissingDaysError([end])
        return _bars(T(start), _CLOSE)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    assert not (tmp_path / OOS_LOG_PATH).exists()  # 주입한 로그 경로만 쓰였다


@pytest.fixture
def grid_file(isolated):
    p = isolated / "grid.json"
    p.write_text(json.dumps(GRID))
    return p


@pytest.fixture
def loader(monkeypatch):
    fake = FakeLoadBars()
    monkeypatch.setattr(br, "load_bars", fake)
    return fake


def _selection(grid_file, idx=0, gate=LOOSE):
    _, params = load_grid(grid_file)
    p = sorted(params, key=lambda q: (q.strategy_id, q.param_id))[idx]
    return wf.make_selection({"start": "2018-03-01", "end": "2022-01-01", "strategy_id": p.strategy_id,
                              "param_id": p.param_id}, gate)


def _write_sel(path: Path, sel) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sel) if not isinstance(sel, str) else sel)
    return path


def _argv(sel_path, log, out, grid, *extra, data_dir="norm"):
    return ["--oos-final", str(sel_path), "--oos-log", str(log), "--grid", str(grid), "--data-dir", str(data_dir),
            "--out", str(out), *extra]


def _files(root):
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _report(out, sel):
    return json.loads((out / "oos" / f"{sel['sha256'][:12]}.json").read_text(), parse_constant=pytest.fail)


def _exit2(argv):
    with pytest.raises(SystemExit) as e:
        br.main(argv)
    assert e.value.code == 2


# 정상 실행 -------------------------------------------------------------------------------------------

def test_success_outputs_and_log(isolated, grid_file, loader):
    sel = _selection(grid_file)
    sp = _write_sel(isolated / "sel" / "selection.json", sel)
    log, out = isolated / "logs" / "oos.jsonl", isolated / "out"
    assert br.main(_argv(sp, log, out, grid_file)) == 0

    rep = _report(out, sel)  # allow_nan=False 로 썼으므로 NaN 상수 없음
    assert (out / "oos" / f"{sel['sha256'][:12]}.md").is_file()
    assert set(rep["results"]) == {"default", "bybit"}
    for k in ("verdict", "bybit_verdict", "dsr", "phase4_eligible", "walkforward_verdict"):
        assert k in rep
    assert rep["meta"]["fee"]["bybit"] == br.costs.PROFILES["bybit"]
    assert rep["meta"]["judge_profile"] == "default"
    assert rep["oos"] == ["2022-01-01", "2025-01-01"]
    assert rep["selection"] == sel
    assert rep["results"]["default"]["n_trades"] > 0  # 합성 데이터에서 거래가 있어야 의미 있다
    assert rep["verdict"] == rep["results"]["default"]["gate"]
    assert rep["bybit_verdict"] == rep["results"]["bybit"]["gate"]

    lines = log.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec) == {"ts", "sha256", "strategy_id", "param_id", "gate"}
    assert rec["sha256"] == sel["sha256"] and rec["gate"] == rep["verdict"]
    assert (rec["strategy_id"], rec["param_id"]) == (sel["strategy_id"], sel["param_id"])
    assert [c[:2] for c in loader.calls] == [(date(2022, 1, 1), date(2024, 12, 31))]  # 종료일 포함 변환


# 변조·불일치 선택 → 2 ---------------------------------------------------------------------------------

def _tamper_param(sel):
    sel["param_id"] = sel["param_id"] + "x"
    return sel


def _tamper_sha(sel):
    sel["sha256"] = ("0" if sel["sha256"][0] != "0" else "1") + sel["sha256"][1:]
    return sel


def _drop_key(sel):
    del sel["gate"]
    return sel


def _not_full_sample(sel):
    sel["selected_on"] = ["2018-03-01", "2021-01-01"]
    sel["sha256"] = selection_sha256(sel)  # 해시를 맞춰도 표본 전체가 아니면 거부
    return sel


@pytest.mark.parametrize("tamper", [_tamper_param, _tamper_sha, _drop_key, _not_full_sample,
                                    "broken", "missing", "list"])
def test_bad_selection_exit_2(isolated, grid_file, loader, tamper):
    sp = isolated / "sel" / "selection.json"
    if tamper == "broken":
        _write_sel(sp, "{not json")
    elif tamper == "list":
        _write_sel(sp, "[1, 2]")
    elif tamper != "missing":
        _write_sel(sp, tamper(_selection(grid_file)))
    log, out = isolated / "oos.jsonl", isolated / "out"
    _exit2(_argv(sp, log, out, grid_file))
    assert not log.exists() and not out.exists()
    assert loader.calls == []


# 접근 로그 가드 ---------------------------------------------------------------------------------------

def test_other_sha_rejected_same_sha_allowed(isolated, grid_file, loader):
    log, out = isolated / "oos.jsonl", isolated / "out"
    first = _selection(grid_file, 0)
    sp1 = _write_sel(isolated / "a" / "selection.json", first)
    assert br.main(_argv(sp1, log, out, grid_file)) == 0
    log_bytes = log.read_bytes()
    j1 = (out / "oos" / f"{first['sha256'][:12]}.json").read_bytes()
    m1 = (out / "oos" / f"{first['sha256'][:12]}.md").read_bytes()
    n_calls = len(loader.calls)

    second = _selection(grid_file, 1)
    assert second["sha256"] != first["sha256"]
    sp2 = _write_sel(isolated / "b" / "selection.json", second)
    _exit2(_argv(sp2, log, out, grid_file))
    assert log.read_bytes() == log_bytes
    assert not (out / "oos" / f"{second['sha256'][:12]}.json").exists()
    assert not (out / "oos" / f"{second['sha256'][:12]}.md").exists()
    assert len(loader.calls) == n_calls

    assert br.main(_argv(sp1, log, out, grid_file)) == 0  # 같은 sha256 재실행은 허용
    assert len(log.read_text().splitlines()) == 2
    assert (out / "oos" / f"{first['sha256'][:12]}.json").read_bytes() == j1  # 결정성
    assert (out / "oos" / f"{first['sha256'][:12]}.md").read_bytes() == m1


def test_unreadable_log_line_exit_2(isolated, grid_file, loader):
    log, out = isolated / "oos.jsonl", isolated / "out"
    log.write_text("garbage\n")
    sp = _write_sel(isolated / "sel" / "selection.json", _selection(grid_file))
    _exit2(_argv(sp, log, out, grid_file))
    assert log.read_text() == "garbage\n"
    assert not out.exists() and loader.calls == []


def test_log_race_after_load_exit_2(isolated, grid_file, monkeypatch):
    """데이터를 읽는 사이 다른 sha256 이 로그에 생기면 `evaluate_oos` 의 PermissionError → 2, 기록·산출물 없음."""
    log, out = isolated / "oos.jsonl", isolated / "out"
    other = '{"sha256": "' + "f" * 64 + '"}\n'

    def racing(start, end, symbol, *, out_dir):
        log.write_text(other)
        return _bars(T(start), _CLOSE)

    monkeypatch.setattr(br, "load_bars", racing)
    sp = _write_sel(isolated / "sel" / "selection.json", _selection(grid_file))
    _exit2(_argv(sp, log, out, grid_file))
    assert log.read_text() == other
    assert _files(out) == []


# 실행 실패 → 1, 로그 미기록 ----------------------------------------------------------------------------

def test_missing_day_exit_1_no_log(isolated, grid_file, monkeypatch):
    fake = FakeLoadBars(fail_on=1)
    monkeypatch.setattr(br, "load_bars", fake)
    sp = _write_sel(isolated / "sel" / "selection.json", _selection(grid_file))
    log, out = isolated / "oos.jsonl", isolated / "out"
    assert br.main(_argv(sp, log, out, grid_file)) == 1
    assert len(fake.calls) == 1
    assert not log.exists()
    assert _files(out) == []  # .tmp 포함 아무 파일도 없다


def test_missing_day_real_store_exit_1(isolated, grid_file):
    sp = _write_sel(isolated / "sel" / "selection.json", _selection(grid_file))
    log, out = isolated / "oos.jsonl", isolated / "out"
    assert br.main(_argv(sp, log, out, grid_file, data_dir=isolated / "empty")) == 1
    assert not log.exists()
    assert _files(out) == []


# 리포트 문구 -----------------------------------------------------------------------------------------

def test_markdown_sections(isolated, grid_file, loader):
    sel = _selection(grid_file)
    sp = _write_sel(isolated / "sel" / "selection.json", sel)
    out = isolated / "out"
    assert br.main(_argv(sp, isolated / "oos.jsonl", out, grid_file)) == 0
    rep = _report(out, sel)
    md = (out / "oos" / f"{sel['sha256'][:12]}.md").read_text()
    assert rep["n_trials"] == 756
    assert f"## OOS 판정\n\n- **{rep['verdict']}**" in md
    assert f"## bybit 병기\n\n- {rep['bybit_verdict']}" in md
    assert "## DSR 참고값 (N=756, 판정 미반영)" in md
    assert "Phase 4 진입 자격 = 워크포워드 pass 그리고 OOS pass" in md
    assert "사람 판단" in md
    assert sel["sha256"][:12] in md
    lines = md.splitlines()
    assert any(ln.startswith("| default |") for ln in lines)
    assert any(ln.startswith("| bybit |") for ln in lines)


# DSR V 출처 ------------------------------------------------------------------------------------------

def _wf_report(sha, var_sr=0.0004, verdict="pass"):
    return {"dsr": {"var_sr": var_sr}, "verdict": verdict, "full_sample": {"selection": {"sha256": sha}}}


def test_dsr_from_matching_walkforward_report(isolated, grid_file, loader):
    sel = _selection(grid_file)
    sp = _write_sel(isolated / "walkforward" / "selection.json", sel)
    (isolated / "walkforward" / "default.json").write_text(json.dumps(_wf_report(sel["sha256"])))
    out = isolated / "out"
    assert br.main(_argv(sp, isolated / "oos.jsonl", out, grid_file)) == 0
    rep = _report(out, sel)
    assert rep["dsr"]["var_sr"] == pytest.approx(0.0004)
    assert rep["dsr"]["source"].endswith("default.json")
    assert rep["dsr"]["sr0"] is not None
    assert rep["results"]["default"]["sr_daily"] is not None
    assert rep["dsr"]["default"] is not None and 0.0 <= rep["dsr"]["default"] <= 1.0
    assert rep["walkforward_verdict"] == "pass"
    assert rep["phase4_eligible"] is (rep["verdict"] == "pass")
    md = (out / "oos" / f"{sel['sha256'][:12]}.md").read_text()
    assert "V 없음" not in md


@pytest.mark.parametrize("wf_content", [None, "other_sha", "broken"])
def test_dsr_without_walkforward_report(isolated, grid_file, loader, wf_content):
    sel = _selection(grid_file)
    sp = _write_sel(isolated / "walkforward" / "selection.json", sel)
    if wf_content == "other_sha":
        (isolated / "walkforward" / "default.json").write_text(json.dumps(_wf_report("e" * 64)))
    elif wf_content == "broken":
        (isolated / "walkforward" / "default.json").write_text("{oops")
    out = isolated / "out"
    assert br.main(_argv(sp, isolated / "oos.jsonl", out, grid_file)) == 0
    rep = _report(out, sel)
    assert rep["dsr"]["var_sr"] is None and rep["dsr"]["default"] is None and rep["dsr"]["bybit"] is None
    assert rep["dsr"]["source"] is None
    assert rep["walkforward_verdict"] is None and rep["phase4_eligible"] is None
    md = (out / "oos" / f"{sel['sha256'][:12]}.md").read_text()
    assert "V 없음" in md and "판단 불가" in md
    assert "Phase 4 진입 자격 = 워크포워드 pass 그리고 OOS pass" in md


# 인자 규칙 → 2 ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    ["--walkforward"],
    ["--start", "2020-03-01", "--end", "2020-04-01"],
    ["--start", "2022-01-01"],
    ["--end", "2025-01-01"],
    ["--fee-profile", "bybit"],
])
def test_oos_arg_rules_exit_2(isolated, grid_file, loader, extra):
    sp = _write_sel(isolated / "sel" / "selection.json", _selection(grid_file))
    log, out = isolated / "oos.jsonl", isolated / "out"
    _exit2(_argv(sp, log, out, grid_file, *extra))
    assert not log.exists() and not out.exists() and loader.calls == []


def test_selection_not_in_grid_exit_2(isolated, grid_file, loader):
    sp = _write_sel(isolated / "sel" / "selection.json", _selection(grid_file))
    small = isolated / "small.json"
    small.write_text(json.dumps({**GRID, "n": [20]}))  # 다른 그리드 → 선택 run 이 없다
    log, out = isolated / "oos.jsonl", isolated / "out"
    _exit2(_argv(sp, log, out, small))
    assert not log.exists() and not out.exists() and loader.calls == []


@pytest.mark.parametrize("mode", [["--walkforward"], ["--start", "2020-03-01", "--end", "2020-04-01"]])
def test_oos_log_without_oos_final_exit_2(isolated, grid_file, loader, mode):
    out = isolated / "out"
    _exit2(["--oos-log", str(isolated / "oos.jsonl"), "--grid", str(grid_file), "--out", str(out), *mode])
    assert not (isolated / "oos.jsonl").exists() and not out.exists() and loader.calls == []


# 로그를 지우는 코드 경로 없음 ----------------------------------------------------------------------------

def test_no_code_path_deletes_oos_log():
    """`src/backtest/` 에서 접근 로그를 다루는 줄에 삭제·덮어쓰기 호출이 없다(쓰기는 append 하나뿐)."""
    root = Path(__file__).resolve().parents[1] / "src" / "backtest"
    log_ref = re.compile(r"OOS_LOG_PATH|log_path|oos_log")
    destructive = re.compile(r"\.unlink\(|rmtree|os\.remove|\.write_text\(|\.write_bytes\(|open\([^)]*[\"']w|"
                             r"os\.replace|_write_text_atomic")
    hits = []
    for f in sorted(root.glob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if log_ref.search(line) and destructive.search(line):
                hits.append(f"{f.name}:{n}: {line.strip()}")
    assert hits == []
    src = (root / "walkforward.py").read_text(encoding="utf-8")
    assert src.count('.open("a"') == 1  # evaluate_oos 의 append 하나
