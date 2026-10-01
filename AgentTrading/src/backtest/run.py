"""Phase 3 백테스트 CLI(기본 모드): 정규화 1분봉 구간 → 그리드별 합성 라운드트립 → 비용 → net 지표·게이트 리포트.

`src.ingest.store.load_bars`(결측 일 → `MissingDaysError`) → run 마다 `synthetic.generate_run`
→ `costs.apply_costs` → `metrics.summarize_net_run`(게이트 판정 포함) 을 순서대로 부르는 얇은 오케스트레이터다.
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase3-backtest.md` "모듈"·"`run.py` 세부".
문서와 이 파일이 다르면 문서를 따른다. 그리드 JSON·JSON 변환·원자적 쓰기는 `src/analysis/run.py` 것을 쓴다.

    python -m src.backtest.run --start 2019-06-01 --end 2019-06-08 --symbol XBTUSD
    python -m src.backtest.run --start 2020-03-01 --end 2020-04-01 --grid grid.json --fee-profile bybit

| 출력 | 경로 |
|---|---|
| net 라운드트립 | `<out>/roundtrips_net/<fee_profile>/<strategy_id>/<YYYYMMDD>_<YYYYMMDD>.parquet` |
| 요약 | `<out>/summary/<fee_profile>/<YYYYMMDD>_<YYYYMMDD>.json` + 같은 이름 `.md` |

- 구간은 반열린 `[start, end)` UTC 자정 — `--end` 날짜는 포함하지 않는다(분석 CLI 의 종료일 포함과 다름).
  파일명의 두 번째 날짜도 반열린 end 다.
- 기본 표본 [2018-03-01, 2022-01-01) 밖·OOS·2025 이후·`start ≥ end` 는 데이터를 읽기 전에 거부(종료코드 2).
- 결측 일 등 실행 중 예외는 로그 후 종료코드 1. 모든 run 을 메모리에서 끝낸 뒤 원자적으로 쓰므로 실패 시 산출물이 없다.
- 게이트 기준은 코드 기본값(`walkforward.resolve_gate(None)`, Spec 3절)을 meta 에 기록한다. 여기서의 판정은
  단일 구간 3기준이며 최종 판정이 아니다(워크포워드·DSR 은 `--walkforward`, 후속 태스크).
- 결정성: 실행 시각·소요 시간은 파일에 넣지 않는다. 같은 입력이면 JSON·MD 바이트가 같다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.analysis.run import _cell, _span, _write_text_atomic, load_grid, to_jsonable
from src.analysis.synthetic import RULESET_VERSION, Params, generate_run
from src.backtest import costs
from src.backtest.metrics import summarize_net_run
from src.backtest.walkforward import check_sample_range, resolve_gate
from src.ingest import normalize
from src.ingest.store import load_bars
from src.shared.schema import ROUNDTRIPS_NET, empty_frame, validate_roundtrips_net

log = logging.getLogger(__name__)

DEFAULT_OUT_DIR = Path("data/out/backtest")
PROGRESS_EVERY = 100
RT_SORT_KEY = ["strategy_id", "param_id", "trade_id"]
MD_COLUMNS = ("strategy_id", "param_id", "n_trades", "sharpe", "mdd", "total_net_ret", "total_gross_ret",
              "n_liq_breach", "gate")


def run_grid(bars: pd.DataFrame, params_list: Sequence[Params], fee_profile: str | Mapping,
             start: date, end: date, gate: Mapping | None = None
             ) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    """run 별 생성 → 비용 → 요약 → ({strategy_id: net 라운드트립}, (strategy_id, param_id) 정렬 요약 목록).

    `[start, end)` 반열린 구간. 실행한 트리거는 거래 0건이어도 0행 `ROUNDTRIPS_NET` 프레임을 가진다.
    """
    frames: dict[str, list[pd.DataFrame]] = {}
    summaries = []
    t0 = time.monotonic()
    for i, p in enumerate(params_list, 1):
        res = generate_run(bars, p)
        net = costs.apply_costs(res.roundtrips, fee_profile)
        summary = summarize_net_run(net, start, end, gate)
        summary["strategy_id"] = p.strategy_id  # 0건 run 은 metrics 가 None 으로 둔다
        summary["param_id"] = p.param_id
        summaries.append(summary)
        parts = frames.setdefault(p.strategy_id, [])
        if len(net):
            parts.append(net)
        if i % PROGRESS_EVERY == 0 or i == len(params_list):
            log.info("run %d/%d (%.1fs)", i, len(params_list), time.monotonic() - t0)

    out = {}
    for sid, parts in frames.items():
        if parts:
            df = pd.concat(parts, ignore_index=True)
            df = df.sort_values(RT_SORT_KEY, kind="stable", ignore_index=True)
        else:
            df = empty_frame(ROUNDTRIPS_NET)
        out[sid] = validate_roundtrips_net(df)
    summaries.sort(key=lambda s: (s["strategy_id"], s["param_id"]))
    return out, summaries


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


def write_outputs(out_dir: Path, fee_profile: str, start: date, end: date,
                  roundtrips: dict[str, pd.DataFrame], report: dict) -> dict[str, Path]:
    """net 라운드트립 parquet(트리거별)·JSON·MD 를 원자적으로 쓴다. 쓴 경로를 돌려준다."""
    paths = output_paths(Path(out_dir), fee_profile, start, end, sorted(roundtrips))
    for sid, df in sorted(roundtrips.items()):
        normalize.write_parquet_atomic(df, paths[sid])
    text = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    _write_text_atomic(text, paths["json"])
    _write_text_atomic(render_markdown(report), paths["md"])
    return paths


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.backtest.run",
        description="정규화 1분봉 [start, end) → 합성 전략 그리드 → 비용 → net 지표·게이트 리포트 (Phase 3)")
    p.add_argument("--start", required=True, type=normalize._parse_date, help="YYYY-MM-DD (포함, UTC)")
    p.add_argument("--end", required=True, type=normalize._parse_date,
                   help="YYYY-MM-DD (미포함, UTC — 반열린 [start, end))")
    p.add_argument("--symbol", default="XBTUSD")
    p.add_argument("--grid", type=Path, default=None,
                   help="축 → 값 목록 JSON (설계 그리드 부분집합, 생략 시 전체 2,268 run)")
    p.add_argument("--fee-profile", default="default", choices=sorted(costs.PROFILES),
                   help="수수료 프로필 (기본 default)")
    p.add_argument("--data-dir", type=Path, default=normalize.DEFAULT_OUT_DIR,
                   help=f"정규화 데이터 디렉터리 (기본 {normalize.DEFAULT_OUT_DIR})")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help=f"출력 디렉터리 (기본 {DEFAULT_OUT_DIR})")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = build_parser()
    a = parser.parse_args(argv)
    try:
        check_sample_range(a.start, a.end)
    except ValueError as e:
        parser.error(str(e))
    try:
        axes, params_list = load_grid(a.grid)
    except (OSError, ValueError) as e:  # json.JSONDecodeError 는 ValueError
        parser.error(f"--grid: {e}")
    gate = resolve_gate(None)

    t0 = time.monotonic()
    try:
        bars = load_bars(a.start, a.end - timedelta(days=1), a.symbol, out_dir=a.data_dir)  # store 는 종료일 포함
        log.info("1분봉 %d개 로드, run %d개 실행 (fee %s)", len(bars), len(params_list), a.fee_profile)
        net, summaries = run_grid(bars, params_list, a.fee_profile, a.start, a.end, gate)
        report = build_report(a.start, a.end, a.symbol, bars, axes, a.fee_profile, gate, summaries)
        paths = write_outputs(a.out, a.fee_profile, a.start, a.end, net, report)
    except Exception:
        log.exception("백테스트 실패")
        return 1
    log.info("완료 (%.1fs): %s", time.monotonic() - t0, paths["json"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
