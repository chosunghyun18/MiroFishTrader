"""net 라운드트립 한 run → 자본곡선·일 수익률·Sharpe·MDD·게이트 판정.

입력: `costs.apply_costs` 출력(`src.shared.schema.ROUNDTRIPS_NET`) 중 **한 run**(같은 `strategy_id`·`param_id`).
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase3-backtest.md`
"게이트 지표"·"config 대응"·"모듈". 문서와 이 파일이 다르면 문서를 따른다. JSON 직렬화(NaN → null)·파일 쓰기는
`run.py`(T-22) 범위라 여기서는 NaN 을 Python `float('nan')` 그대로 둔다.

    from src.backtest.metrics import summarize_net_run
    s = summarize_net_run(net, "2020-01-01", "2020-07-01", gate=cfg["backtest"]["gate"])
    s["gate"]  # "pass" | "fail" | "insufficient"

| 지표 | 정의 |
|---|---|
| 자본곡선 | `entry_ts` → `trade_id` stable 정렬. 첫 점 `(start, 1.0)`, 이후 `(exit_ts_k, E_{k−1} × (1 + net_ret_k))` |
| 일 수익률 | d ∈ [start, end) 의 UTC 날짜마다 `E_d` = 다음 날 00:00 UTC 전까지 청산된 거래를 반영한 자본, `r_d = E_d / E_{d−1} − 1`, `E_{start−1} = 1`. `T = (end − start).days` |
| Sharpe | `mean(r_d) / std(r_d, ddof=1) × √365`. `T < 2` · `std = 0` · 비유한 → NaN |
| MDD | 거래 단위 곡선에서 `max(1 − E / cummax(E))`. 0건 → 0.0. 보유 중 미실현 손실은 안 보이므로 하한값 |
| 누적 수익 | `Π(1 + net_ret) − 1`, `Π(1 + gross_ret) − 1`. 0건 → 0.0 |
| 강제청산 위반 | 롱 `exit_price ≤ entry_price / 1.095`, 숏 `exit_price ≥ entry_price / 0.905` 인 거래 수 |
| 모멘트 | `sr_daily = mean/std(ddof=1)`, `skew = m3/m2^1.5`, `kurt = m4/m2²`(비초과, 정규 = 3, 모집단 식) |
| 게이트 | `n_trades < min_trades` → `insufficient`, 그 외 `sharpe ≥ min_sharpe` 그리고 `mdd ≤ max_drawdown` → `pass`, 아니면 `fail`(NaN sharpe 포함) |

- 한계: `net_ret = net_pnl / equity_before`(gross 경로 자본)라 `Π(1 + net_ret)` 은 실제 net 자본과 미세하게 다를 수 있다.
  문서 정의를 그대로 따른다.
- `std = 0` 은 정확히 0 일 때만이다(문서 정의). 상수 수익도 복리 나눗셈 오차로 ~1e-18 흩어지면 유한한 큰 값이 된다.
- 입력은 바꾸지 않는다. 행 순서와 무관하고 같은 입력 → 같은 출력.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Integral, Real

import numpy as np
import pandas as pd

from src.shared.schema import validate_roundtrips_net

GATE_KEYS = ("min_trades", "min_sharpe", "max_drawdown")
DEFAULT_GATE: dict = {"min_trades": 100, "min_sharpe": 1.0, "max_drawdown": 0.30}
GATE_VERDICTS = ("pass", "fail", "insufficient")
SUMMARY_KEYS = ("strategy_id", "param_id", "start", "end", "n_trades", "sharpe", "mdd",
                "total_net_ret", "total_gross_ret", "n_liq_breach", "sr_daily", "skew_daily",
                "kurt_daily", "n_days", "gate")

ANNUALIZE = math.sqrt(365)
LIQ_LONG = 1.095   # 롱 청산: exit ≤ entry / 1.095
LIQ_SHORT = 0.905  # 숏 청산: exit ≥ entry / 0.905
_DAY = pd.Timedelta(days=1)


def _ts(x, name: str) -> pd.Timestamp:
    """문자열·Timestamp → UTC Timestamp. tz 없으면 UTC 로 본다."""
    try:
        t = pd.Timestamp(x)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} 를 시각으로 읽을 수 없다: {x!r}") from e
    if t is pd.NaT:
        raise ValueError(f"{name} 가 NaT 다")
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _day(x, name: str) -> pd.Timestamp:
    t = _ts(x, name)
    if t != t.normalize():
        raise ValueError(f"{name} 는 UTC 자정이어야 한다: {t}")
    return t


def _sorted_run(rt: pd.DataFrame) -> pd.DataFrame:
    """검증 → 한 run 확인 → entry_ts·trade_id stable 정렬한 복사본."""
    validate_roundtrips_net(rt, strict=True)
    runs = rt[["strategy_id", "param_id"]].drop_duplicates()
    if len(runs) > 1:
        raise ValueError(f"한 run 만 받는다: (strategy_id, param_id) {len(runs)}개")
    return rt.sort_values(["entry_ts", "trade_id"], kind="mergesort").reset_index(drop=True)


def _curve(srt: pd.DataFrame, start) -> pd.DataFrame:
    if start is None:
        if len(srt) == 0:
            raise ValueError("0건 run 은 start 가 있어야 자본곡선을 만들 수 있다")
        start = srt["entry_ts"].iloc[0]
    start = _ts(start, "start")
    eq = np.concatenate([[1.0], np.cumprod(1.0 + srt["net_ret"].to_numpy(dtype=float))])
    ts = pd.DatetimeIndex([start]).append(pd.DatetimeIndex(srt["exit_ts"]))
    return pd.DataFrame({"ts": ts, "equity": eq})


def equity_curve(rt: pd.DataFrame, start=None) -> pd.DataFrame:
    """[ts, equity] 프레임. 첫 행 `(start, 1.0)`(start 없으면 첫 `entry_ts`), 이후 거래마다 청산 시점 자본.

    0건이면 `(start, 1.0)` 한 행. 0건인데 start 도 없으면 `ValueError`.
    """
    return _curve(_sorted_run(rt), start)


def daily_returns(curve: pd.DataFrame, start, end) -> pd.Series:
    """UTC 날짜 [start, end) 의 일 수익률 Series(index = 자정 Timestamp, T 개). 거래 없는 날은 0."""
    start, end = _day(start, "start"), _day(end, "end")
    if end <= start:
        raise ValueError(f"end 는 start 보다 뒤여야 한다: {start} ~ {end}")
    ts = pd.DatetimeIndex(curve["ts"]).tz_convert("UTC")
    eq = curve["equity"].to_numpy(dtype=float)
    if len(eq) == 0:
        raise ValueError("자본곡선이 비었다(첫 점 (start, 1.0) 필요)")
    pts = ts[1:]
    if len(pts) and ((pts < start).any() or (pts >= end).any()):
        raise ValueError(f"자본곡선 시점이 [{start}, {end}) 밖에 있다")
    days = pd.date_range(start, end, freq="D", inclusive="left")
    # 곡선 순서에서 k 번째 점은 앞선 모든 점이 청산된 뒤에야 반영된다 → 시점의 누적 최댓값으로 센다.
    reach = np.maximum.accumulate(pts.asi8) if len(pts) else np.array([], dtype=np.int64)
    n_done = np.searchsorted(reach, (days + _DAY).asi8, side="left")
    e_day = eq[n_done]
    prev = np.concatenate([[eq[0]], e_day[:-1]])
    return pd.Series(e_day / prev - 1.0, index=days, name="ret")


def sharpe(daily) -> float:
    """연환산 Sharpe(√365, ddof=1). `T < 2`·`std = 0`·비유한 → NaN."""
    r = np.asarray(daily, dtype=float)
    if len(r) < 2 or not np.isfinite(r).all():
        return float("nan")
    sd = float(np.std(r, ddof=1))
    if sd == 0 or not math.isfinite(sd):
        return float("nan")
    return float(np.mean(r) / sd * ANNUALIZE)


def max_drawdown(curve) -> float:
    """거래 단위 곡선의 MDD(0~1). 곡선 1점(0건) → 0.0. `curve` 는 [ts, equity] 프레임 또는 자본 배열."""
    eq = curve["equity"] if isinstance(curve, pd.DataFrame) else curve
    eq = np.asarray(eq, dtype=float)
    if len(eq) < 2:
        return 0.0
    return float(np.max(1.0 - eq / np.maximum.accumulate(eq)))


def _n_liq_breach(rt: pd.DataFrame) -> int:
    long = (rt["side"] == "long").to_numpy(dtype=bool)
    p_in = rt["entry_price"].to_numpy(dtype=float)
    p_out = rt["exit_price"].to_numpy(dtype=float)
    hit = np.where(long, p_out <= p_in / LIQ_LONG, p_out >= p_in / LIQ_SHORT)
    return int(hit.sum())


def daily_moments(daily) -> dict:
    """DSR 입력 {sr_daily, skew_daily, kurt_daily, n_days}. 정의 불가는 NaN."""
    r = np.asarray(daily, dtype=float)
    n = len(r)
    nan = float("nan")
    out = {"sr_daily": nan, "skew_daily": nan, "kurt_daily": nan, "n_days": int(n)}
    if n == 0 or not np.isfinite(r).all():
        return out
    d = r - r.mean()
    m2 = float(np.mean(d ** 2))
    if n >= 2:
        sd = float(np.std(r, ddof=1))
        if sd != 0:
            out["sr_daily"] = float(r.mean() / sd)
    if m2 != 0:
        out["skew_daily"] = float(np.mean(d ** 3) / m2 ** 1.5)
        out["kurt_daily"] = float(np.mean(d ** 4) / m2 ** 2)
    return out


def _resolve_gate(gate: Mapping | None) -> dict:
    """DEFAULT_GATE 위에 gate 를 덮어쓴 검증된 dict. 누락 키 = 기본값, 여분 키 무시."""
    if gate is None:
        gate = {}
    if not isinstance(gate, Mapping):
        raise ValueError(f"gate 는 dict 여야 한다: {type(gate).__name__}")
    out = {}
    for k in GATE_KEYS:
        v = gate.get(k, DEFAULT_GATE[k])
        if isinstance(v, bool) or not isinstance(v, Real):
            raise ValueError(f"gate {k!r} 는 실수여야 한다: {v!r}")
        if k == "min_trades":
            if not (isinstance(v, Integral) or float(v).is_integer()) or v < 0:
                raise ValueError(f"gate 'min_trades' 는 정수 ≥ 0 이어야 한다: {v!r}")
            out[k] = int(v)
            continue
        v = float(v)
        if not math.isfinite(v):
            raise ValueError(f"gate {k!r} 는 유한해야 한다: {v!r}")
        if k == "max_drawdown" and v < 0:
            raise ValueError(f"gate 'max_drawdown' 는 ≥ 0 이어야 한다: {v!r}")
        out[k] = v
    return out


def gate_verdict(summary: Mapping, gate: Mapping | None = None) -> str:
    """판정 규칙 1~3 → "pass" | "fail" | "insufficient". 거래 수 미달이 다른 지표보다 우선."""
    g = _resolve_gate(gate)
    if summary["n_trades"] < g["min_trades"]:
        return "insufficient"
    s, mdd = float(summary["sharpe"]), float(summary["mdd"])
    if math.isnan(s) or math.isnan(mdd):
        return "fail"
    return "pass" if s >= g["min_sharpe"] and mdd <= g["max_drawdown"] else "fail"


def summarize_net_run(rt: pd.DataFrame, start, end, gate: Mapping | None = None) -> dict:
    """run 요약 dict(문서 출력 15키, 그 순서). start/end 는 "YYYY-MM-DD", 수치는 Python float/int."""
    g = _resolve_gate(gate)
    start_d, end_d = _day(start, "start"), _day(end, "end")
    srt = _sorted_run(rt)
    curve = _curve(srt, start_d)
    daily = daily_returns(curve, start_d, end_d)
    n = len(srt)
    s = {
        "strategy_id": str(srt["strategy_id"].iloc[0]) if n else None,
        "param_id": str(srt["param_id"].iloc[0]) if n else None,
        "start": start_d.strftime("%Y-%m-%d"),
        "end": end_d.strftime("%Y-%m-%d"),
        "n_trades": int(n),
        "sharpe": sharpe(daily),
        "mdd": max_drawdown(curve),
        "total_net_ret": float(curve["equity"].iloc[-1] - 1.0),
        "total_gross_ret": float(np.prod(1.0 + srt["gross_ret"].to_numpy(dtype=float)) - 1.0),
        "n_liq_breach": _n_liq_breach(srt),
    }
    s.update(daily_moments(daily.to_numpy()))
    s["gate"] = gate_verdict(s, g)
    return {k: s[k] for k in SUMMARY_KEYS}
