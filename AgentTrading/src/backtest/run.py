"""Phase 3 백테스트 CLI(기본 모드): 정규화 1분봉 구간 → 그리드별 합성 라운드트립 → 비용 → net 지표·게이트 리포트.

`src.ingest.store.load_bars`(결측 일 → `MissingDaysError`) → run 마다 `synthetic.generate_run`
→ `costs.apply_costs` → `metrics.summarize_net_run`(게이트 판정 포함) 을 순서대로 부르는 얇은 오케스트레이터다.
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase3-backtest.md` "모듈"·"`run.py` 세부".
문서와 이 파일이 다르면 문서를 따른다. 그리드 JSON·JSON 변환·원자적 쓰기는 `src/analysis/run.py` 것을 쓴다.

    python -m src.backtest.run --start 2019-06-01 --end 2019-06-08 --symbol XBTUSD
    python -m src.backtest.run --start 2020-03-01 --end 2020-04-01 --grid grid.json --fee-profile bybit
    python -m src.backtest.run --walkforward --grid grid.json

| 출력 | 경로 |
|---|---|
| net 라운드트립 | `<out>/roundtrips_net/<fee_profile>/<strategy_id>/<YYYYMMDD>_<YYYYMMDD>.parquet` |
| 요약 | `<out>/summary/<fee_profile>/<YYYYMMDD>_<YYYYMMDD>.json` + 같은 이름 `.md` |
| 워크포워드(`--walkforward`) | `<out>/walkforward/default.json` + `.md`, 선택이 있으면 `<out>/walkforward/selection.json` |

- 구간은 반열린 `[start, end)` UTC 자정 — `--end` 날짜는 포함하지 않는다(분석 CLI 의 종료일 포함과 다름).
  파일명의 두 번째 날짜도 반열린 end 다.
- 기본 표본 [2018-03-01, 2022-01-01) 밖·OOS·2025 이후·`start ≥ end` 는 데이터를 읽기 전에 거부(종료코드 2).
- 결측 일 등 실행 중 예외는 로그 후 종료코드 1. net 라운드트립은 run 단위로 트리거별 `<parquet>.tmp` 에 이어 쓰고
  (`RoundtripSink`, 메모리 상한 = 행 그룹 버퍼 + run 1개), 전체 성공 후에야 rename → JSON·MD 를 쓴다.
  실패하면 이번 실행의 `.tmp` 를 지우므로 최종 산출물이 없다.
- 게이트 기준은 코드 기본값(`walkforward.resolve_gate(None)`, Spec 3절)을 meta 에 기록한다. 여기서의 판정은
  단일 구간 3기준이며 최종 판정이 아니다(워크포워드·DSR 은 `--walkforward`).
- `--walkforward`: 기본 표본 [2018-03-01, 2022-01-01) 고정 6폴드(`make_folds()`)로 `run_walkforward` 실행.
  `--start`/`--end` 와 함께 쓰면 거부(종료코드 2), `--fee-profile` 은 `default` 만(판정 default·민감도 bybit 를
  한 리포트에 담는다). 엔진이 끝까지 성공한 뒤에만 JSON → MD → selection.json 을 원자적으로 쓴다. 선택이 없으면
  selection.json 을 쓰지 않고 이전 실행의 낡은 파일을 지운다. 실패(종료코드 1)·거부(2)면 기존 산출물은 그대로.
- 결정성: 실행 시각·소요 시간은 파일에 넣지 않는다. 같은 입력이면 JSON·MD 바이트가 같다.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.analysis.run import _cell, _span, _write_text_atomic, load_grid, to_jsonable
from src.analysis.synthetic import RULESET_VERSION, Params, generate_run
from src.backtest import costs
from src.backtest.metrics import summarize_net_run
from src.backtest.walkforward import (N_TRIALS, OOS_START, SAMPLE_START, WALKFORWARD_PARAM_ID,
                                      WALKFORWARD_STRATEGY_ID, Fold, check_sample_range, deflated_sharpe,
                                      expected_max_sr, make_folds, make_selection, resolve_gate,
                                      select_params, sr_variance, stitch_test_roundtrips,
                                      walkforward_verdict)
from src.ingest import normalize
from src.ingest.store import load_bars
from src.shared.schema import ROUNDTRIPS_NET, empty_frame, validate_roundtrips_net

log = logging.getLogger(__name__)

DEFAULT_OUT_DIR = Path("data/out/backtest")
PROGRESS_EVERY = 100
ROW_GROUP_ROWS = 100_000  # 트리거별 버퍼가 이 행 수에 닿으면 행 그룹 하나로 flush
RT_SORT_KEY = ["strategy_id", "param_id", "trade_id"]
MD_COLUMNS = ("strategy_id", "param_id", "n_trades", "sharpe", "mdd", "total_net_ret", "total_gross_ret",
              "n_liq_breach", "gate")


class RoundtripSink:
    """트리거별 net 라운드트립을 run 단위로 `<parquet>.tmp` 에 이어 쓰고, `commit()` 에서 일괄 rename.

    `write(sid, df)` 는 `(strategy_id, param_id, trade_id)` 정렬 순서로 불려야 한다(같은 sid 는 연속).
    동시에 열린 writer 는 1개, 버퍼는 `ROW_GROUP_ROWS` 행. 0행 df 만 받은 트리거는 0행 parquet 가 된다.
    with 블록에서 예외가 나거나 commit 전에 빠져나오면 `abort()` 가 이번 실행의 `.tmp` 를 모두 지운다.
    """

    def __init__(self, paths: Mapping[str, Path]):
        self.paths = dict(paths)
        self.schema = pa.Schema.from_pandas(empty_frame(ROUNDTRIPS_NET), preserve_index=False)
        self._tmps: dict[str, Path] = {}
        self._sid: str | None = None
        self._writer: pq.ParquetWriter | None = None
        self._buf: list[pa.Table] = []
        self._buf_rows = 0
        self._committed = False

    def write(self, sid: str, df: pd.DataFrame) -> None:
        if sid != self._sid:
            if sid in self._tmps:
                raise ValueError(f"strategy_id {sid!r} 가 연속되지 않는다(정렬 순서로 써야 한다)")
            self._close()
            tmp = self.paths[sid].with_name(self.paths[sid].name + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            self._tmps[sid] = tmp
            self._writer = pq.ParquetWriter(tmp, self.schema, compression="snappy")
            self._sid = sid
        if len(df):
            validate_roundtrips_net(df)
            self._buf.append(pa.Table.from_pandas(df, schema=self.schema, preserve_index=False))
            self._buf_rows += len(df)
            if self._buf_rows >= ROW_GROUP_ROWS:
                self._flush()

    def _flush(self) -> None:
        if self._buf:
            self._writer.write_table(pa.concat_tables(self._buf))
            self._buf, self._buf_rows = [], 0

    def _close(self) -> None:
        if self._writer is not None:
            try:
                self._flush()
            finally:
                self._writer.close()
                self._writer, self._sid = None, None

    def commit(self) -> None:
        self._close()
        for sid, tmp in self._tmps.items():
            os.replace(tmp, self.paths[sid])
        self._committed = True

    def abort(self) -> None:
        self._buf, self._buf_rows = [], 0
        try:
            if self._writer is not None:
                self._writer.close()
        finally:
            self._writer, self._sid = None, None
            for tmp in self._tmps.values():
                tmp.unlink(missing_ok=True)

    def __enter__(self) -> RoundtripSink:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None or not self._committed:
            self.abort()


def run_grid(bars: pd.DataFrame, params_list: Sequence[Params], fee_profile: str | Mapping,
             start: date, end: date, gate: Mapping | None, sink) -> list[dict]:
    """run 별 생성 → 비용 → `sink.write(strategy_id, net)` → 요약. (strategy_id, param_id) 정렬 요약 목록을 돌려준다.

    `[start, end)` 반열린 구간. params 를 (strategy_id, param_id) 로 stable 정렬해 실행하고 run 내 `trade_id` 가
    0..n-1 오름차순이므로, 싱크가 받는 순서가 곧 `RT_SORT_KEY` 순서다. 거래 0건 run 도 0행 프레임을 넘긴다
    (실행한 트리거는 0행 parquet 라도 생긴다). net 프레임은 넘긴 뒤 버린다 — 누적은 싱크의 책임.
    """
    ordered = sorted(params_list, key=lambda p: (p.strategy_id, p.param_id))
    summaries = []
    t0 = time.monotonic()
    for i, p in enumerate(ordered, 1):
        res = generate_run(bars, p)
        net = costs.apply_costs(res.roundtrips, fee_profile)
        sink.write(p.strategy_id, net)
        summary = summarize_net_run(net, start, end, gate)
        summary["strategy_id"] = p.strategy_id  # 0건 run 은 metrics 가 None 으로 둔다
        summary["param_id"] = p.param_id
        summaries.append(summary)
        del res, net
        if i % PROGRESS_EVERY == 0 or i == len(ordered):
            log.info("run %d/%d (%.1fs)", i, len(ordered), time.monotonic() - t0)
    summaries.sort(key=lambda s: (s["strategy_id"], s["param_id"]))
    return summaries


# 워크포워드 ------------------------------------------------------------------------------------------

PROFILE_NAMES = ("default", "bybit")  # default = 판정, bybit = 민감도(설계 "워크포워드 검증 곡선과 판정")


class _DiscardSink:
    """net 프레임을 저장하지 않는 싱크(학습·표본 전체 그리드는 요약만 필요)."""

    def write(self, sid: str, df: pd.DataFrame) -> None:
        pass


def _fmt_day(t: pd.Timestamp) -> str:
    return t.strftime("%Y-%m-%d")


def _load_range(load_bars: Callable, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """주입 로더로 `[start, end)` 바를 읽고, 구간 밖 `ts`(누수)·0행이면 `ValueError`."""
    bars = load_bars(start, end)
    if len(bars) == 0:
        raise ValueError(f"[{_fmt_day(start)}, {_fmt_day(end)}) 바가 0행이다")
    ts = bars["ts"]
    if bool((ts < start).any()) or bool((ts >= end).any()):
        raise ValueError(f"로더가 [{_fmt_day(start)}, {_fmt_day(end)}) 밖 바를 돌려줬다(학습·검증 누수 방지)")
    return bars


def _check_folds(folds: Sequence[Fold], sample: tuple[pd.Timestamp, pd.Timestamp]) -> None:
    """각 구간 표본 가드 통과, 학습 end ≤ 검증 start, 검증 구간 오름차순·빈틈/겹침 없음, 모두 `sample` 안."""
    if not folds:
        raise ValueError("폴드가 없다")
    s, e = sample
    prev_end = None
    for i, f in enumerate(folds, 1):
        ts, te = check_sample_range(f.train_start, f.train_end)
        vs, ve = check_sample_range(f.test_start, f.test_end)
        if te > vs:
            raise ValueError(f"폴드 {i}: 학습 구간이 검증 구간과 겹친다")
        if ts < s or ve > e:
            raise ValueError(f"폴드 {i}: 표본 [{_fmt_day(s)}, {_fmt_day(e)}) 밖 구간")
        if prev_end is not None and vs != prev_end:
            raise ValueError(f"폴드 {i}: 검증 구간이 앞 폴드에 이어지지 않는다(빈틈·겹침·역순)")
        prev_end = ve


def representative_var(summaries: Sequence[Mapping], params_list: Sequence[Params]) -> dict:
    """DSR 의 V: `risk_pct == 1` 대표 run 요약의 `sr_daily` 표본 분산(ddof=1, NaN 제외).

    그리드에 `risk_pct = 1` run 이 없거나 유한값이 2개 미만이면 `var_sr` = NaN → dsr NaN → 판정 미달.
    """
    keys = {(p.strategy_id, p.param_id) for p in params_list if p.risk_pct == 1.0}
    rep = [s for s in summaries if (s["strategy_id"], s["param_id"]) in keys]
    finite = sum(1 for s in rep if math.isfinite(float(s["sr_daily"])))
    return {"var_sr": sr_variance(rep), "n_runs": len(rep), "n_finite": finite}


def _with_run_key(summary: dict, sid, pid) -> dict:
    summary["strategy_id"], summary["param_id"] = sid, pid  # 0건 run 은 metrics 가 None 으로 둔다
    return summary


def run_walkforward(folds: Sequence[Fold], load_bars: Callable, params_list: Sequence[Params],
                    gate: Mapping | None = None, sample=(SAMPLE_START, OOS_START),
                    n_trials: int = N_TRIALS) -> dict:
    """폴드별 학습 그리드(`default`) → `select_params` → 검증 실행(`default`·`bybit`) → 이어 붙인 곡선·DSR 리포트.

    `load_bars(start, end)` 는 반열린 `[start, end)` UTC 자정 Timestamp 를 받아 1분봉을 돌려주는 주입 함수다.
    학습 구간을 먼저 읽고 선택이 끝난 뒤에만 검증 구간을 읽는다. 선택 없음 폴드는 검증 바를 읽지 않고
    거래 0·현금 보유로 요약한다. V 는 표본 전체 `risk_pct = 1` run 기준, `selection` 은 `sample` 이 기본 표본
    전체일 때만 `make_selection` 결과(그 외 `None`). 반환 dict 의 NaN 은 float 그대로(JSON 변환은 호출자).
    """
    g = resolve_gate(gate)
    s, e = check_sample_range(*sample)
    _check_folds(folds, (s, e))
    by_key = {(p.strategy_id, p.param_id): p for p in params_list}

    fold_reports = []
    test_nets: dict[str, list] = {name: [] for name in PROFILE_NAMES}
    for i, f in enumerate(folds, 1):
        bars = _load_range(load_bars, f.train_start, f.train_end)
        train = run_grid(bars, params_list, "default", f.train_start, f.train_end, g, _DiscardSink())
        del bars
        sel = select_params(train, g)
        test = {}
        if sel is None:
            log.info("폴드 %d: 선택 없음 → 현금 보유", i)
            selection = None
            empty = empty_frame(ROUNDTRIPS_NET)
            for name in PROFILE_NAMES:
                test[name] = summarize_net_run(empty, f.test_start, f.test_end, g)
                test_nets[name].append(None)
        else:
            p = by_key[(sel["strategy_id"], sel["param_id"])]
            selection = {"strategy_id": p.strategy_id, "param_id": p.param_id, "train_sharpe": sel["sharpe"],
                         "train_n_trades": sel["n_trades"], "train_mdd": sel["mdd"]}
            bars = _load_range(load_bars, f.test_start, f.test_end)
            res = generate_run(bars, p)
            del bars
            for name in PROFILE_NAMES:
                net = costs.apply_costs(res.roundtrips, name)
                test[name] = _with_run_key(summarize_net_run(net, f.test_start, f.test_end, g),
                                           p.strategy_id, p.param_id)
                test_nets[name].append(net)
            del res
        fold_reports.append({
            "train_start": _fmt_day(f.train_start), "train_end": _fmt_day(f.train_end),
            "test_start": _fmt_day(f.test_start), "test_end": _fmt_day(f.test_end),
            "selection": selection, "test": test,
        })

    eval_start, eval_end = folds[0].test_start, folds[-1].test_end
    stitched = {}
    for name in PROFILE_NAMES:
        rt = stitch_test_roundtrips(test_nets[name])
        stitched[name] = _with_run_key(summarize_net_run(rt, eval_start, eval_end, g),
                                       WALKFORWARD_STRATEGY_ID, WALKFORWARD_PARAM_ID)
    del test_nets

    bars = _load_range(load_bars, s, e)
    full = run_grid(bars, params_list, "default", s, e, g, _DiscardSink())
    del bars
    rv = representative_var(full, params_list)
    full_sel = select_params(full, g)
    selection_file = make_selection(full_sel, g) if full_sel is not None and (s, e) == (SAMPLE_START, OOS_START) \
        else None

    dsr = {"var_sr": rv["var_sr"], "n_var_runs": rv["n_runs"], "n_var_finite": rv["n_finite"],
           "sr0": expected_max_sr(int(n_trials), rv["var_sr"])}
    verdicts = {}
    for name in PROFILE_NAMES:
        st = stitched[name]
        dsr[name] = deflated_sharpe(st["sr_daily"], n_trials, rv["var_sr"], st["n_days"], st["skew_daily"],
                                    st["kurt_daily"])
        verdicts[name] = walkforward_verdict(st, dsr[name], g)

    return {
        "gate": g, "n_trials": int(n_trials), "sample": [_fmt_day(s), _fmt_day(e)], "n_runs": len(params_list),
        "folds": fold_reports, "stitched": stitched, "dsr": dsr,
        "verdict": verdicts["default"], "bybit_verdict": verdicts["bybit"],
        "full_sample": {"selected": full_sel, "selection": selection_file},
    }


def store_loader(symbol: str, data_dir: Path) -> Callable:
    """반열린 `[start, end)` UTC 자정 Timestamp → `store.load_bars(start, end − 1일)`(store 는 종료일 포함)."""

    def load(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        return load_bars(start.date(), (end - pd.Timedelta(days=1)).date(), symbol, out_dir=data_dir)

    return load


def walkforward_paths(out_dir: Path) -> dict[str, Path]:
    d = out_dir / "walkforward"
    return {"json": d / "default.json", "md": d / "default.md", "selection": d / "selection.json"}


def build_walkforward_report(engine_report: Mapping, symbol: str, axes: dict) -> dict:
    meta = {
        "symbol": symbol, "ruleset_version": RULESET_VERSION, "grid": axes, "n_runs": engine_report["n_runs"],
        "fee": {name: costs.PROFILES[name] for name in PROFILE_NAMES}, "judge_profile": "default",
    }
    return to_jsonable({"meta": meta, **engine_report})


WF_FOLD_COLUMNS = ("폴드", "학습", "검증", "선택", "학습 Sharpe", "default 거래", "default Sharpe", "default MDD",
                   "default net", "bybit net")
WF_CURVE_COLUMNS = ("프로필", "n_trades", "sharpe", "mdd", "total_net_ret", "n_days", "gate")


def _row(cells) -> str:
    return "| " + " | ".join(cells) + " |"


def render_walkforward_markdown(report: dict) -> str:
    """워크포워드 JSON 리포트 → 폴드 표·검증 곡선 3기준·DSR·최종 판정·bybit 민감도(사람이 읽을 용도)."""
    m, g, dsr = report["meta"], report["gate"], report["dsr"]
    s0, s1 = report["sample"]
    sel_file = report["full_sample"]["selection"]
    lines = [
        f"# 워크포워드 {m['symbol']} [{s0}, {s1}) · 판정 {m['judge_profile']}",
        "",
        f"- ruleset `{m['ruleset_version']}` · run {m['n_runs']}개 · 폴드 {len(report['folds'])}개"
        f" · DSR 시행 수 N = {report['n_trials']}",
        f"- 게이트 기준: 거래 ≥ {g['min_trades']} · Sharpe ≥ {g['min_sharpe']} · MDD ≤ {g['max_drawdown']}"
        f" · DSR ≥ {g['min_dsr']}",
        "- 단위: mdd·net 비율(0.01 = 1%), sharpe 연환산(√365). 학습 선택·판정은 default, bybit 는 민감도.",
        "",
        "## 폴드",
        "",
        _row(WF_FOLD_COLUMNS),
        "|" + "---|" * len(WF_FOLD_COLUMNS),
    ]
    for i, f in enumerate(report["folds"], 1):
        sel, d, b = f["selection"], f["test"]["default"], f["test"]["bybit"]
        chosen = f"{sel['strategy_id']} / {sel['param_id']}" if sel else "선택 없음(현금)"
        lines.append(_row([
            str(i), f"[{f['train_start']}, {f['train_end']})", f"[{f['test_start']}, {f['test_end']})",
            _cell(chosen), _cell(sel["train_sharpe"] if sel else None), _cell(d["n_trades"]), _cell(d["sharpe"]),
            _cell(d["mdd"]), _cell(d["total_net_ret"]), _cell(b["total_net_ret"]),
        ]))
    lines += [
        "",
        "## 검증 곡선 3기준 (폴드 검증 구간 이어 붙임)",
        "",
        _row(WF_CURVE_COLUMNS),
        "|" + "---|" * len(WF_CURVE_COLUMNS),
    ]
    for name in PROFILE_NAMES:
        st = report["stitched"][name]
        lines.append(_row([name] + [_cell(st.get(c)) for c in WF_CURVE_COLUMNS[1:]]))
    lines += [
        "",
        "## DSR",
        "",
        f"- V(표본 전체 `risk_pct = 1` 대표 run sr_daily 분산) = {_cell(dsr['var_sr'])}"
        f" (대표 run {dsr['n_var_runs']}개, 유한 {dsr['n_var_finite']}개) · SR0 = {_cell(dsr['sr0'])}",
        f"- DSR default = {_cell(dsr['default'])} · bybit = {_cell(dsr['bybit'])} (기준 ≥ {g['min_dsr']})",
        "",
        "## 최종 판정",
        "",
        f"- **{report['verdict']}** (default: 검증 곡선 3기준 + DSR)",
        "",
        "## bybit 민감도",
        "",
        f"- {report['bybit_verdict']} (DSR {_cell(dsr['bybit'])}) — 판정에 쓰지 않는다",
        "",
        "## 선택 파일",
        "",
    ]
    if sel_file:
        lines.append(f"- `selection.json`: {sel_file['strategy_id']} / {sel_file['param_id']}"
                     f" · sha256 `{sel_file['sha256'][:12]}`")
    else:
        lines.append("- 선택 없음 — selection.json 미작성, OOS 진행 불가")
    lines += [
        "",
        "- ⚠ 펀딩·강제청산 미모델링. 실거래 전환은 사람이 판단한다.",
    ]
    return "\n".join(lines) + "\n"


def _json_text(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def write_walkforward_outputs(paths: Mapping[str, Path], report: dict) -> None:
    """JSON → MD → (선택이 있으면) selection.json 원자적 쓰기. 선택이 없으면 낡은 selection.json 을 지운다."""
    _write_text_atomic(_json_text(report), paths["json"])
    _write_text_atomic(render_walkforward_markdown(report), paths["md"])
    sel = report["full_sample"]["selection"]
    if sel is not None:
        _write_text_atomic(_json_text(sel), paths["selection"])
    elif paths["selection"].exists():
        log.warning("선택 없음 → 이전 실행의 %s 를 지운다(리포트와 어긋난 선택 방지)", paths["selection"])
        paths["selection"].unlink()


def build_report(start: date, end: date, symbol: str, bars: pd.DataFrame, axes: dict, fee_profile: str,
                 gate: dict, summaries: list[dict]) -> dict:
    n = len(bars)
    verdicts = [s["gate"] for s in summaries]
    meta = {
        "start": start, "end": end, "interval": f"[{start}, {end})", "symbol": symbol,
        "ruleset_version": RULESET_VERSION, "n_bars": n,
        "first_ts": bars["ts"].iat[0] if n else None,
        "last_ts": bars["ts"].iat[-1] if n else None,
        "grid": axes, "n_runs": len(summaries),
        "fee_profile": fee_profile, "fee": costs.PROFILES[fee_profile], "gate": gate,
        "n_pass": verdicts.count("pass"), "n_fail": verdicts.count("fail"),
        "n_insufficient": verdicts.count("insufficient"),
    }
    return to_jsonable({"meta": meta, "runs": summaries})


def render_markdown(report: dict) -> str:
    """JSON 리포트 → run 별 핵심 지표 표와 통과 run 수(사람이 읽을 용도)."""
    m = report["meta"]
    g, f = m["gate"], m["fee"]
    lines = [
        f"# 백테스트 {m['symbol']} [{m['start']}, {m['end']}) · {m['fee_profile']}",
        "",
        f"- ruleset `{m['ruleset_version']}` · 1분봉 {m['n_bars']}개 ({m['first_ts']} ~ {m['last_ts']})"
        f" · run {m['n_runs']}개",
        f"- 수수료 프로필 `{m['fee_profile']}`: taker {f['taker_fee']} · maker {f['maker_fee']}"
        f" · 슬리피지 {f['slippage_bps']}bp",
        f"- 게이트 기준: 거래 ≥ {g['min_trades']} · Sharpe ≥ {g['min_sharpe']} · MDD ≤ {g['max_drawdown']}"
        f" (워크포워드 DSR ≥ {g['min_dsr']})",
        f"- **통과 run {m['n_pass']}/{m['n_runs']}** (fail {m['n_fail']} · insufficient {m['n_insufficient']})",
        "- ⚠ 단일 구간 3기준 판정이다. 최종 판정 아님 — 워크포워드·DSR 필요. 펀딩·강제청산 미모델링.",
        "- 단위: mdd·total_net_ret·total_gross_ret 비율(0.01 = 1%), sharpe 연환산(√365).",
        "",
        "| " + " | ".join(MD_COLUMNS) + " |",
        "|" + "---|" * len(MD_COLUMNS),
    ]
    for r in report["runs"]:
        lines.append("| " + " | ".join(_cell(r.get(c)) for c in MD_COLUMNS) + " |")
    return "\n".join(lines) + "\n"


def output_paths(out_dir: Path, fee_profile: str, start: date, end: date, strategy_ids) -> dict[str, Path]:
    span = _span(start, end)
    paths = {sid: out_dir / "roundtrips_net" / fee_profile / sid / f"{span}.parquet" for sid in strategy_ids}
    paths["json"] = out_dir / "summary" / fee_profile / f"{span}.json"
    paths["md"] = out_dir / "summary" / fee_profile / f"{span}.md"
    return paths


def write_outputs(paths: Mapping[str, Path], report: dict) -> None:
    """요약 JSON·MD 를 원자적으로 쓴다(net 라운드트립 parquet 는 `RoundtripSink` 가 쓴다)."""
    text = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    _write_text_atomic(text, paths["json"])
    _write_text_atomic(render_markdown(report), paths["md"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.backtest.run",
        description="정규화 1분봉 [start, end) → 합성 전략 그리드 → 비용 → net 지표·게이트 리포트 (Phase 3)")
    p.add_argument("--start", type=normalize._parse_date, help="YYYY-MM-DD (포함, UTC). 기본 모드에서 필수")
    p.add_argument("--end", type=normalize._parse_date,
                   help="YYYY-MM-DD (미포함, UTC — 반열린 [start, end)). 기본 모드에서 필수")
    p.add_argument("--symbol", default="XBTUSD")
    p.add_argument("--grid", type=Path, default=None,
                   help="축 → 값 목록 JSON (설계 그리드 부분집합, 생략 시 전체 2,268 run)")
    p.add_argument("--fee-profile", default="default", choices=sorted(costs.PROFILES),
                   help="수수료 프로필 (기본 default)")
    p.add_argument("--data-dir", type=Path, default=normalize.DEFAULT_OUT_DIR,
                   help=f"정규화 데이터 디렉터리 (기본 {normalize.DEFAULT_OUT_DIR})")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help=f"출력 디렉터리 (기본 {DEFAULT_OUT_DIR})")
    p.add_argument("--walkforward", action="store_true",
                   help="기본 표본 고정 6폴드 워크포워드 → walkforward/default.json·md + selection.json"
                        " (--start/--end 불가, --fee-profile default 만)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.walkforward:
        if a.start is not None or a.end is not None:
            parser.error("--walkforward 는 기본 표본 [2018-03-01, 2022-01-01) 고정 폴드라 --start/--end 를 받지 않는다")
        if a.fee_profile != "default":
            parser.error("--walkforward 는 --fee-profile default 만 받는다(bybit 민감도는 리포트에 함께 담긴다)")
    else:
        if a.start is None or a.end is None:
            parser.error("--start 와 --end 가 필요하다(또는 --walkforward)")
        try:
            check_sample_range(a.start, a.end)
        except ValueError as e:
            parser.error(str(e))
    try:
        axes, params_list = load_grid(a.grid)
    except (OSError, ValueError) as e:  # json.JSONDecodeError 는 ValueError
        parser.error(f"--grid: {e}")
    if a.walkforward:
        return _main_walkforward(a, axes, params_list)
    gate = resolve_gate(None)

    t0 = time.monotonic()
    try:
        bars = load_bars(a.start, a.end - timedelta(days=1), a.symbol, out_dir=a.data_dir)  # store 는 종료일 포함
        log.info("1분봉 %d개 로드, run %d개 실행 (fee %s)", len(bars), len(params_list), a.fee_profile)
        paths = output_paths(a.out, a.fee_profile, a.start, a.end, sorted({p.strategy_id for p in params_list}))
        with RoundtripSink(paths) as sink:
            summaries = run_grid(bars, params_list, a.fee_profile, a.start, a.end, gate, sink)
            report = build_report(a.start, a.end, a.symbol, bars, axes, a.fee_profile, gate, summaries)
            sink.commit()
        write_outputs(paths, report)
    except Exception:
        log.exception("백테스트 실패")
        return 1
    log.info("완료 (%.1fs): %s", time.monotonic() - t0, paths["json"])
    return 0


def _main_walkforward(a: argparse.Namespace, axes: dict, params_list: Sequence[Params]) -> int:
    t0 = time.monotonic()
    paths = walkforward_paths(a.out)
    try:
        engine = run_walkforward(make_folds(), store_loader(a.symbol, a.data_dir), params_list,
                                 gate=resolve_gate(None))
        report = build_walkforward_report(engine, a.symbol, axes)
        write_walkforward_outputs(paths, report)
    except Exception:
        log.exception("워크포워드 실패")
        return 1
    log.info("완료 (%.1fs): %s 판정 %s", time.monotonic() - t0, paths["json"], report["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
