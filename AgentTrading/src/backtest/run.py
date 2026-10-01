"""Phase 3 백테스트 CLI(기본 모드): 정규화 1분봉 구간 → 그리드별 합성 라운드트립 → 비용 → net 지표·게이트 리포트.

`src.ingest.store.load_bars`(결측 일 → `MissingDaysError`) → run 마다 `synthetic.generate_run`
→ `costs.apply_costs` → `metrics.summarize_net_run`(게이트 판정 포함) 을 순서대로 부르는 얇은 오케스트레이터다.
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase3-backtest.md` "모듈"·"`run.py` 세부".
문서와 이 파일이 다르면 문서를 따른다. 그리드 JSON·JSON 변환·원자적 쓰기는 `src/analysis/run.py` 것을 쓴다.

    python -m src.backtest.run --start 2019-06-01 --end 2019-06-08 --symbol XBTUSD
    python -m src.backtest.run --start 2020-03-01 --end 2020-04-01 --grid grid.json --fee-profile bybit
    python -m src.backtest.run --walkforward --grid grid.json
    python -m src.backtest.run --oos-final data/out/backtest/walkforward/selection.json --grid grid.json
    python -m src.backtest.run --start 2019-06-01 --end 2019-06-08 --funding
    python -m src.backtest.run --walkforward --grid grid.json --funding --funding-dir data/raw/normalized/bitmex/funding

| 출력 | 경로 |
|---|---|
| net 라운드트립 | `<out>/roundtrips_net/<fee_profile>/<strategy_id>/<YYYYMMDD>_<YYYYMMDD>.parquet` |
| 요약 | `<out>/summary/<fee_profile>/<YYYYMMDD>_<YYYYMMDD>.json` + 같은 이름 `.md` |
| 워크포워드(`--walkforward`) | `<out>/walkforward/default.json` + `.md`, 선택이 있으면 `<out>/walkforward/selection.json` |
| OOS 1회(`--oos-final PATH`) | `<out>/oos/<sha256 앞 12자>.json` + `.md`, 접근 로그 `--oos-log`(기본 `data/backtest/oos_access.jsonl`) 한 줄 |
| 펀딩 반영(`--funding`) | 위 `<fee_profile>` → `<fee_profile>+funding`, `walkforward/default+funding.json`·`.md`·`selection+funding.json`, OOS 는 경로 그대로(sha256 이 달라 분리) |

- 구간은 반열린 `[start, end)` UTC 자정 — `--end` 날짜는 포함하지 않는다(분석 CLI 의 종료일 포함과 다름).
  파일명의 두 번째 날짜도 반열린 end 다.
- 기본 표본 [2018-03-01, 2022-01-01) 밖·OOS·2025 이후·`start ≥ end` 는 데이터를 읽기 전에 거부(종료코드 2).
- 결측 일 등 실행 중 예외는 로그 후 종료코드 1. net 라운드트립은 run 단위로 트리거별 `<parquet>.tmp` 에 이어 쓰고
  (`RoundtripSink` ← `src/shared/sink.py`, 메모리 상한 = 행 그룹 버퍼 + run 1개), 전체 성공 후에야 rename → JSON·MD 를 쓴다.
  실패하면 이번 실행의 `.tmp` 를 지우므로 최종 산출물이 없다.
- 게이트 기준은 코드 기본값(`walkforward.resolve_gate(None)`, Spec 3절)을 meta 에 기록한다. 여기서의 판정은
  단일 구간 3기준이며 최종 판정이 아니다(워크포워드·DSR 은 `--walkforward`).
- `--walkforward`: 기본 표본 [2018-03-01, 2022-01-01) 고정 6폴드(`make_folds()`)로 `run_walkforward` 실행.
  `--start`/`--end` 와 함께 쓰면 거부(종료코드 2), `--fee-profile` 은 `default` 만(판정 default·민감도 bybit 를
  한 리포트에 담는다). 엔진이 끝까지 성공한 뒤에만 JSON → MD → selection.json 을 원자적으로 쓴다. 선택이 없으면
  selection.json 을 쓰지 않고 이전 실행의 낡은 파일을 지운다. 실패(종료코드 1)·거부(2)면 기존 산출물은 그대로.
- `--oos-final PATH`: 선택 파일 검증·접근 로그 검사(`authorize_oos`, 쓰기 없음) → OOS [2022-01-01, 2025-01-01)
  선택 run 1개(`default` 판정·`bybit` 병기) → `evaluate_oos`(접근 로그 append, 유일한 로그 쓰기) → JSON → MD.
  선택·로그·그리드 문제는 데이터를 읽기 전에 종료코드 2, 로드·실행 실패는 1(로그 미기록). `--walkforward`·
  `--start`/`--end` 와 함께 쓰면 2, `--fee-profile` 은 `default` 만. `--oos-log` 는 `--oos-final` 과만 쓴다.
  로그를 지우거나 덮어쓰는 코드는 없다 — 가드 해제는 사람 판단. DSR(N=756)은 참고값이며 판정에 넣지 않는다.
- 결정성: 실행 시각·소요 시간은 파일에 넣지 않는다. 같은 입력이면 JSON·MD 바이트가 같다.
- `--jobs N`(기본 1 = 순차): N ≥ 2 면 그리드 run 을 spawn 프로세스 풀에서 계산한다(bars 는 워커 initializer 로
  워커당 1회 전달). 결과는 정렬된 run 순서로 받아(미완료 상한 `WINDOW_PER_JOB × N`) 싱크 쓰기는 메인만 하므로
  산출물은 N 과 무관하게 같고, 메모리 상한 = 행 그룹 버퍼 + 2N run + 워커당 bars 사본. `--oos-final` 은 순차.
- `--funding [--funding-dir PATH]`(기본 끔, 설계 "펀딩 모델" 5·8·9·10항): 실행 전 평가 구간(기본 `[start, end)`,
  `--walkforward` 기본 표본, `--oos-final` OOS) 펀딩 파일 부재·격자(04·12·20 UTC) 결측 → 종료코드 1·산출물 없음.
  모든 run 이 `apply_costs` 뒤 `costs.apply_funding`(net 이 펀딩 포함으로 갱신)을 거치고, 요약은 `ROUNDTRIPS_NET`
  32열 투영본으로 계산한 뒤 `n_funding`·`total_funding_xbt` 합을 붙인다. 리포트 `meta.funding`·MD 펀딩 열,
  선택 파일 `funding: true`(sha256 이 달라짐). `--oos-final` 은 선택 `funding` 과 `--funding` 이 다르면 2.
  끄면 모든 산출물이 펀딩 도입 전과 바이트 동일하다(키·열·경로를 켰을 때만 만든다).
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import multiprocessing
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.analysis.run import _cell, _span, _write_text_atomic, load_grid, to_jsonable
from src.analysis.synthetic import RULESET_VERSION, Params, generate_run
from src.backtest import costs, walkforward
from src.backtest.metrics import summarize_net_run
from src.backtest.walkforward import (N_TRIALS, OOS_END, OOS_LOG_PATH, OOS_START, SAMPLE_START, WALKFORWARD_PARAM_ID,
                                      WALKFORWARD_STRATEGY_ID, Fold, check_sample_range, deflated_sharpe,
                                      expected_max_sr, make_folds, make_selection, resolve_gate,
                                      select_params, sr_variance, stitch_test_roundtrips,
                                      walkforward_verdict)
from src.ingest import bitmex_funding, normalize
from src.ingest.store import load_bars
from src.shared import sink as shared_sink
from src.shared.schema import (ROUNDTRIPS_NET, ROUNDTRIPS_NET_FUNDING, TableSchema, empty_frame,
                               validate_roundtrips_net, validate_roundtrips_net_funding)

log = logging.getLogger(__name__)

DEFAULT_OUT_DIR = Path("data/out/backtest")
PROGRESS_EVERY = 100
ROW_GROUP_ROWS = 100_000  # 트리거별 버퍼가 이 행 수에 닿으면 행 그룹 하나로 flush
RT_SORT_KEY = ["strategy_id", "param_id", "trade_id"]
MD_COLUMNS = ("strategy_id", "param_id", "n_trades", "sharpe", "mdd", "total_net_ret", "total_gross_ret",
              "n_liq_breach", "gate")
FUNDING_MD_COLUMNS = ("n_funding", "total_funding_xbt")  # 펀딩 켰을 때만 기본 모드 표 끝에
FUNDING_SUFFIX = "+funding"
FUNDING_GAP_SHOW = 5  # 결측 오류 메시지에 보일 시각 수


class RoundtripSink(shared_sink.RoundtripSink):
    """net 라운드트립용 `src.shared.sink.RoundtripSink`(0행 트리거는 행 그룹 0개).

    검증 함수는 schema(net/net_funding)로 고르고, 행 그룹 크기는 생성 시점의 모듈 `ROW_GROUP_ROWS` 를 쓴다.
    """

    def __init__(self, paths: Mapping[str, Path], schema: TableSchema = ROUNDTRIPS_NET):
        validate = validate_roundtrips_net_funding if schema is ROUNDTRIPS_NET_FUNDING else validate_roundtrips_net
        super().__init__(paths, schema, validate, row_group_rows=ROW_GROUP_ROWS)


def _project_net(net: pd.DataFrame) -> pd.DataFrame:
    """펀딩 반영 net(34열) → `ROUNDTRIPS_NET` 32열(`net_ret` 은 이미 펀딩 포함). metrics·walkforward 의 strict 검증용."""
    return net[ROUNDTRIPS_NET.column_names]


def _funding_totals(net: pd.DataFrame) -> dict:
    """펀딩 반영 net → run 요약에 붙일 `n_funding`(정산 수 합)·`total_funding_xbt`(비용 부호 XBT 합)."""
    return {"n_funding": int(net["n_funding"].sum()), "total_funding_xbt": float(net["funding_xbt"].sum())}


def _empty_funding_totals() -> dict:
    return {"n_funding": 0, "total_funding_xbt": 0.0}


def _net_and_summary(net: pd.DataFrame, start, end, gate: Mapping | None, funding: pd.DataFrame | None,
                     bars: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """비용 적용된 net → (펀딩 켰으면 `apply_funding`) → 요약. 펀딩 끔이면 기존 경로 그대로.

    켜면 요약은 32열 투영본으로 계산하고(`summarize_net_run` 무변경, 설계 8항) 펀딩 합계를 붙인다.
    """
    if funding is None:
        return net, summarize_net_run(net, start, end, gate)
    net = costs.apply_funding(net, funding, bars)
    summary = summarize_net_run(_project_net(net), start, end, gate)
    summary.update(_funding_totals(net))
    return net, summary


def _compute_run(bars: pd.DataFrame, p: Params, fee_profile: str | Mapping, start: date, end: date,
                 gate: Mapping | None, keep_net: bool = True,
                 funding: pd.DataFrame | None = None) -> tuple[dict, pd.DataFrame | None]:
    """run 1개: 생성 → 비용 → (펀딩) → 요약(+ run 키). 순차·병렬 경로가 모두 이 함수를 부른다(결정성의 근거).

    모듈 전역 이름(`generate_run`·`costs.apply_costs`·`summarize_net_run`)을 호출 시점에 찾는다(테스트 주입용).
    `keep_net=False` 면 net 프레임 대신 None 을 돌려준다(요약만 필요한 그리드의 IPC 절약).
    `funding` 이 있으면 `apply_costs` 뒤 `costs.apply_funding` 을 적용한다(net = `ROUNDTRIPS_NET_FUNDING`).
    """
    res = generate_run(bars, p)
    net = costs.apply_costs(res.roundtrips, fee_profile)
    del res
    net, summary = _net_and_summary(net, start, end, gate, funding, bars)
    summary = _with_run_key(summary, p.strategy_id, p.param_id)
    return summary, (net if keep_net else None)


_WORKER: dict = {}  # spawn 워커 프로세스 전역: initializer 가 bars 등 run 공통 입력을 한 번만 받아 둔다


def _init_worker(bars, fee_profile, start, end, gate, keep_net, funding=None) -> None:
    _WORKER.update(bars=bars, fee_profile=fee_profile, start=start, end=end, gate=gate, keep_net=keep_net,
                   funding=funding)


def _worker_run(p: Params) -> tuple[dict, pd.DataFrame | None]:
    w = _WORKER
    return _compute_run(w["bars"], p, w["fee_profile"], w["start"], w["end"], w["gate"], w["keep_net"],
                        w["funding"])


WINDOW_PER_JOB = 2  # 병렬 실행 시 미완료 run 상한 = WINDOW_PER_JOB × jobs (메모리 상한이 run 수와 무관)


def _parallel_runs(bars, ordered: Sequence[Params], fee_profile, start, end, gate, keep_net: bool, jobs: int,
                   funding=None):
    """spawn 프로세스 풀에서 run 을 계산해 `ordered` 순서 그대로 (summary, net) 을 하나씩 내놓는다.

    정렬 순서 슬라이딩 윈도우: 미완료 future 를 최대 `WINDOW_PER_JOB × jobs` 개 제출하고 맨 앞 future 의
    결과를 기다려 내놓은 뒤 하나 더 제출한다(완료 순서 무시). 예외·중단 시 남은 태스크를 취소한다.
    """
    ctx = multiprocessing.get_context("spawn")
    window = WINDOW_PER_JOB * jobs
    ex = ProcessPoolExecutor(max_workers=jobs, mp_context=ctx, initializer=_init_worker,
                             initargs=(bars, fee_profile, start, end, gate, keep_net, funding))
    pending: deque = deque()
    it = iter(ordered)
    ok = False
    try:
        for p in itertools.islice(it, window):
            pending.append(ex.submit(_worker_run, p))
        while pending:
            result = pending.popleft().result()
            nxt = next(it, None)
            if nxt is not None:
                pending.append(ex.submit(_worker_run, nxt))
            yield result
        ok = True
    finally:
        ex.shutdown(wait=True, cancel_futures=not ok)


def run_grid(bars: pd.DataFrame, params_list: Sequence[Params], fee_profile: str | Mapping,
             start: date, end: date, gate: Mapping | None, sink, jobs: int = 1,
             funding: pd.DataFrame | None = None) -> list[dict]:
    """run 별 생성 → 비용 → `sink.write(strategy_id, net)` → 요약. (strategy_id, param_id) 정렬 요약 목록을 돌려준다.

    `[start, end)` 반열린 구간. params 를 (strategy_id, param_id) 로 stable 정렬해 실행하고 run 내 `trade_id` 가
    0..n-1 오름차순이므로, 싱크가 받는 순서가 곧 `RT_SORT_KEY` 순서다. 거래 0건 run 도 0행 프레임을 넘긴다
    (실행한 트리거는 0행 parquet 라도 생긴다). net 프레임은 넘긴 뒤 버린다 — 누적은 싱크의 책임.
    `jobs ≥ 2` 면 run 계산을 spawn 프로세스 풀에서 하되 결과는 정렬 순서로 받아 싱크 쓰기는 이 프로세스에서만
    한다(`_parallel_runs`) — 출력은 `jobs=1` 과 같다. `_DiscardSink` 면 워커는 요약만 돌려준다.
    `funding` 이 있으면 모든 run 에 펀딩을 적용한다(싱크는 `ROUNDTRIPS_NET_FUNDING` 스키마여야 한다).
    """
    if jobs < 1:
        raise ValueError(f"jobs 는 1 이상이어야 한다: {jobs}")
    ordered = sorted(params_list, key=lambda p: (p.strategy_id, p.param_id))
    keep_net = not isinstance(sink, _DiscardSink)
    if jobs >= 2 and len(ordered) >= 2:
        results = _parallel_runs(bars, ordered, fee_profile, start, end, gate, keep_net, jobs, funding)
    else:
        results = (_compute_run(bars, p, fee_profile, start, end, gate, keep_net, funding) for p in ordered)
    summaries = []
    t0 = time.monotonic()
    try:
        for i, (p, (summary, net)) in enumerate(zip(ordered, results), 1):
            if keep_net:
                sink.write(p.strategy_id, net)
            summaries.append(summary)
            del net
            if i % PROGRESS_EVERY == 0 or i == len(ordered):
                log.info("run %d/%d (%.1fs)", i, len(ordered), time.monotonic() - t0)
    finally:
        results.close()  # 싱크 쓰기 실패 등으로 빠져나오면 풀의 남은 태스크를 바로 취소한다
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
                    n_trials: int = N_TRIALS, jobs: int = 1, funding: pd.DataFrame | None = None) -> dict:
    """폴드별 학습 그리드(`default`) → `select_params` → 검증 실행(`default`·`bybit`) → 이어 붙인 곡선·DSR 리포트.

    `load_bars(start, end)` 는 반열린 `[start, end)` UTC 자정 Timestamp 를 받아 1분봉을 돌려주는 주입 함수다.
    학습 구간을 먼저 읽고 선택이 끝난 뒤에만 검증 구간을 읽는다. 선택 없음 폴드는 검증 바를 읽지 않고
    거래 0·현금 보유로 요약한다. V 는 표본 전체 `risk_pct = 1` run 기준, `selection` 은 `sample` 이 기본 표본
    전체일 때만 `make_selection` 결과(그 외 `None`). 반환 dict 의 NaN 은 float 그대로(JSON 변환은 호출자).
    `jobs` 는 학습·표본 전체 그리드 `run_grid` 에만 넘긴다(검증 run 1개는 순차).
    `funding` 이 있으면 학습·검증·표본 전체 run 모두에 적용하고, 검증·곡선 요약에 펀딩 합계를 붙이며
    선택 파일에 `funding: true` 를 넣는다(이어 붙이기는 32열 투영본, 곡선 합계 = 폴드 검증 합계의 합).
    """
    g = resolve_gate(gate)
    s, e = check_sample_range(*sample)
    _check_folds(folds, (s, e))
    by_key = {(p.strategy_id, p.param_id): p for p in params_list}
    funded = funding is not None

    fold_reports = []
    test_nets: dict[str, list] = {name: [] for name in PROFILE_NAMES}
    for i, f in enumerate(folds, 1):
        bars = _load_range(load_bars, f.train_start, f.train_end)
        train = run_grid(bars, params_list, "default", f.train_start, f.train_end, g, _DiscardSink(), jobs,
                         funding)
        del bars
        sel = select_params(train, g)
        test = {}
        if sel is None:
            log.info("폴드 %d: 선택 없음 → 현금 보유", i)
            selection = None
            empty = empty_frame(ROUNDTRIPS_NET)
            for name in PROFILE_NAMES:
                test[name] = summarize_net_run(empty, f.test_start, f.test_end, g)
                if funded:
                    test[name].update(_empty_funding_totals())
                test_nets[name].append(None)
        else:
            p = by_key[(sel["strategy_id"], sel["param_id"])]
            selection = {"strategy_id": p.strategy_id, "param_id": p.param_id, "train_sharpe": sel["sharpe"],
                         "train_n_trades": sel["n_trades"], "train_mdd": sel["mdd"]}
            bars = _load_range(load_bars, f.test_start, f.test_end)
            res = generate_run(bars, p)
            for name in PROFILE_NAMES:
                net, summary = _net_and_summary(costs.apply_costs(res.roundtrips, name), f.test_start, f.test_end,
                                                g, funding, bars)
                test[name] = _with_run_key(summary, p.strategy_id, p.param_id)
                test_nets[name].append(_project_net(net) if funded else net)
            del res, bars  # 펀딩 mark 조회가 끝난 뒤 버린다
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
        if funded:
            stitched[name].update(
                n_funding=sum(fr["test"][name]["n_funding"] for fr in fold_reports),
                total_funding_xbt=sum(fr["test"][name]["total_funding_xbt"] for fr in fold_reports))
    del test_nets

    bars = _load_range(load_bars, s, e)
    full = run_grid(bars, params_list, "default", s, e, g, _DiscardSink(), jobs, funding)
    del bars
    rv = representative_var(full, params_list)
    full_sel = select_params(full, g)
    selection_file = make_selection(full_sel, g, funding=funded) \
        if full_sel is not None and (s, e) == (SAMPLE_START, OOS_START) else None

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


def _funding_label(name: str, funded: bool) -> str:
    """산출 경로 이름: 펀딩 켜면 `<name>+funding`(설계 "펀딩 모델" 10항), 끄면 그대로."""
    return name + FUNDING_SUFFIX if funded else name


def walkforward_paths(out_dir: Path, funded: bool = False) -> dict[str, Path]:
    d = out_dir / "walkforward"
    rep, sel = _funding_label("default", funded), _funding_label("selection", funded)
    return {"json": d / f"{rep}.json", "md": d / f"{rep}.md", "selection": d / f"{sel}.json"}


def build_walkforward_report(engine_report: Mapping, symbol: str, axes: dict,
                             funding_meta: Mapping | None = None) -> dict:
    meta = {
        "symbol": symbol, "ruleset_version": RULESET_VERSION, "grid": axes, "n_runs": engine_report["n_runs"],
        "fee": {name: costs.PROFILES[name] for name in PROFILE_NAMES}, "judge_profile": "default",
    }
    if funding_meta is not None:
        meta["funding"] = dict(funding_meta)
    return to_jsonable({"meta": meta, **engine_report})


WF_FOLD_COLUMNS = ("폴드", "학습", "검증", "선택", "학습 Sharpe", "default 거래", "default Sharpe", "default MDD",
                   "default net", "bybit net")
WF_CURVE_COLUMNS = ("프로필", "n_trades", "sharpe", "mdd", "total_net_ret", "n_days", "gate")


def _row(cells) -> str:
    return "| " + " | ".join(cells) + " |"


def _curve_columns(meta: Mapping) -> tuple:
    """워크포워드 검증 곡선·OOS 결과 표 열. 펀딩 켠 리포트만 끝에 `total_funding_xbt`."""
    return WF_CURVE_COLUMNS + ("total_funding_xbt",) if "funding" in meta else WF_CURVE_COLUMNS


def _unmodeled(meta: Mapping) -> str:
    """경고 줄의 미모델링 문구. 펀딩 켠 리포트는 강제청산만."""
    return "강제청산 미모델링(펀딩 반영 `--funding`)" if "funding" in meta else "펀딩·강제청산 미모델링"


def render_walkforward_markdown(report: dict) -> str:
    """워크포워드 JSON 리포트 → 폴드 표·검증 곡선 3기준·DSR·최종 판정·bybit 민감도(사람이 읽을 용도)."""
    m, g, dsr = report["meta"], report["gate"], report["dsr"]
    s0, s1 = report["sample"]
    sel_file = report["full_sample"]["selection"]
    curve_cols = _curve_columns(m)
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
        _row(curve_cols),
        "|" + "---|" * len(curve_cols),
    ]
    for name in PROFILE_NAMES:
        st = report["stitched"][name]
        lines.append(_row([name] + [_cell(st.get(c)) for c in curve_cols[1:]]))
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
    sel_name = f"{_funding_label('selection', 'funding' in m)}.json"
    if sel_file:
        lines.append(f"- `{sel_name}`: {sel_file['strategy_id']} / {sel_file['param_id']}"
                     f" · sha256 `{sel_file['sha256'][:12]}`")
    else:
        lines.append(f"- 선택 없음 — {sel_name} 미작성, OOS 진행 불가")
    lines += [
        "",
        f"- ⚠ {_unmodeled(m)}. 실거래 전환은 사람이 판단한다.",
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


# OOS 1회 --------------------------------------------------------------------------------------------

def oos_paths(out_dir: Path, sha256: str) -> dict[str, Path]:
    d = out_dir / "oos"
    return {"json": d / f"{sha256[:12]}.json", "md": d / f"{sha256[:12]}.md"}


def read_selection(path: Path) -> dict:
    """선택 파일 JSON → dict. 읽기 실패는 `OSError`, JSON 오류·dict 아님은 `ValueError`."""
    sel = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(sel, dict):
        raise ValueError("선택 파일이 JSON 객체가 아니다")
    return sel


def walkforward_context(selection_path: Path, sha: str, funded: bool = False) -> dict:
    """선택 파일 옆 워크포워드 리포트(`default.json`, 펀딩 선택이면 `default+funding.json`)에서 DSR 의 V 와
    워크포워드 판정을 읽는다.

    그 리포트의 `full_sample.selection.sha256` 이 `sha` 와 같을 때만 값을 쓴다. 없거나·읽을 수 없거나·sha256 이
    다르면 V = NaN·판정 None(+ 경고). OOS DSR 은 참고값이라 실패 사유가 아니다.
    """
    path = Path(selection_path).parent / f"{_funding_label('default', funded)}.json"
    none = {"report": None, "var_sr": float("nan"), "verdict": None}
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
        rep_sha = (rep.get("full_sample") or {}).get("selection", {}) or {}
        if rep_sha.get("sha256") != sha:
            log.warning("워크포워드 리포트 %s 의 선택 sha256 이 다르다 → DSR V 없음", path)
            return none
        v = rep["dsr"]["var_sr"]
        return {"report": str(path), "var_sr": float("nan") if v is None else float(v),
                "verdict": rep["verdict"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("워크포워드 리포트 %s 를 쓸 수 없다(%s) → DSR V 없음", path, e)
        return none


def run_oos(selection: Mapping, params: Params, load_bars: Callable, log_path, wf_ctx: Mapping,
            n_trials: int = N_TRIALS, funding: pd.DataFrame | None = None) -> dict:
    """OOS [2022-01-01, 2025-01-01) 바 → 선택 run 1개 → `default`·`bybit` 비용 → 요약 → `evaluate_oos`(로그 append).

    모든 계산이 끝난 뒤 마지막에 `evaluate_oos` 를 한 번 부른다 — 그 전 실패는 로그를 남기지 않는다.
    판정 = `evaluate_oos` 반환(default 3기준), bybit 는 병기. DSR 은 참고값(판정 미반영).
    `funding` 이 있으면 두 프로필 모두 펀딩을 적용하고(평가는 32열 투영본) 결과 요약에 펀딩 합계를 붙인다.
    """
    g = selection["gate"]
    bars = _load_range(load_bars, OOS_START, OOS_END)
    res = generate_run(bars, params)
    nets, totals = {}, {}
    for name in PROFILE_NAMES:
        net = costs.apply_costs(res.roundtrips, name)
        if funding is not None:
            net = costs.apply_funding(net, funding, bars)
            totals[name] = _funding_totals(net)
            net = _project_net(net)
        nets[name] = net
    del res, bars  # 펀딩 mark 조회가 끝난 뒤 버린다
    bybit = _with_run_key(summarize_net_run(nets["bybit"], OOS_START, OOS_END, g),
                          params.strategy_id, params.param_id)
    v = float(wf_ctx["var_sr"])
    default = walkforward.evaluate_oos(selection, nets["default"], log_path)  # 유일한 로그 쓰기
    if funding is not None:
        default.update(totals["default"])
        bybit.update(totals["bybit"])
    results = {"default": default, "bybit": bybit}
    dsr = {"var_sr": v, "sr0": expected_max_sr(int(n_trials), v), "source": wf_ctx["report"]}
    for name in PROFILE_NAMES:
        r = results[name]
        dsr[name] = deflated_sharpe(r["sr_daily"], n_trials, v, r["n_days"], r["skew_daily"], r["kurt_daily"])
    wf_verdict = wf_ctx["verdict"]
    return {
        "selection": dict(selection), "oos": [_fmt_day(OOS_START), _fmt_day(OOS_END)], "gate": g,
        "n_trials": int(n_trials), "results": results,
        "verdict": default["gate"], "bybit_verdict": bybit["gate"], "dsr": dsr,
        "walkforward_verdict": wf_verdict,
        "phase4_eligible": None if wf_verdict is None else (wf_verdict == "pass" and default["gate"] == "pass"),
    }


def build_oos_report(engine_report: Mapping, symbol: str, axes: dict, funding_meta: Mapping | None = None) -> dict:
    """실행 시각은 넣지 않는다(결정적 — 시각은 접근 로그에만)."""
    meta = {"symbol": symbol, "ruleset_version": RULESET_VERSION, "grid": axes,
            "fee": {name: costs.PROFILES[name] for name in PROFILE_NAMES}, "judge_profile": "default"}
    if funding_meta is not None:
        meta["funding"] = dict(funding_meta)
    return to_jsonable({"meta": meta, **engine_report})


PHASE4_TEXT = "Phase 4 진입 자격 = 워크포워드 pass 그리고 OOS pass, 착수·실거래 전환은 사람 판단"


def render_oos_markdown(report: dict) -> str:
    """OOS JSON 리포트 → 결과 표·OOS 판정·bybit 병기·DSR 참고값·Phase 4 자격(사람이 읽을 용도)."""
    m, g, dsr, sel = report["meta"], report["gate"], report["dsr"], report["selection"]
    o0, o1 = report["oos"]
    curve_cols = _curve_columns(m)
    lines = [
        f"# OOS 1회 {m['symbol']} [{o0}, {o1}) · 판정 {m['judge_profile']}",
        "",
        f"- 선택: {sel['strategy_id']} / {sel['param_id']} · sha256 `{sel['sha256'][:12]}`"
        f" · 선택 구간 [{sel['selected_on'][0]}, {sel['selected_on'][1]})",
        f"- ruleset `{m['ruleset_version']}`",
        f"- 게이트 기준(3기준): 거래 ≥ {g['min_trades']} · Sharpe ≥ {g['min_sharpe']} · MDD ≤ {g['max_drawdown']}",
        "- 단위: mdd·net 비율(0.01 = 1%), sharpe 연환산(√365).",
        "",
        "## 결과",
        "",
        _row(curve_cols),
        "|" + "---|" * len(curve_cols),
    ]
    for name in PROFILE_NAMES:
        r = report["results"][name]
        lines.append(_row([name] + [_cell(r.get(c)) for c in curve_cols[1:]]))
    if dsr["var_sr"] is None:
        dsr_lines = ["- V 없음 — 같은 sha256 의 워크포워드 리포트(선택 파일 옆"
                     f" `{_funding_label('default', 'funding' in m)}.json`)를 찾지 못했다"]
    else:
        dsr_lines = [f"- V = {_cell(dsr['var_sr'])} (출처 `{dsr['source']}`) · SR0 = {_cell(dsr['sr0'])}",
                     f"- DSR default = {_cell(dsr['default'])} · bybit = {_cell(dsr['bybit'])}"]
    elig = report["phase4_eligible"]
    elig_text = ("판단 불가 — 워크포워드 리포트 없음" if elig is None
                 else f"{'충족' if elig else '미충족'} (워크포워드 {report['walkforward_verdict']}"
                      f" · OOS {report['verdict']})")
    lines += [
        "",
        "## OOS 판정",
        "",
        f"- **{report['verdict']}** (default: 3기준)",
        "",
        "## bybit 병기",
        "",
        f"- {report['bybit_verdict']} — 판정에 쓰지 않는다",
        "",
        f"## DSR 참고값 (N={report['n_trials']}, 판정 미반영)",
        "",
        *dsr_lines,
        "",
        "## Phase 4 진입 자격",
        "",
        f"- {PHASE4_TEXT}",
        f"- 이번 결과: {elig_text}",
        "",
        f"- ⚠ {_unmodeled(m)}. OOS 는 한 선택으로 한 번만 본다 — 접근 로그 가드 해제는 사람 판단.",
    ]
    return "\n".join(lines) + "\n"


def write_oos_outputs(paths: Mapping[str, Path], report: dict) -> None:
    _write_text_atomic(_json_text(report), paths["json"])
    _write_text_atomic(render_oos_markdown(report), paths["md"])


def build_report(start: date, end: date, symbol: str, bars: pd.DataFrame, axes: dict, fee_profile: str,
                 gate: dict, summaries: list[dict], funding_meta: Mapping | None = None) -> dict:
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
    if funding_meta is not None:
        meta["funding"] = dict(funding_meta)
    return to_jsonable({"meta": meta, "runs": summaries})


def render_markdown(report: dict) -> str:
    """JSON 리포트 → run 별 핵심 지표 표와 통과 run 수(사람이 읽을 용도)."""
    m = report["meta"]
    g, f = m["gate"], m["fee"]
    cols = MD_COLUMNS + FUNDING_MD_COLUMNS if "funding" in m else MD_COLUMNS
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
        f"- ⚠ 단일 구간 3기준 판정이다. 최종 판정 아님 — 워크포워드·DSR 필요. {_unmodeled(m)}.",
        "- 단위: mdd·total_net_ret·total_gross_ret 비율(0.01 = 1%), sharpe 연환산(√365).",
        "",
        "| " + " | ".join(cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for r in report["runs"]:
        lines.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
    return "\n".join(lines) + "\n"


def output_paths(out_dir: Path, fee_profile: str, start: date, end: date, strategy_ids,
                 funded: bool = False) -> dict[str, Path]:
    span, label = _span(start, end), _funding_label(fee_profile, funded)
    paths = {sid: out_dir / "roundtrips_net" / label / sid / f"{span}.parquet" for sid in strategy_ids}
    paths["json"] = out_dir / "summary" / label / f"{span}.json"
    paths["md"] = out_dir / "summary" / label / f"{span}.md"
    return paths


def _load_checked_funding(symbol: str, start, end, funding_dir: Path) -> tuple[pd.DataFrame, dict]:
    """평가 구간 `[start, end)` 펀딩 로드 + 격자 결측 검사(설계 "펀딩 모델" 5항, run 계산·바 로드 전에 1회).

    파일 없음 `FileNotFoundError`, 스키마 위반 `SchemaError`, 격자 시각(04·12·20 UTC) 결측·NaN 이 하나라도 있으면
    `ValueError`(→ CLI 종료코드 1). 격자 밖 행(`off_grid`)은 경고만 — 데이터 `ts` 대로 부과한다(1항).
    반환 meta = 리포트 `meta.funding`(`source` 파일 경로·`n_settlements` 구간 정산 행 수).
    """
    df = bitmex_funding.load_funding(symbol, start, end, funding_dir)
    gaps = bitmex_funding.check_funding_gaps(df, start, end)
    missing = [gap.ts for gap in gaps if gap.kind == "missing"]
    off_grid = [gap.ts for gap in gaps if gap.kind == "off_grid"]
    if missing:
        shown = ", ".join(str(t) for t in missing[:FUNDING_GAP_SHOW])
        raise ValueError(f"펀딩 결측 {len(missing)}건 [{start}, {end}): {shown}"
                         + (" …" if len(missing) > FUNDING_GAP_SHOW else ""))
    if off_grid:
        log.warning("펀딩 격자 밖 정산 %d건(데이터 ts 대로 부과): %s", len(off_grid),
                    ", ".join(str(t) for t in off_grid[:FUNDING_GAP_SHOW]))
    meta = {"source": str(bitmex_funding.funding_path(funding_dir, symbol)), "n_settlements": len(df)}
    log.info("펀딩 %d건 로드 [%s, %s): %s", len(df), start, end, meta["source"])
    return df, meta


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
    p.add_argument("--oos-final", type=Path, default=None, metavar="PATH",
                   help="워크포워드 selection.json 으로 OOS [2022-01-01, 2025-01-01) 1회 실행 → oos/<sha256 앞 12자>.json·md"
                        " (--walkforward·--start/--end 불가, --fee-profile default 만)")
    p.add_argument("--oos-log", type=Path, default=None, metavar="PATH",
                   help=f"OOS 접근 로그 경로 (기본 {OOS_LOG_PATH}, --oos-final 과만). 테스트·재현용 — "
                        "로그 삭제·가드 해제는 사람")
    p.add_argument("--jobs", type=int, default=1, metavar="N",
                   help="run 병렬 프로세스 수 (기본 1 = 순차). 워커마다 bars 사본을 들므로 메모리 ≈ N × bars."
                        " 결과는 N 과 무관하게 같다. --oos-final 은 run 1개라 효과 없음")
    p.add_argument("--funding", action="store_true",
                   help="펀딩 비용 반영(기본 끔). 실행 전 평가 구간 펀딩 결측 검사(결측 → 종료코드 1), 산출 경로"
                        " <fee_profile>+funding·selection+funding.json, 선택 파일 funding: true")
    p.add_argument("--funding-dir", type=Path, default=None, metavar="PATH",
                   help=f"펀딩 parquet 디렉터리 (기본 {bitmex_funding.DEFAULT_FUNDING_DIR}, --funding 과만)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.jobs < 1:
        parser.error(f"--jobs 는 1 이상이어야 한다: {a.jobs}")
    if a.funding_dir is not None and not a.funding:
        parser.error("--funding-dir 는 --funding 과 함께만 쓴다")
    if a.funding_dir is None:
        a.funding_dir = bitmex_funding.DEFAULT_FUNDING_DIR
    if a.oos_final is not None:
        if a.walkforward:
            parser.error("--oos-final 은 --walkforward 와 함께 쓸 수 없다")
        if a.start is not None or a.end is not None:
            parser.error("--oos-final 은 OOS [2022-01-01, 2025-01-01) 고정이라 --start/--end 를 받지 않는다")
        if a.fee_profile != "default":
            parser.error("--oos-final 은 --fee-profile default 만 받는다(bybit 는 리포트에 병기된다)")
    elif a.oos_log is not None:
        parser.error("--oos-log 는 --oos-final 과 함께만 쓴다")
    elif a.walkforward:
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
    if a.oos_final is not None:
        return _main_oos(parser, a, axes, params_list)
    if a.walkforward:
        return _main_walkforward(a, axes, params_list)
    gate = resolve_gate(None)

    t0 = time.monotonic()
    funding = fmeta = None
    try:
        if a.funding:  # 결측 검사는 바 로드·run 계산 전에(설계 "펀딩 모델" 5항)
            funding, fmeta = _load_checked_funding(a.symbol, a.start, a.end, a.funding_dir)
        bars = load_bars(a.start, a.end - timedelta(days=1), a.symbol, out_dir=a.data_dir)  # store 는 종료일 포함
        log.info("1분봉 %d개 로드, run %d개 실행 (fee %s)", len(bars), len(params_list), a.fee_profile)
        paths = output_paths(a.out, a.fee_profile, a.start, a.end, sorted({p.strategy_id for p in params_list}),
                             funded=a.funding)
        schema = ROUNDTRIPS_NET_FUNDING if a.funding else ROUNDTRIPS_NET
        with RoundtripSink(paths, schema) as sink:
            summaries = run_grid(bars, params_list, a.fee_profile, a.start, a.end, gate, sink, a.jobs, funding)
            report = build_report(a.start, a.end, a.symbol, bars, axes, a.fee_profile, gate, summaries, fmeta)
            sink.commit()
        write_outputs(paths, report)
    except Exception:
        log.exception("백테스트 실패")
        return 1
    log.info("완료 (%.1fs): %s", time.monotonic() - t0, paths["json"])
    return 0


def _main_walkforward(a: argparse.Namespace, axes: dict, params_list: Sequence[Params]) -> int:
    t0 = time.monotonic()
    paths = walkforward_paths(a.out, funded=a.funding)
    funding = fmeta = None
    try:
        if a.funding:  # 폴드 학습·검증 합집합 = 기본 표본 전체를 실행 전에 한 번 검사
            funding, fmeta = _load_checked_funding(a.symbol, SAMPLE_START, OOS_START, a.funding_dir)
        engine = run_walkforward(make_folds(), store_loader(a.symbol, a.data_dir), params_list,
                                 gate=resolve_gate(None), jobs=a.jobs, funding=funding)
        report = build_walkforward_report(engine, a.symbol, axes, fmeta)
        write_walkforward_outputs(paths, report)
    except Exception:
        log.exception("워크포워드 실패")
        return 1
    log.info("완료 (%.1fs): %s 판정 %s", time.monotonic() - t0, paths["json"], report["verdict"])
    return 0


def _main_oos(parser: argparse.ArgumentParser, a: argparse.Namespace, axes: dict,
              params_list: Sequence[Params]) -> int:
    log_path = a.oos_log or OOS_LOG_PATH
    try:
        selection = read_selection(a.oos_final)
        if selection.get("funding", False) != a.funding:  # 데이터·로그 접근 전에(설계 "펀딩 모델" 10항)
            raise ValueError(f"선택 파일 funding={selection.get('funding', False)!r} 와 --funding={a.funding} 이"
                             " 다르다 — 선택을 만든 워크포워드 실행과 같은 펀딩 설정으로 실행해라")
        walkforward.authorize_oos(selection, log_path)  # 쓰기 없음(조기 거부용)
    except (OSError, ValueError) as e:  # PermissionError(로그) ⊂ OSError, JSONDecodeError ⊂ ValueError
        parser.error(f"--oos-final {a.oos_final}: {e}")
    by_key = {(p.strategy_id, p.param_id): p for p in params_list}
    params = by_key.get((selection["strategy_id"], selection["param_id"]))
    if params is None:
        parser.error(f"선택 run {selection['strategy_id']} / {selection['param_id']} 가 그리드에 없다 — "
                     "--grid 를 선택을 만든 워크포워드 실행과 같게 줘라")
    sha = selection["sha256"]
    paths = oos_paths(a.out, sha)
    wf_ctx = walkforward_context(a.oos_final, sha, funded=a.funding)
    t0 = time.monotonic()
    funding = fmeta = None
    try:
        if a.funding:  # OOS 전체를 바 로드 전에 한 번 검사(결측이면 1, 접근 로그 미기록)
            funding, fmeta = _load_checked_funding(a.symbol, OOS_START, OOS_END, a.funding_dir)
        engine = run_oos(selection, params, store_loader(a.symbol, a.data_dir), log_path, wf_ctx,
                         funding=funding)
    except PermissionError as e:  # 데이터를 읽는 사이 로그에 다른 sha256 이 생김 → 거부(로그 미기록)
        parser.error(f"OOS 접근 로그 {log_path}: {e}")
    except Exception:
        log.exception("OOS 실행 실패(접근 로그 미기록)")
        return 1
    try:
        report = build_oos_report(engine, a.symbol, axes, fmeta)
        write_oos_outputs(paths, report)
    except Exception:
        log.exception("OOS 리포트 쓰기 실패 — 접근 로그는 기록됨, 같은 선택으로 재실행하면 복구된다")
        return 1
    log.info("완료 (%.1fs): %s OOS 판정 %s", time.monotonic() - t0, paths["json"], report["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
