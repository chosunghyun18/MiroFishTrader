"""backtest.run `--funding` — 기본·`--walkforward`·`--oos-final`·`--jobs N` 펀딩 로드·결측 검사·적용·리포트 열.

설계: Obsidian `design/phase3-backtest.md` "펀딩 모델" 5·8·9·10항. 합성 1분봉(기존 CLI 테스트 픽스처)과
`GRID_HOURS` 격자 합성 펀딩 파일(요율 0.0001·0.003·−0.0005 순환)로 `main` 을 그대로 실행한다.
끔 상태 바이트 동일성은 구현 전 코드(HEAD f22fe93)로 만든 산출물 sha256 을 `OFF_PINNED` 에 고정해 확인한다.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src.analysis.run import load_grid, to_jsonable
from src.analysis.synthetic import generate_run
from src.backtest import run as br
from src.backtest import walkforward as wf
from src.backtest.costs import apply_costs, apply_funding
from src.backtest.metrics import SUMMARY_KEYS, summarize_net_run
from src.backtest.walkforward import OOS_END, OOS_START, SAMPLE_START, resolve_gate, selection_sha256
from src.ingest.bitmex_funding import funding_path, load_funding, write_funding
from src.ingest.store import load_bars
from src.shared.schema import (FUNDING, ROUNDTRIPS_NET, ROUNDTRIPS_NET_FUNDING, validate_roundtrips_net,
                               validate_roundtrips_net_funding)
from tests.test_backtest_oos_cli import FakeLoadBars as OosLoadBars
from tests.test_backtest_oos_cli import _selection as oos_selection
from tests.test_backtest_run import D1, D2, SPAN, SYM, write_days
from tests.test_backtest_wfcli import FakeLoadBars, FoldsSpy, _loose_gate

def T(s) -> pd.Timestamp:
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")

# 트리거 2개 × 4 = 8 run(병렬 윈도우 2N=4 초과), max_hold 240 이라 정산을 넘는 거래가 생긴다
GRID = {"trigger": ["h1", "h2"], "n": [15], "k": [2], "stop_pct": [0.5], "tp_r": [2],
        "max_hold": [60, 240], "risk_pct": [1, 2]}
START, END = "2020-03-12", "2020-03-14"
RATES = (0.0001, 0.003, -0.0005)
FUND_START, FUND_END = "2018-03-01", "2025-01-01"  # 기본 표본 + OOS 전체 격자
FUNDING_LABEL = "default+funding"


def funding_frame(start=FUND_START, end=FUND_END, drop=(), extra=()):
    """`[start, end)` 매일 04·12·20 UTC 격자 펀딩(요율 순환). `drop` 시각 제거, `extra` = [(ts, rate)] 추가."""
    ts = pd.date_range(T(start) + pd.Timedelta(hours=4), T(end), freq="8h", inclusive="left")
    rate = np.array([RATES[i % len(RATES)] for i in range(len(ts))])
    df = pd.DataFrame({"ts": ts, "symbol": SYM, "funding_rate": rate})
    df = df[~df["ts"].isin([T(t) for t in drop])]
    if extra:
        df = pd.concat([df, pd.DataFrame({"ts": [T(t) for t, _ in extra], "symbol": SYM,
                                          "funding_rate": [r for _, r in extra]})], ignore_index=True)
    return df.sort_values("ts", kind="stable").reset_index(drop=True).astype(FUNDING.dtypes)


def write_fund(root: Path, **kw) -> Path:
    d = root / "funding"
    write_funding(funding_frame(**kw), SYM, d)
    return d


def _files(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


@pytest.fixture
def fdir(tmp_path):
    return write_fund(tmp_path)


def _default_argv(norm, out, grid, *extra, jobs=1):
    return ["--start", START, "--end", END, "--symbol", SYM, "--data-dir", str(norm), "--out", str(out),
            "--grid", str(grid), "--jobs", str(jobs), *extra]


@pytest.fixture
def wf_env(monkeypatch):
    loader = FakeLoadBars()
    monkeypatch.setattr(br, "load_bars", loader)
    monkeypatch.setattr(br, "make_folds", FoldsSpy())
    monkeypatch.setattr(br, "resolve_gate", _loose_gate)
    return loader


def _wf_argv(out, grid, *extra, jobs=1):
    return ["--walkforward", "--grid", str(grid), "--data-dir", "norm", "--out", str(out), "--jobs", str(jobs),
            *extra]


@pytest.fixture
def oos_env(monkeypatch, tmp_path):
    """OOS: chdir(기본 접근 로그 경로 오염 방지) + 합성 OOS 로더. 끝에 기본 로그가 안 생겼는지 확인."""
    monkeypatch.chdir(tmp_path)
    loader = OosLoadBars()
    monkeypatch.setattr(br, "load_bars", loader)
    yield loader
    assert not (tmp_path / wf.OOS_LOG_PATH).exists()


def _oos_argv(sel_path, log, out, grid, *extra):
    return ["--oos-final", str(sel_path), "--oos-log", str(log), "--grid", str(grid), "--data-dir", "norm",
            "--out", str(out), *extra]


def _write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return path


def _exit2(argv):
    with pytest.raises(SystemExit) as e:
        br.main(argv)
    assert e.value.code == 2


# 끔 상태 바이트 동일(완료 기준 4) ---------------------------------------------------------------------

# 구현 전 HEAD f22fe93 코드로 `_off_outputs` 를 돌려 얻은 sha256(같은 픽스처·같은 환경).
OFF_PINNED = {
    "default.json": "769a108a4599d652014b129a16e1b62b93415293238dfeab5991b7bb3ca25b32",
    "default.md": "4d1f70edc67016767d260d7e071c3a42e753614f0d93631404261dc412f901c3",
    "wf/default.json": "430a8c06977ae9d4205222ddc811f9301c8b4ca362c24f6b4eed64fd7d454357",
    "wf/default.md": "eac419e932c80602eaaf12278b31b6becb5c1a6ae21962f5ae780d4dcb9718df",
    "wf/selection.json": "670722740b03129c242b55691e5b3a786634695d3ca44dc2af5749c0afdec001",
    "oos.json": "f51063eec4c71cb43f924eabaf9714b754fffc0f953223fa30c8700d0c13b247",
    "oos.md": "055649c638415036a01aa416cfa4ac5a2c4b89ce8d0e1c2f6c0fc91df4a58e3b",
}


def _off_outputs(tmp_path, monkeypatch, norm, grid_file) -> dict[str, Path]:
    """세 모드 대표 픽스처를 펀딩 없이 실행 → {이름: 산출 파일}."""
    out = tmp_path / "off"
    assert br.main(_default_argv(norm, out / "base", grid_file)) == 0
    files = {"default.json": out / "base" / "summary" / "default" / f"{SPAN}.json",
             "default.md": out / "base" / "summary" / "default" / f"{SPAN}.md"}
    with monkeypatch.context() as m:
        m.setattr(br, "load_bars", FakeLoadBars())
        m.setattr(br, "make_folds", FoldsSpy())
        m.setattr(br, "resolve_gate", _loose_gate)
        assert br.main(_wf_argv(out / "wf", grid_file)) == 0
    for name in ("default.json", "default.md", "selection.json"):
        files[f"wf/{name}"] = out / "wf" / "walkforward" / name
    with monkeypatch.context() as m:
        m.chdir(tmp_path)
        m.setattr(br, "load_bars", OosLoadBars())
        sel = oos_selection(grid_file)
        sel_path = _write_json(tmp_path / "sel" / "selection.json", sel)
        assert br.main(_oos_argv(sel_path, tmp_path / "log.jsonl", out / "oos", grid_file)) == 0
    for ext in ("json", "md"):
        files[f"oos.{ext}"] = out / "oos" / "oos" / f"{sel['sha256'][:12]}.{ext}"
    return files


def test_off_outputs_byte_identical_to_pre_funding(tmp_path, monkeypatch, norm, grid_file):
    files = _off_outputs(tmp_path, monkeypatch, norm, grid_file)
    assert {k: _sha(v) for k, v in files.items()} == OFF_PINNED
    for f in files.values():
        assert "funding" not in f.read_text()  # meta·요약·선택 어디에도 펀딩 키 없음
    out = tmp_path / "off" / "base"
    assert not any("+funding" in str(f) for f in _files(tmp_path / "off"))
    bars = load_bars(D1, D2, SYM, out_dir=norm)
    _, params = load_grid(grid_file)
    for sid in ("syn-v1-h1", "syn-v1-h2"):
        got = pd.read_parquet(out / "roundtrips_net" / "default" / sid / f"{SPAN}.parquet")
        assert list(got.columns) == ROUNDTRIPS_NET.column_names
        parts = [apply_costs(generate_run(bars, p).roundtrips, "default")
                 for p in sorted(params, key=lambda q: q.param_id) if p.strategy_id == sid]
        want = pd.concat(parts, ignore_index=True).sort_values(br.RT_SORT_KEY, kind="stable", ignore_index=True)
        pdt.assert_frame_equal(got, validate_roundtrips_net(want))


# 기본 모드 ------------------------------------------------------------------------------------------

def test_default_mode_funding(norm, grid_file, fdir, tmp_path):
    on, off = tmp_path / "on", tmp_path / "off"
    assert br.main(_default_argv(norm, on, grid_file, "--funding", "--funding-dir", str(fdir))) == 0
    assert br.main(_default_argv(norm, off, grid_file)) == 0

    files = _files(on)
    assert files and all(f.parts[1] == FUNDING_LABEL for f in files)  # default/ 는 없다
    assert Path("summary", FUNDING_LABEL, f"{SPAN}.json") in files
    assert Path("summary", FUNDING_LABEL, f"{SPAN}.md") in files

    report = json.loads((on / "summary" / FUNDING_LABEL / f"{SPAN}.json").read_text())
    off_runs = {(r["strategy_id"], r["param_id"]): r
                for r in json.loads((off / "summary" / "default" / f"{SPAN}.json").read_text())["runs"]}
    funding = load_funding(SYM, START, END, fdir)
    assert report["meta"]["funding"] == {"source": str(funding_path(fdir, SYM)), "n_settlements": 6}
    assert len(funding) == 6

    bars = load_bars(D1, D2, SYM, out_dir=norm)
    _, params = load_grid(grid_file)
    ordered = sorted(params, key=lambda q: (q.strategy_id, q.param_id))
    runs = report["runs"]
    assert [(r["strategy_id"], r["param_id"]) for r in runs] == [(p.strategy_id, p.param_id) for p in ordered]
    nets = {}
    for p, got in zip(ordered, runs):
        net = apply_funding(apply_costs(generate_run(bars, p).roundtrips, "default"), funding, bars)
        nets.setdefault(p.strategy_id, []).append(net)
        assert set(got) == set(SUMMARY_KEYS) | {"n_funding", "total_funding_xbt"}
        want = summarize_net_run(net[ROUNDTRIPS_NET.column_names], START, END, resolve_gate(None))
        want["strategy_id"], want["param_id"] = p.strategy_id, p.param_id
        assert {k: got[k] for k in want} == json.loads(json.dumps(to_jsonable(want)))
        assert got["n_funding"] == int(net["n_funding"].sum())
        assert got["total_funding_xbt"] == pytest.approx(float(net["funding_xbt"].sum()), abs=1e-15)
    assert any(r["n_funding"] > 0 for r in runs)
    changed = [r for r in runs if r["n_funding"] > 0
               and r["total_net_ret"] != off_runs[(r["strategy_id"], r["param_id"])]["total_net_ret"]]
    assert changed  # 펀딩이 net 에 실제로 반영됐다

    for sid, parts in nets.items():
        got = validate_roundtrips_net_funding(
            pd.read_parquet(on / "roundtrips_net" / FUNDING_LABEL / sid / f"{SPAN}.parquet"))
        assert list(got.columns) == ROUNDTRIPS_NET_FUNDING.column_names
        pdt.assert_frame_equal(got, pd.concat(parts, ignore_index=True))

    md = (on / "summary" / FUNDING_LABEL / f"{SPAN}.md").read_text()
    assert "| gate | n_funding | total_funding_xbt |" in md
    assert "강제청산 미모델링(펀딩 반영 `--funding`)" in md and "펀딩·강제청산 미모델링" not in md
    assert "n_funding" not in (off / "summary" / "default" / f"{SPAN}.md").read_text()


@pytest.fixture
def no_bars(monkeypatch):
    """바 로더 spy(호출되면 기록 후 실패) — 펀딩 검사가 바 로드 전에 끝나는지 확인."""
    calls = []

    def spy(*a, **k):
        calls.append(a)
        raise AssertionError("바 로드 전에 거부돼야 한다")

    monkeypatch.setattr(br, "load_bars", spy)
    return calls


def test_default_missing_funding_file_exit_1(norm, grid_file, tmp_path, no_bars):
    out = tmp_path / "out"
    assert br.main(_default_argv(norm, out, grid_file, "--funding", "--funding-dir", str(tmp_path / "none"))) == 1
    assert _files(out) == [] and no_bars == []


def test_default_missing_settlement_in_range_exit_1(norm, grid_file, tmp_path, no_bars):
    fd = write_fund(tmp_path, drop=["2020-03-13 12:00"])
    out = tmp_path / "out"
    assert br.main(_default_argv(norm, out, grid_file, "--funding", "--funding-dir", str(fd))) == 1
    assert _files(out) == [] and no_bars == []


def test_default_missing_settlement_outside_range_ok(norm, grid_file, tmp_path):
    fd = write_fund(tmp_path, drop=["2020-03-14 04:00", "2020-03-11 20:00"])
    out = tmp_path / "out"
    assert br.main(_default_argv(norm, out, grid_file, "--funding", "--funding-dir", str(fd))) == 0
    assert (out / "summary" / FUNDING_LABEL / f"{SPAN}.json").is_file()


def test_default_off_grid_row_is_charged(norm, grid_file, fdir, tmp_path):
    base = tmp_path / "base"
    assert br.main(_default_argv(norm, base, grid_file, "--funding", "--funding-dir", str(fdir))) == 0
    rt = pd.read_parquet(base / "roundtrips_net" / FUNDING_LABEL / "syn-v1-h1" / f"{SPAN}.parquet")
    grid = set(funding_frame(START, END)["ts"])
    hold = rt[(rt["exit_ts"] - rt["entry_ts"]) >= pd.Timedelta(minutes=2)]
    off_ts = next(t for t in hold["entry_ts"] + pd.Timedelta(minutes=1) if t not in grid)

    fd = write_fund(tmp_path / "x", extra=[(off_ts, 0.01)])
    out = tmp_path / "out"
    assert br.main(_default_argv(norm, out, grid_file, "--funding", "--funding-dir", str(fd))) == 0
    rep = lambda o: json.loads((o / "summary" / FUNDING_LABEL / f"{SPAN}.json").read_text())  # noqa: E731
    assert rep(out)["meta"]["funding"]["n_settlements"] == 7
    assert sum(r["n_funding"] for r in rep(out)["runs"]) > sum(r["n_funding"] for r in rep(base)["runs"])


@pytest.mark.parametrize("mode", ["default", "walkforward", "oos"])
def test_funding_dir_without_funding_exit_2(mode, norm, grid_file, tmp_path):
    out = tmp_path / "out"
    extra = ("--funding-dir", str(tmp_path / "f"))
    if mode == "default":
        argv = _default_argv(norm, out, grid_file, *extra)
    elif mode == "walkforward":
        argv = _wf_argv(out, grid_file, *extra)
    else:
        argv = _oos_argv(tmp_path / "sel.json", tmp_path / "log.jsonl", out, grid_file, *extra)
    _exit2(argv)
    assert _files(out) == [] and not (tmp_path / "log.jsonl").exists()


def test_help_mentions_funding_options(capsys):
    with pytest.raises(SystemExit):
        br.main(["--help"])
    text = capsys.readouterr().out
    assert "--funding" in text and "--funding-dir" in text


# 워크포워드 ------------------------------------------------------------------------------------------

def test_walkforward_funding(wf_env, grid_file, fdir, tmp_path):
    on, off = tmp_path / "on", tmp_path / "off"
    assert br.main(_wf_argv(on, grid_file, "--funding", "--funding-dir", str(fdir))) == 0
    assert br.main(_wf_argv(off, grid_file)) == 0
    d = on / "walkforward"
    assert sorted(p.name for p in d.iterdir()) == ["default+funding.json", "default+funding.md",
                                                  "selection+funding.json"]

    sel = json.loads((d / "selection+funding.json").read_text())
    off_sel = json.loads((off / "walkforward" / "selection.json").read_text())
    assert sel["funding"] is True and sel["sha256"] == selection_sha256(sel)
    assert "funding" not in off_sel and sel["sha256"] != off_sel["sha256"]

    rep = json.loads((d / "default+funding.json").read_text(), parse_constant=pytest.fail)
    n_sample = len(funding_frame(SAMPLE_START, OOS_START))
    assert rep["meta"]["funding"] == {"source": str(funding_path(fdir, SYM)), "n_settlements": n_sample}
    assert rep["full_sample"]["selection"] == sel
    for name in br.PROFILE_NAMES:
        tests = [f["test"][name] for f in rep["folds"]]
        assert all({"n_funding", "total_funding_xbt"} <= set(t) for t in tests)
        st = rep["stitched"][name]
        assert st["n_funding"] == sum(t["n_funding"] for t in tests)
        assert st["total_funding_xbt"] == pytest.approx(sum(t["total_funding_xbt"] for t in tests), abs=1e-15)
    assert any(f["test"]["default"]["n_funding"] > 0 for f in rep["folds"])

    md = (d / "default+funding.md").read_text()
    assert "| 프로필 | n_trades | sharpe | mdd | total_net_ret | n_days | gate | total_funding_xbt |" in md
    assert "`selection+funding.json`" in md and "강제청산 미모델링(펀딩 반영 `--funding`)" in md
    assert "total_funding_xbt" not in (off / "walkforward" / "default.md").read_text()


def test_walkforward_missing_settlement_in_sample_exit_1(wf_env, grid_file, tmp_path):
    fd = write_fund(tmp_path, drop=["2019-01-01 04:00"])  # 표본 안·폴드(2020-03) 밖
    out = tmp_path / "out"
    assert br.main(_wf_argv(out, grid_file, "--funding", "--funding-dir", str(fd))) == 1
    assert _files(out) == [] and wf_env.calls == []  # 실행 전 검사


# OOS ------------------------------------------------------------------------------------------------

def _fsel(grid_file, funding):
    _, params = load_grid(grid_file)
    p = sorted(params, key=lambda q: (q.strategy_id, q.param_id))[0]
    return wf.make_selection({"start": "2018-03-01", "end": "2022-01-01", "strategy_id": p.strategy_id,
                              "param_id": p.param_id}, {"min_trades": 1, "min_sharpe": 0.0, "max_drawdown": 1.0},
                             funding=funding)


def test_oos_funding(oos_env, grid_file, fdir, tmp_path):
    sel = _fsel(grid_file, True)
    sel_path = _write_json(tmp_path / "wf" / "selection+funding.json", sel)
    _write_json(tmp_path / "wf" / "default+funding.json",
                {"full_sample": {"selection": {"sha256": sel["sha256"]}}, "dsr": {"var_sr": 0.0004},
                 "verdict": "pass"})
    log, out = tmp_path / "log.jsonl", tmp_path / "out"
    assert br.main(_oos_argv(sel_path, log, out, grid_file, "--funding", "--funding-dir", str(fdir))) == 0

    assert _files(out) == [Path("oos", f"{sel['sha256'][:12]}.json"), Path("oos", f"{sel['sha256'][:12]}.md")]
    rep = json.loads((out / "oos" / f"{sel['sha256'][:12]}.json").read_text(), parse_constant=pytest.fail)
    n_oos = len(funding_frame(OOS_START, OOS_END))
    assert rep["meta"]["funding"] == {"source": str(funding_path(fdir, SYM)), "n_settlements": n_oos}
    assert rep["selection"]["funding"] is True
    for name in br.PROFILE_NAMES:
        assert {"n_funding", "total_funding_xbt"} <= set(rep["results"][name])
    assert rep["results"]["default"]["n_funding"] == rep["results"]["bybit"]["n_funding"]
    assert rep["dsr"]["var_sr"] == 0.0004 and rep["dsr"]["source"].endswith("default+funding.json")
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["sha256"] == sel["sha256"]
    md = (out / "oos" / f"{sel['sha256'][:12]}.md").read_text()
    assert "| gate | total_funding_xbt |" in md and "강제청산 미모델링(펀딩 반영 `--funding`)" in md


@pytest.mark.parametrize("sel_funding, flag", [(True, False), (False, True)])
def test_oos_funding_mismatch_exit_2(oos_env, grid_file, fdir, tmp_path, sel_funding, flag):
    sel_path = _write_json(tmp_path / "wf" / "selection.json", _fsel(grid_file, sel_funding))
    log, out = tmp_path / "log.jsonl", tmp_path / "out"
    extra = ("--funding", "--funding-dir", str(fdir)) if flag else ()
    _exit2(_oos_argv(sel_path, log, out, grid_file, *extra))
    assert not log.exists() and _files(out) == [] and oos_env.calls == []


def test_oos_missing_settlement_exit_1_no_log(oos_env, grid_file, tmp_path):
    fd = write_fund(tmp_path, drop=["2023-06-01 12:00"])
    sel_path = _write_json(tmp_path / "wf" / "selection+funding.json", _fsel(grid_file, True))
    log, out = tmp_path / "log.jsonl", tmp_path / "out"
    assert br.main(_oos_argv(sel_path, log, out, grid_file, "--funding", "--funding-dir", str(fd))) == 1
    assert not log.exists() and _files(out) == [] and oos_env.calls == []


# 순차/병렬 바이트 동일(켬) -----------------------------------------------------------------------------

def test_default_mode_funding_jobs1_vs_jobs2_identical(norm, grid_file, fdir, tmp_path):
    o1, o2 = tmp_path / "o1", tmp_path / "o2"
    extra = ("--funding", "--funding-dir", str(fdir))
    assert br.main(_default_argv(norm, o1, grid_file, *extra, jobs=1)) == 0
    assert br.main(_default_argv(norm, o2, grid_file, *extra, jobs=2)) == 0
    files = _files(o1)
    assert files == _files(o2) and len([f for f in files if f.suffix == ".parquet"]) == 2
    for f in files:
        assert (o1 / f).read_bytes() == (o2 / f).read_bytes(), f
    rep = json.loads((o1 / "summary" / FUNDING_LABEL / f"{SPAN}.json").read_text())
    assert rep["meta"]["n_runs"] == 8 and any(r["n_funding"] > 0 for r in rep["runs"])


def test_walkforward_funding_jobs1_vs_jobs2_identical(wf_env, grid_file, fdir, tmp_path):
    outs = []
    for jobs in (1, 2):
        out = tmp_path / f"wf{jobs}"
        assert br.main(_wf_argv(out, grid_file, "--funding", "--funding-dir", str(fdir), jobs=jobs)) == 0
        outs.append(out / "walkforward")
    for name in ("default+funding.json", "default+funding.md", "selection+funding.json"):
        assert (outs[0] / name).read_bytes() == (outs[1] / name).read_bytes(), name
