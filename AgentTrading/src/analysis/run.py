"""Phase 2 분석 CLI: 정규화 1분봉 구간 → 파라미터 그리드별 합성 라운드트립 → 분포 리포트.

`src.ingest.store.load_bars`(결측 일 → `MissingDaysError`) → run 마다 `synthetic.generate_run`
→ `patterns.summarize_run` 을 순서대로 부르는 얇은 오케스트레이터다. 지표·생성·요약 로직은 각 모듈에 있다.
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase2-synthetic-strategy.md`
"저장", "파라미터 범위", "미래 정보 누수 방지" 7(표본 분리)·8(결측 일 허용 금지)·9(결정성).
문서와 이 파일이 다르면 문서를 따른다.

    python -m src.analysis.run --start 2019-06-01 --end 2019-06-07 --symbol XBTUSD
    python -m src.analysis.run --start 2019-06-01 --end 2019-06-07 --grid grid.json --out data/out/analysis
    python -m src.analysis.run --start 2019-06-01 --end 2019-06-07 --jobs 4

| 출력 | 경로 |
|---|---|
| 라운드트립 | `<out>/roundtrips/<strategy_id>/<YYYYMMDD>_<YYYYMMDD>.parquet` (실행한 그 트리거의 모든 param_id) |
| 분포 요약 | `<out>/distributions/<YYYYMMDD>_<YYYYMMDD>.json` + 같은 이름 `.md` |

- 표본 구간은 2018-03-01~2021-12-31. 밖(특히 OOS 2022-01-01 이후)은 데이터를 읽기 전에 거부(종료코드 2).
- `--grid` 는 축 → 값 목록 JSON(설계 그리드 부분집합만, 생략 축은 설계 값 전체). 위반은 종료코드 2.
- 결측 일·하드 가드 등 실행 중 예외는 로그 후 종료코드 1. 라운드트립은 run 을 (strategy_id, param_id) 순서로
  실행하며 run 단위로 트리거별 `<parquet>.tmp` 에 이어 쓰고(`src.shared.sink.RoundtripSink`, 메모리 상한 = 행 그룹
  버퍼 + run 1개(`--jobs 1`) — run 수·구간 길이와 무관), 전체 성공 후에야 rename → JSON·MD 를 원자적으로 쓴다. 실패하면 이번
  실행의 `.tmp` 를 지우므로 기존 산출물은 그대로다(rename 뒤 JSON·MD 쓰기 실패는 파일별 원자성만 보장).
- 0행 트리거는 빈 행 그룹 1개를 쓴다. 트리거당 행 수가 `ROW_GROUP_ROWS` 미만이면 parquet 바이트가 스트리밍 도입 전
  (`DataFrame.to_parquet` 1회)과 같고, 그 이상이면 행 그룹 배치만 달라진다(읽은 내용은 같다).
- 결정성: 실행 시각·소요 시간은 파일에 넣지 않고 로그로만 낸다. 같은 입력이면 JSON·MD 바이트가 같다.
- `--jobs N`(기본 1 = 순차): N ≥ 2 면 그리드 run 을 spawn 프로세스 풀에서 계산한다(`src.shared.parallel`, bars 는
  워커 initializer 로 워커당 1회 전달). 결과는 정렬된 run 순서로 받아(미완료 상한 `WINDOW_PER_JOB × N`) 싱크 쓰기는
  메인만 하므로 산출물은 N 과 무관하게 바이트 동일하고, 메모리 상한 = 행 그룹 버퍼 + 2N run + 워커당 bars 사본.
- 모든 손익 지표는 gross(비용 전)다.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import numbers
import sys
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.analysis.patterns import summarize_run
from src.analysis.synthetic import GRID, RULESET_VERSION, Params, generate_run
from src.ingest import normalize
from src.ingest.store import load_bars
from src.shared.schema import ROUNDTRIPS, validate_roundtrips
from src.shared.parallel import ordered_pool_map
from src.shared.sink import ROW_GROUP_ROWS, RoundtripSink

log = logging.getLogger(__name__)

SAMPLE_START = date(2018, 3, 1)
OOS_START = date(2022, 1, 1)
DEFAULT_OUT_DIR = Path("data/out/analysis")
PROGRESS_EVERY = 100
RT_SORT_KEY = ["strategy_id", "param_id", "trade_id"]

MD_COLUMNS = (
    "strategy_id", "param_id", "n_trades", "skipped_min_qty", "halted", "win_rate", "payoff_ratio",
    "expectancy_ret", "holding_min_p50", "notional_pct_p50", "leverage_max", "size_capped_share",
    "max_consec_losses", "total_ret",
)


def check_sample_range(start: date, end: date) -> None:
    """표본 구간(SAMPLE_START ≤ start ≤ end < OOS_START) 밖이면 ValueError."""
    if start > end:
        raise ValueError(f"start 가 end 보다 늦음: {start} > {end}")
    if start < SAMPLE_START:
        raise ValueError(f"start {start} 는 표본 시작 {SAMPLE_START} 이전")
    if end >= OOS_START:
        raise ValueError(f"end {end} 는 OOS 구간({OOS_START} 이후) — Phase 2 에서 읽지 않는다")


def _canonical(axis: str, v):
    """JSON 값 → GRID 정식 값. bool·그리드 밖 값은 ValueError."""
    if axis == "trigger":
        if isinstance(v, str) and v in GRID[axis]:
            return v
    elif v is None:
        if None in GRID[axis]:
            return None
    elif isinstance(v, numbers.Real) and not isinstance(v, bool):
        for g in GRID[axis]:
            if g is not None and v == g:
                return g
    raise ValueError(f"그리드 밖 값: {axis}={v!r} (허용 {list(GRID[axis])})")


def expand_grid(spec: dict) -> tuple[dict, list[Params]]:
    """축 → 값 목록 dict → (정규화된 축 값, (trigger, param_id) 정렬 Params 목록)."""
    if not isinstance(spec, dict):
        raise ValueError("그리드는 축 → 값 목록 객체여야 한다")
    unknown = sorted(set(spec) - set(GRID))
    if unknown:
        raise ValueError(f"알 수 없는 축: {unknown} (허용 {list(GRID)})")
    axes = {}
    for axis, allowed in GRID.items():
        if axis not in spec:
            axes[axis] = list(allowed)
            continue
        vals = spec[axis]
        if not isinstance(vals, list) or not vals:
            raise ValueError(f"{axis} 는 비어 있지 않은 목록이어야 한다: {vals!r}")
        chosen = {_canonical(axis, v) for v in vals}
        axes[axis] = [g for g in allowed if g in chosen]  # GRID 순서, 중복 제거

    out = []
    for trg in axes["trigger"]:
        ks = [None] if trg == "h1" else axes["k"]
        for n in axes["n"]:
            for k in ks:
                for s in axes["stop_pct"]:
                    for tp in axes["tp_r"]:
                        for mh in axes["max_hold"]:
                            for r in axes["risk_pct"]:
                                out.append(Params(trigger=trg, n=n, k=k, stop_pct=s, tp_r=tp,
                                                  max_hold=mh, risk_pct=r))
    out.sort(key=lambda p: (p.trigger, p.param_id))
    return axes, out


def load_grid(path: Path | None) -> tuple[dict, list[Params]]:
    """`--grid` JSON 파일(없으면 설계 전체 그리드) → `expand_grid` 결과."""
    if path is None:
        return expand_grid({})
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    return expand_grid(spec)


def _compute_run(bars: pd.DataFrame, p: Params) -> tuple[dict, pd.DataFrame]:
    """run 1개: 생성 → 요약(+ strategy_id·param_id·halted). 순차·병렬 경로가 모두 이 함수를 부른다(결정성의 근거).

    모듈 전역 이름(`generate_run`·`summarize_run`)을 호출 시점에 찾는다(테스트 주입용).
    """
    res = generate_run(bars, p)
    summary = summarize_run(res.roundtrips, skipped_min_qty=res.skipped_min_qty)
    summary["strategy_id"] = p.strategy_id  # 0건 run 은 patterns 가 None 으로 둔다
    summary["param_id"] = p.param_id
    summary["halted"] = bool(res.halted)
    return summary, res.roundtrips


_WORKER: dict = {}  # spawn 워커 프로세스 전역: initializer 가 bars 를 한 번만 받아 둔다


def _init_worker(bars: pd.DataFrame) -> None:
    _WORKER["bars"] = bars


def _worker_run(p: Params) -> tuple[dict, pd.DataFrame]:
    return _compute_run(_WORKER["bars"], p)


def run_grid(bars: pd.DataFrame, params_list: Sequence[Params], sink, jobs: int = 1) -> list[dict]:
    """run 을 (strategy_id, param_id) 순서로 생성·요약 → `sink.write(strategy_id, 라운드트립)` → 정렬 요약 목록.

    `generate_run` 의 trade_id 가 run 안에서 0..n-1 이므로 이 실행 순서가 곧 `RT_SORT_KEY` 정렬 순서다.
    거래 0건 run 도 `write` 를 부르므로 실행한 트리거는 0행이라도 파일을 가진다. 라운드트립은 run 이 끝나면 버린다.
    요약에는 `halted` 를 덧붙인다. `jobs ≥ 2` 이고 run 이 2개 이상이면 run 계산을 spawn 프로세스 풀에서 하되 결과는
    정렬 순서로 받아 싱크 쓰기는 이 프로세스에서만 한다(`ordered_pool_map`) — 출력은 `jobs=1` 과 같다.
    """
    if jobs < 1:
        raise ValueError(f"jobs 는 1 이상이어야 한다: {jobs}")
    ordered = sorted(params_list, key=lambda p: (p.strategy_id, p.param_id))
    if jobs >= 2 and len(ordered) >= 2:
        results = ordered_pool_map(_worker_run, ordered, jobs, initializer=_init_worker, initargs=(bars,))
    else:
        results = (_compute_run(bars, p) for p in ordered)
    summaries = []
    t0 = time.monotonic()
    try:
        for i, (p, (summary, roundtrips)) in enumerate(zip(ordered, results), 1):
            summaries.append(summary)
            sink.write(p.strategy_id, roundtrips)
            del roundtrips
            if i % PROGRESS_EVERY == 0 or i == len(ordered):
                log.info("run %d/%d (%.1fs)", i, len(ordered), time.monotonic() - t0)
    finally:
        results.close()  # 싱크 쓰기 실패 등으로 빠져나오면 풀의 남은 태스크를 바로 취소한다
    return summaries


def open_sink(paths: dict[str, Path], row_group_rows: int | None = None) -> RoundtripSink:
    """분석 라운드트립 싱크(`validate_roundtrips` 검증, 0행 트리거는 빈 행 그룹 1개)."""
    return RoundtripSink(paths, ROUNDTRIPS, validate_roundtrips,
                         row_group_rows=ROW_GROUP_ROWS if row_group_rows is None else row_group_rows,
                         write_empty_row_group=True)


def to_jsonable(obj):
    """Timestamp → ISO-8601 문자열, NaN·inf → None, numpy 스칼라 → 파이썬 기본형(재귀)."""
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if obj is None or obj is pd.NaT:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, numbers.Integral):
        return int(obj)
    if isinstance(obj, numbers.Real):
        v = float(obj)
        return v if math.isfinite(v) else None
    return obj


def build_report(start: date, end: date, symbol: str, bars: pd.DataFrame, axes: dict,
                 summaries: list[dict]) -> dict:
    n = len(bars)
    meta = {
        "start": start, "end": end, "symbol": symbol, "ruleset_version": RULESET_VERSION,
        "n_bars": n,
        "first_ts": bars["ts"].iat[0] if n else None,
        "last_ts": bars["ts"].iat[-1] if n else None,
        "grid": axes, "n_runs": len(summaries),
    }
    return to_jsonable({"meta": meta, "runs": summaries})


def _cell(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "Y" if v else ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v).replace("|", "\\|")


def render_markdown(report: dict) -> str:
    """JSON 리포트 → run 별 핵심 지표 표(사람이 읽을 용도)."""
    m = report["meta"]
    lines = [
        f"# 합성 전략 분포 {m['symbol']} {m['start']} ~ {m['end']}",
        "",
        f"- ruleset `{m['ruleset_version']}` · 1분봉 {m['n_bars']}개 ({m['first_ts']} ~ {m['last_ts']})"
        f" · run {m['n_runs']}개",
        "- 모든 손익 지표는 gross(수수료·슬리피지·펀딩 전). 레버리지·명목 비중은 사이징 규칙의 결과다.",
        "- 단위: win_rate·size_capped_share 비율, expectancy_ret·total_ret·notional_pct %, holding 분, leverage 배.",
        "",
        "| " + " | ".join(MD_COLUMNS) + " |",
        "|" + "---|" * len(MD_COLUMNS),
    ]
    for r in report["runs"]:
        lines.append("| " + " | ".join(_cell(r.get(c)) for c in MD_COLUMNS) + " |")
    return "\n".join(lines) + "\n"


def _span(start: date, end: date) -> str:
    return f"{start:%Y%m%d}_{end:%Y%m%d}"


def output_paths(out_dir: Path, start: date, end: date, strategy_ids) -> dict[str, Path]:
    span = _span(start, end)
    paths = {sid: out_dir / "roundtrips" / sid / f"{span}.parquet" for sid in strategy_ids}
    paths["json"] = out_dir / "distributions" / f"{span}.json"
    paths["md"] = out_dir / "distributions" / f"{span}.md"
    return paths


def _write_text_atomic(text: str, path: Path) -> None:
    normalize._replace_atomic(lambda tmp: tmp.write_text(text, encoding="utf-8"), path)


def write_outputs(paths: dict[str, Path], report: dict) -> None:
    """분포 JSON·MD 를 원자적으로 쓴다(라운드트립 parquet 는 `RoundtripSink` 가 쓴다)."""
    text = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    _write_text_atomic(text, paths["json"])
    _write_text_atomic(render_markdown(report), paths["md"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.analysis.run",
        description="정규화 1분봉 구간 → 합성 전략 그리드 → 라운드트립·분포 리포트 (Phase 2, gross)")
    p.add_argument("--start", required=True, type=normalize._parse_date, help="YYYY-MM-DD (포함, UTC)")
    p.add_argument("--end", required=True, type=normalize._parse_date, help="YYYY-MM-DD (포함, UTC)")
    p.add_argument("--symbol", default="XBTUSD")
    p.add_argument("--grid", type=Path, default=None,
                   help="축 → 값 목록 JSON (설계 그리드 부분집합, 생략 시 전체 2,268 run)")
    p.add_argument("--data-dir", type=Path, default=normalize.DEFAULT_OUT_DIR,
                   help=f"정규화 데이터 디렉터리 (기본 {normalize.DEFAULT_OUT_DIR})")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help=f"출력 디렉터리 (기본 {DEFAULT_OUT_DIR})")
    p.add_argument("--jobs", type=int, default=1, metavar="N",
                   help="run 병렬 프로세스 수 (기본 1 = 순차). 워커마다 bars 사본을 들므로 메모리 ≈ N × bars."
                        " 결과는 N 과 무관하게 같다")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.jobs < 1:
        parser.error(f"--jobs 는 1 이상이어야 한다: {a.jobs}")
    try:
        check_sample_range(a.start, a.end)
    except ValueError as e:
        parser.error(str(e))
    try:
        axes, params_list = load_grid(a.grid)
    except (OSError, ValueError) as e:  # json.JSONDecodeError 는 ValueError
        parser.error(f"--grid: {e}")

    t0 = time.monotonic()
    try:
        bars = load_bars(a.start, a.end, a.symbol, out_dir=a.data_dir)
        log.info("1분봉 %d개 로드, run %d개 실행", len(bars), len(params_list))
        paths = output_paths(a.out, a.start, a.end, sorted({p.strategy_id for p in params_list}))
        with open_sink(paths) as sink:
            summaries = run_grid(bars, params_list, sink, a.jobs)
            report = build_report(a.start, a.end, a.symbol, bars, axes, summaries)
            sink.commit()
        write_outputs(paths, report)
    except Exception:
        log.exception("분석 실패")
        return 1
    log.info("완료 (%.1fs): %s", time.monotonic() - t0, paths["json"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
