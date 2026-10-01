"""gross 라운드트립 `roundtrips` → 수수료·슬리피지를 반영한 net 라운드트립 `roundtrips_net`.

입력: `src.shared.schema.ROUNDTRIPS` 를 정확히 따르는 프레임(여분 열 불허 — 이중 적용 방지).
출력: 입력 24열(값·dtype·행 순서·인덱스 그대로) 뒤에 net 8열을 붙인 `ROUNDTRIPS_NET` 프레임.
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase3-backtest.md`
"체결 모델"·"비용 모델". 문서와 이 파일이 다르면 문서를 따른다. 파일 저장은 `run.py`(T-22) 범위.

    from src.backtest.costs import PROFILES, apply_costs, profile_from_config
    net = apply_costs(rt, "default")                 # 이름 또는 dict
    net = apply_costs(rt, profile_from_config(cfg))  # config backtest.* 에서

| 항목 | 정의 |
|---|---|
| 유동성 | 진입 taker. 청산 `take_profit` → maker, 그 외(`stop`·`time`·`end_of_data`) → taker |
| 슬리피지 | `b = slippage_bps / 10_000`(maker 는 0). 매수 `p × (1 + b)`, 매도 `p × (1 − b)` |
| 매수/매도 | 롱 진입·숏 청산 = 매수, 숏 진입·롱 청산 = 매도 |
| 수수료 | `fee_xbt = qty / entry_fill × rate_entry + qty / exit_fill × rate_exit` (인버스, XBT) |
| 체결가 손익 | 롱 `qty × (1/entry_fill − 1/exit_fill)`, 숏 `qty × (1/exit_fill − 1/entry_fill)` |
| 슬리피지 비용 | `slippage_xbt = gross_pnl_xbt − 체결가 손익` |
| net | `net_pnl_xbt = 체결가 손익 − fee_xbt`, `net_ret = net_pnl_xbt / equity_before` |

- 프로필 dict 는 `PROFILE_KEYS` 세 키만 읽는다(여분 키는 무시). 세 값 모두 유한한 실수 ≥ 0, bool 거부.
- 행별 독립 계산이라 정렬하지 않는다(정렬은 metrics 책임).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Real

import numpy as np
import pandas as pd

from src.shared.schema import (
    ROUNDTRIPS_NET,
    empty_frame,
    validate_roundtrips,
    validate_roundtrips_net,
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
