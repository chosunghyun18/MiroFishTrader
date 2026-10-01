"""gross 라운드트립 `roundtrips` → 수수료·슬리피지를 반영한 net 라운드트립 `roundtrips_net`.

입력: `src.shared.schema.ROUNDTRIPS` 를 정확히 따르는 프레임(여분 열 불허 — 이중 적용 방지).
출력: 입력 24열(값·dtype·행 순서·인덱스 그대로) 뒤에 net 8열을 붙인 `ROUNDTRIPS_NET` 프레임.
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase3-backtest.md`
"체결 모델"·"비용 모델". 문서와 이 파일이 다르면 문서를 따른다. 파일 저장은 `run.py`(T-22) 범위.

    from src.backtest.costs import PROFILES, apply_costs, profile_from_config
    net = apply_costs(rt, "default")                 # 이름 또는 dict
    net = apply_costs(rt, profile_from_config(cfg))  # config backtest.* 에서
    net_f = apply_funding(net, funding, bars)        # 펀딩 반영(`ROUNDTRIPS_NET_FUNDING`)

| 항목 | 정의 |
|---|---|
| 유동성 | 진입 taker. 청산 `take_profit` → maker, 그 외(`stop`·`time`·`end_of_data`) → taker |
| 슬리피지 | `b = slippage_bps / 10_000`(maker 는 0). 매수 `p × (1 + b)`, 매도 `p × (1 − b)` |
| 매수/매도 | 롱 진입·숏 청산 = 매수, 숏 진입·롱 청산 = 매도 |
| 수수료 | `fee_xbt = qty / entry_fill × rate_entry + qty / exit_fill × rate_exit` (인버스, XBT) |
| 체결가 손익 | 롱 `qty × (1/entry_fill − 1/exit_fill)`, 숏 `qty × (1/exit_fill − 1/entry_fill)` |
| 슬리피지 비용 | `slippage_xbt = gross_pnl_xbt − 체결가 손익` |
| net | `net_pnl_xbt = 체결가 손익 − fee_xbt`, `net_ret = net_pnl_xbt / equity_before` |
| 펀딩 | 같은 `symbol` 펀딩 행 중 `ts ∈ (entry_ts, exit_ts]` 전부에 `funding_xbt = s × Σ qty / mark_T × rate_T` (롱 s = +1, 숏 −1, `mark_T` = 봉 `T − 1분` close), `net_pnl_xbt −= funding_xbt` |

- 프로필 dict 는 `PROFILE_KEYS` 세 키만 읽는다(여분 키는 무시). 세 값 모두 유한한 실수 ≥ 0, bool 거부.
- 행별 독립 계산이라 정렬하지 않는다(정렬은 metrics 책임).
- 펀딩(설계 "펀딩 모델"): 부과 대상은 펀딩 데이터 `ts` 그대로(격자 밖 행도 부과), 매일 04·12·20 UTC
  격자(`GRID_HOURS`)는 결측 검사에만 쓴다 — 보유 구간의 격자 시각에 펀딩 행이 없으면 `ValueError`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Real

import numpy as np
import pandas as pd

from src.ingest.bitmex_funding import GRID_HOURS
from src.shared.schema import (
    ROUNDTRIPS_NET,
    ROUNDTRIPS_NET_FUNDING,
    empty_frame,
    validate_funding,
    validate_roundtrips,
    validate_roundtrips_net,
    validate_roundtrips_net_funding,
)

PROFILE_KEYS = ("taker_fee", "maker_fee", "slippage_bps")

PROFILES: dict[str, dict[str, float]] = {
    "default": {"taker_fee": 0.0004, "maker_fee": 0.0002, "slippage_bps": 2.0},
    "bybit": {"taker_fee": 0.00055, "maker_fee": 0.0002, "slippage_bps": 2.0},
}


def _resolve_profile(profile: str | Mapping) -> dict[str, float]:
    """프로필 이름 또는 dict → 검증된 {taker_fee, maker_fee, slippage_bps}(float) 새 dict."""
    if isinstance(profile, str):
        if profile not in PROFILES:
            raise ValueError(f"알 수 없는 수수료 프로필: {profile!r} (허용 {sorted(PROFILES)})")
        profile = PROFILES[profile]
    if not isinstance(profile, Mapping):
        raise ValueError(f"수수료 프로필은 이름(str) 또는 dict 여야 한다: {type(profile).__name__}")
    missing = [k for k in PROFILE_KEYS if k not in profile]
    if missing:
        raise ValueError(f"수수료 프로필 키 누락: {missing}")
    out = {}
    for k in PROFILE_KEYS:
        v = profile[k]
        if isinstance(v, bool) or not isinstance(v, Real):
            raise ValueError(f"수수료 프로필 {k!r} 는 실수여야 한다: {v!r}")
        v = float(v)
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"수수료 프로필 {k!r} 는 유한한 값 ≥ 0 이어야 한다: {v!r}")
        out[k] = v
    return out


def profile_from_config(cfg: Mapping | None) -> dict[str, float]:
    """config 의 `backtest` 절에서 프로필 dict 를 만든다. 없는 키는 `default` 값."""
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, Mapping):
        raise ValueError(f"config 는 dict 여야 한다: {type(cfg).__name__}")
    bt = cfg.get("backtest") or {}
    if not isinstance(bt, Mapping):
        raise ValueError(f"config backtest 절은 dict 여야 한다: {type(bt).__name__}")
    merged = {k: bt.get(k, PROFILES["default"][k]) for k in PROFILE_KEYS}
    return _resolve_profile(merged)


def apply_costs(roundtrips: pd.DataFrame, profile: str | Mapping) -> pd.DataFrame:
    """gross roundtrips 에 net 8열을 붙인 새 프레임. 입력은 바꾸지 않는다."""
    prof = _resolve_profile(profile)
    validate_roundtrips(roundtrips, strict=True)
    if len(roundtrips) == 0:
        return empty_frame(ROUNDTRIPS_NET)

    out = roundtrips.copy()
    b = prof["slippage_bps"] / 10_000
    long = (out["side"] == "long").to_numpy(dtype=bool)
    maker_exit = (out["exit_reason"] == "take_profit").to_numpy(dtype=bool)
    qty = out["qty"].to_numpy(dtype=float)
    p_in = out["entry_price"].to_numpy(dtype=float)
    p_out = out["exit_price"].to_numpy(dtype=float)

    # 진입: 롱 = 매수(+), 숏 = 매도(−). 청산은 반대 방향, maker 면 b = 0.
    sign_in = np.where(long, 1.0, -1.0)
    b_out = np.where(maker_exit, 0.0, b)
    entry_fill = p_in * (1 + sign_in * b)
    exit_fill = p_out * (1 - sign_in * b_out)

    rate_out = np.where(maker_exit, prof["maker_fee"], prof["taker_fee"])
    fee = qty / entry_fill * prof["taker_fee"] + qty / exit_fill * rate_out
    fill_pnl = np.where(long,
                        qty * (1 / entry_fill - 1 / exit_fill),
                        qty * (1 / exit_fill - 1 / entry_fill))
    gross = out["gross_pnl_xbt"].to_numpy(dtype=float)
    net = fill_pnl - fee

    out["entry_liquidity"] = pd.array(["taker"] * len(out), dtype="string")
    out["exit_liquidity"] = pd.array(np.where(maker_exit, "maker", "taker"), dtype="string")
    out["entry_fill_price"] = entry_fill
    out["exit_fill_price"] = exit_fill
    out["fee_xbt"] = fee
    out["slippage_xbt"] = gross - fill_pnl
    out["net_pnl_xbt"] = net
    out["net_ret"] = net / out["equity_before"].to_numpy(dtype=float)
    return validate_roundtrips_net(out, strict=True)


BAR_KEYS = ("ts", "symbol", "close")
MISSING_SHOWN = 5


def _ns(values) -> np.ndarray:
    """UTC 시각 열 → int64 ns 배열."""
    return pd.DatetimeIndex(values).asi8


def _settlement_grid(lo_ns: int, hi_ns: int) -> np.ndarray:
    """`(lo, hi]` 의 기대 정산 격자(매일 `GRID_HOURS` UTC) — int64 ns 오름차순."""
    lo, hi = pd.Timestamp(lo_ns, tz="UTC"), pd.Timestamp(hi_ns, tz="UTC")
    day0 = lo.floor("D")
    parts = [pd.date_range(day0 + pd.Timedelta(hours=h), hi, freq="24h").asi8 for h in GRID_HOURS]
    grid = np.sort(np.concatenate(parts)) if parts else np.empty(0, dtype=np.int64)
    return grid[(grid > lo_ns) & (grid <= hi_ns)]


def _covered(n: int, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """길이 n 위치 중 `[lo_i, hi_i)` 어느 하나에라도 드는 위치 표시(bool)."""
    diff = np.zeros(n + 1, dtype=np.int64)
    np.add.at(diff, lo, 1)
    np.add.at(diff, hi, -1)
    return np.cumsum(diff[:-1]) > 0


def _check_grid(sym: str, entry: np.ndarray, exit_: np.ndarray, fts: np.ndarray) -> None:
    """보유 구간 `(entry, exit]` 의 격자 시각에 펀딩 행이 없으면 ValueError(설계 5항)."""
    grid = _settlement_grid(int(entry.min()), int(exit_.max()))
    missing = ~np.isin(grid, fts)
    if not missing.any():
        return
    lo = np.searchsorted(grid, entry, side="right")
    hi = np.searchsorted(grid, exit_, side="right")
    csum = np.concatenate(([0], np.cumsum(missing)))
    bad = (csum[hi] - csum[lo]) > 0
    if not bad.any():
        return
    times = grid[_covered(len(grid), lo[bad], hi[bad]) & missing]
    shown = ", ".join(str(pd.Timestamp(t, tz="UTC")) for t in times[:MISSING_SHOWN])
    raise ValueError(f"펀딩 결측 {sym}: 보유 구간 정산 격자 {len(times)}개 시각에 펀딩 행 없음 "
                     f"(앞 {min(len(times), MISSING_SHOWN)}개: {shown}), 라운드트립 {int(bad.sum())}행")


def _marks(sym: str, bars: pd.DataFrame, query_ns: np.ndarray) -> np.ndarray:
    """`query_ns`(= 정산 `T − 1분`) 각 시각의 같은 `symbol` 봉 close. 없거나 중복이면 ValueError."""
    sub = bars[(bars["symbol"] == sym).to_numpy(dtype=bool)]
    bts = _ns(sub["ts"])
    hit = np.isin(bts, query_ns)
    bts, close = bts[hit], sub["close"].to_numpy(dtype=float)[hit]
    if len(np.unique(bts)) != len(bts):
        dup = pd.Index(bts)[pd.Index(bts).duplicated()]
        raise ValueError(f"봉 ts 중복 {sym}: {[str(pd.Timestamp(t, tz='UTC')) for t in dup[:MISSING_SHOWN]]}")
    pos = pd.Index(bts).get_indexer(query_ns)
    if (pos < 0).any():
        miss = query_ns[pos < 0]
        raise ValueError(f"mark 봉 없음 {sym}: 정산 T − 1분 봉 {len(miss)}개 "
                         f"{[str(pd.Timestamp(t, tz='UTC')) for t in miss[:MISSING_SHOWN]]}")
    marks = close[pos]
    bad = ~(np.isfinite(marks) & (marks > 0))
    if bad.any():
        raise ValueError(f"mark 봉 close 가 유한한 양수가 아님 {sym}: {int(bad.sum())}개")
    return marks


def apply_funding(roundtrips_net: pd.DataFrame, funding: pd.DataFrame,
                  bars: pd.DataFrame) -> pd.DataFrame:
    """net roundtrips 에 펀딩 2열(`n_funding`·`funding_xbt`)을 붙이고 net 2열을 펀딩 포함 값으로 갱신.

    입력은 바꾸지 않는다. `bars` 는 run 과 같은 연속 1분봉(`ts`·`symbol`·`close` 만 읽는다).
    부과 = 같은 `symbol` 펀딩 행 중 `ts ∈ (entry_ts, exit_ts]`, mark = 봉 `ts − 1분` close.
    """
    validate_roundtrips_net(roundtrips_net, strict=True)
    validate_funding(funding)
    missing = [c for c in BAR_KEYS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars 누락 컬럼: {missing}")
    if len(roundtrips_net) == 0:
        return empty_frame(ROUNDTRIPS_NET_FUNDING)

    out = roundtrips_net.copy()
    m = len(out)
    entry, exit_ = _ns(out["entry_ts"]), _ns(out["exit_ts"])
    rt_sym = out["symbol"].to_numpy(dtype=object)
    qty = out["qty"].to_numpy(dtype=float)
    f_sym = funding["symbol"].to_numpy(dtype=object)
    f_ts_all = _ns(funding["ts"])
    f_rate_all = funding["funding_rate"].to_numpy(dtype=float)
    minute = pd.Timedelta(minutes=1).value

    n_funding = np.zeros(m, dtype=np.int64)
    total = np.zeros(m, dtype=float)
    for sym in pd.unique(rt_sym):
        rows = np.flatnonzero(rt_sym == sym)
        sel = f_sym == sym
        order = np.argsort(f_ts_all[sel], kind="stable")
        fts, rate = f_ts_all[sel][order], f_rate_all[sel][order]
        e, x = entry[rows], exit_[rows]
        _check_grid(sym, e, x, fts)

        lo = np.searchsorted(fts, e, side="right")
        hi = np.searchsorted(fts, x, side="right")
        cnt = hi - lo
        n_funding[rows] = cnt
        if cnt.max() == 0:
            continue
        used = np.flatnonzero(_covered(len(fts), lo, hi))
        mark = np.full(len(fts), np.nan)
        mark[used] = _marks(sym, bars, fts[used] - minute)

        q = qty[rows]
        acc = np.zeros(len(rows), dtype=float)
        for k in range(int(cnt.max())):  # 정산 시각 순 합산(설계 손 계산과 같은 순서)
            act = cnt > k
            j = lo[act] + k
            acc[act] = acc[act] + q[act] / mark[j] * rate[j]
        total[rows] = acc

    sign = np.where((out["side"] == "long").to_numpy(dtype=bool), 1.0, -1.0)
    funding_xbt = sign * total + 0.0  # 숏 0회의 −0.0 → 0.0
    net = out["net_pnl_xbt"].to_numpy(dtype=float) - funding_xbt
    out["net_pnl_xbt"] = net
    out["net_ret"] = net / out["equity_before"].to_numpy(dtype=float)
    out["n_funding"] = n_funding
    out["funding_xbt"] = funding_xbt
    return validate_roundtrips_net_funding(out, strict=True)
