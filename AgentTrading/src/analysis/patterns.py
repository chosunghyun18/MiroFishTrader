"""라운드트립 `roundtrips` → run(`strategy_id`, `param_id`) 별 분포 요약.

입력: `src.shared.schema.ROUNDTRIPS` 를 따르는 라운드트립 프레임(여분 열 허용 — Phase 3 net 열 대비).
출력: run 하나당 평평한 dict 1개. 값은 파이썬 기본형(int·float·str·None·dict)과 시각 `pd.Timestamp`(UTC).
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase2-synthetic-strategy.md`
"분포 지표" 표. 문서와 이 파일이 다르면 문서를 따른다. 파일 저장·JSON 직렬화는 `run.py`(T-15) 범위.

    from src.analysis.patterns import summarize_run, summarize_runs
    one = summarize_run(rt_one_run, skipped_min_qty=3)
    many = summarize_runs(rt, skipped_min_qty={("syn-v1-h1", pid): 3})
    table = pd.DataFrame(many)                       # 와이드 표가 필요하면

| 키 | 정의 | 단위 |
|---|---|---|
| n_trades / skipped_min_qty | 행 수 / 생성기에서 전달받은 건너뛴 신호 수 | 건 |
| entry_reason_count / _share | {entry_reason: {side: 건수}} / 건수 ÷ n_trades | 건, 비율 |
| exit_reason_count / _share | {exit_reason: 건수} / 건수 ÷ n_trades | 건, 비율 |
| holding_min_{p10,p50,p90,mean,max} | holding_min | 분 |
| notional_pct_{p10,p50,p90,mean} | leverage × 100 | % |
| size_capped_share | size_capped 비율 | 비율 |
| leverage_{p10,p50,p90,max} | leverage | 배 |
| win_rate | gross_pnl_xbt > 0 비율(0 은 패) | 비율 |
| payoff_ratio | 이긴 거래 gross_ret 평균 / |진 거래 gross_ret 평균| | 배 |
| expectancy_ret | gross_ret 평균 × 100 | % |
| stop_overshoot_{max,mean} | stop 청산 거래의 −gross_ret×100 − 예정 손실 % | %p |
| max_consec_losses | entry_ts 순 gross_pnl_xbt ≤ 0 최장 연속 길이 | 건 |
| max_consec_loss_{start,end,ret} | 최장 구간 첫 entry_ts·마지막 exit_ts·(Π(1+r)−1)×100 | 시각, % |
| total_ret | (Π(1+gross_ret)−1)×100 | % |

- 모두 gross(비용 전). 백분위는 `numpy.percentile` 기본(linear). 계산 전 `entry_ts` 기준 stable 정렬.
- 거래 0건: 개수 지표(n_trades·max_consec_losses) 0, 빈도 dict `{}`, 나머지 None.
- payoff_ratio 는 진 거래 0건·이긴 거래 0건·진 거래 평균 0 이면 None.
- 최장 연속 손실 구간이 여러 개면 먼저 나온 구간. 손실 0건이면 구간 키는 None.
- 사유 빈도는 관측된 값만 키로 둔다(0건 사유는 키 없음).
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from src.shared.schema import ROUNDTRIPS, validate

RUN_KEY = ("strategy_id", "param_id")

SUMMARY_KEYS = (
    "strategy_id", "param_id", "n_trades", "skipped_min_qty",
    "entry_reason_count", "entry_reason_share", "exit_reason_count", "exit_reason_share",
    "holding_min_p10", "holding_min_p50", "holding_min_p90", "holding_min_mean", "holding_min_max",
    "notional_pct_p10", "notional_pct_p50", "notional_pct_p90", "notional_pct_mean",
    "size_capped_share",
    "leverage_p10", "leverage_p50", "leverage_p90", "leverage_max",
    "win_rate", "payoff_ratio", "expectancy_ret",
    "stop_overshoot_max", "stop_overshoot_mean",
    "max_consec_losses", "max_consec_loss_start", "max_consec_loss_end", "max_consec_loss_ret",
    "total_ret",
)

_PCTS = (10, 50, 90)


def _f(x) -> float:
    return float(x)


def _compound_pct(ret: np.ndarray) -> float:
    return float((np.prod(1.0 + ret) - 1.0) * 100.0)


def _max_consec_losses(pnl: np.ndarray) -> tuple[int, int | None, int | None]:
    """pnl ≤ 0 최장 연속 (길이, 시작 위치, 끝 위치). 같은 길이면 먼저 나온 구간, 없으면 (0, None, None)."""
    best, best_start = 0, None
    run, run_start = 0, 0
    for i, v in enumerate(pnl):
        if v <= 0:
            if run == 0:
                run_start = i
            run += 1
            if run > best:
                best, best_start = run, run_start
        else:
            run = 0
    if best == 0:
        return 0, None, None
    return best, best_start, best_start + best - 1


def _planned_loss_pct(rt: pd.DataFrame) -> np.ndarray:
    """손절가 체결 시 예정 손실(자본 대비 %) = qty × |1/entry − 1/stop| / equity_before × 100."""
    qty = rt["qty"].to_numpy(dtype=float)
    inv = np.abs(1.0 / rt["entry_price"].to_numpy() - 1.0 / rt["stop_price"].to_numpy())
    return qty * inv / rt["equity_before"].to_numpy() * 100.0


def _empty_summary(skipped_min_qty) -> dict:
    out = dict.fromkeys(SUMMARY_KEYS)
    out.update(
        n_trades=0, skipped_min_qty=skipped_min_qty, max_consec_losses=0,
        entry_reason_count={}, entry_reason_share={}, exit_reason_count={}, exit_reason_share={},
    )
    return out


def summarize_run(rt: pd.DataFrame, skipped_min_qty: int | None = None) -> dict:
    """한 run 의 라운드트립 → 분포 요약 dict(키 순서 `SUMMARY_KEYS`). run 이 2개 이상 섞이면 ValueError."""
    validate(rt, ROUNDTRIPS, strict=False)
    if skipped_min_qty is not None:
        skipped_min_qty = int(skipped_min_qty)
    runs = rt[list(RUN_KEY)].drop_duplicates()
    if len(runs) > 1:
        raise ValueError(f"run 이 {len(runs)}개 섞여 있음 — summarize_runs 를 쓸 것")
    if len(rt) == 0:
        return _empty_summary(skipped_min_qty)

    rt = rt.sort_values("entry_ts", kind="mergesort").reset_index(drop=True)
    n = len(rt)
    out: dict = {"strategy_id": str(rt["strategy_id"].iat[0]),
                 "param_id": str(rt["param_id"].iat[0]),
                 "n_trades": n, "skipped_min_qty": skipped_min_qty}

    entry_count: dict = {}
    for (reason, side), c in rt.groupby(["entry_reason", "side"], sort=True).size().items():
        if c:
            entry_count.setdefault(str(reason), {})[str(side)] = int(c)
    out["entry_reason_count"] = entry_count
    out["entry_reason_share"] = {
        r: {s: c / n for s, c in sides.items()} for r, sides in entry_count.items()}
    exit_count = {str(r): int(c)
                  for r, c in rt.groupby("exit_reason", sort=True).size().items() if c}
    out["exit_reason_count"] = exit_count
    out["exit_reason_share"] = {r: c / n for r, c in exit_count.items()}

    hold = rt["holding_min"].to_numpy()
    for p in _PCTS:
        out[f"holding_min_p{p}"] = _f(np.percentile(hold, p))
    out["holding_min_mean"] = _f(hold.mean())
    out["holding_min_max"] = _f(hold.max())

    lev = rt["leverage"].to_numpy()
    notional = lev * 100.0
    for p in _PCTS:
        out[f"notional_pct_p{p}"] = _f(np.percentile(notional, p))
    out["notional_pct_mean"] = _f(notional.mean())
    out["size_capped_share"] = _f(rt["size_capped"].to_numpy().mean())
    for p in _PCTS:
        out[f"leverage_p{p}"] = _f(np.percentile(lev, p))
    out["leverage_max"] = _f(lev.max())

    pnl = rt["gross_pnl_xbt"].to_numpy()
    ret = rt["gross_ret"].to_numpy()
    win = pnl > 0
    out["win_rate"] = _f(win.mean())
    payoff = None
    if win.any() and (~win).any():
        loss_mean = ret[~win].mean()
        if loss_mean != 0:
            payoff = _f(ret[win].mean() / abs(loss_mean))
    out["payoff_ratio"] = payoff
    out["expectancy_ret"] = _f(ret.mean() * 100.0)

    stop = (rt["exit_reason"] == "stop").to_numpy()
    if stop.any():
        over = -ret[stop] * 100.0 - _planned_loss_pct(rt.loc[stop])
        out["stop_overshoot_max"] = _f(over.max())
        out["stop_overshoot_mean"] = _f(over.mean())
    else:
        out["stop_overshoot_max"] = out["stop_overshoot_mean"] = None

    length, start, end = _max_consec_losses(pnl)
    out["max_consec_losses"] = length
    if length:
        out["max_consec_loss_start"] = pd.Timestamp(rt["entry_ts"].iat[start])
        out["max_consec_loss_end"] = pd.Timestamp(rt["exit_ts"].iat[end])
        out["max_consec_loss_ret"] = _compound_pct(ret[start:end + 1])
    else:
        out["max_consec_loss_start"] = out["max_consec_loss_end"] = None
        out["max_consec_loss_ret"] = None
    out["total_ret"] = _compound_pct(ret)

    return {k: out[k] for k in SUMMARY_KEYS}


def summarize_runs(rt: pd.DataFrame,
                   skipped_min_qty: Mapping[tuple[str, str], int] | None = None) -> list[dict]:
    """여러 run 이 섞인 라운드트립 → (`strategy_id`, `param_id`) 정렬 순 run 별 요약 목록."""
    validate(rt, ROUNDTRIPS, strict=False)
    skipped = skipped_min_qty or {}
    out = []
    for (sid, pid), part in rt.groupby(list(RUN_KEY), sort=True):
        out.append(summarize_run(part, skipped.get((str(sid), str(pid)))))
    return out
