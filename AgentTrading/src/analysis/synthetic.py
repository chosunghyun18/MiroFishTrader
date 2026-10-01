"""합성 전략 규칙 × 1분봉 `bars_1m` → 가상 라운드트립 `roundtrips`(+ 파생 synthetic `fills`).

입력: 한 심볼의 1분봉(`src.shared.schema.BARS_1M`), `ts` 오름차순·정확히 1분 연속(검증은 features 에 위임).
출력: `SyntheticRun(roundtrips, skipped_min_qty, halted)`. `roundtrips` 는 `ROUNDTRIPS` 스키마를 통과한다.
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase2-synthetic-strategy.md`
규칙 R1~R5·H1~H3·X1~X4·P1~P4, "사이징 공식", "라운드트립 스키마", "fills 와의 관계", "미래 정보 누수 방지".
문서와 이 파일이 다르면 문서를 따른다. 파일 저장·그리드 실행 CLI 는 `run.py`(T-15) 범위.

    from src.analysis.synthetic import Params, generate_run, roundtrips_to_fills
    res = generate_run(bars, Params(trigger="h1", n=60, stop_pct=1, tp_r=2, max_hold=240, risk_pct=2))
    res.roundtrips, res.skipped_min_qty, res.halted
    fills = roundtrips_to_fills(res.roundtrips)

| 규칙 | 구현 |
|---|---|
| H1 / H2 / H3 (봉 t 마감) | close > donchian_high_n · < donchian_low_n / mom_n > ±k·sigma_n (sigma > 0) / z_n ≤ −k · ≥ +k |
| 진입 | 신호 봉 t 의 다음 봉 t+1 open 체결. 마지막 봉 신호는 버림 |
| X1 손절 / X2 익절 | 진입 봉부터 봉 low·high 로 판정, 손절가·익절가 체결. 진입 이후 봉 open 이 밖이면(갭) open 체결. 둘 다 닿으면 손절 |
| X3 시간 | 봉 e+max_hold−1 마감 신호 → 봉 e+max_hold open 체결(그 봉 open 이 손절가 밖이어도 사유 time) |
| X4 구간 끝 | 마지막 봉 마감에 남은 포지션은 그 봉 close 체결(X3 신호 봉이 마지막 봉이어도 X4) |
| P1·P2 | 동시 포지션 1개, 보유 중 신호 무시, 반전 없음 |
| P3 | X1·X2 청산 봉 마감에 다시 평가, X3·X4 신호 봉 마감 신호는 버림(X3 체결 봉 마감부터 평가) |
| P4 | 계약 수 < 1 이면 건너뛰고 skipped_min_qty += 1, 청산 후 자본 ≤ 0 이면 halted=True 로 중단 |
| 사이징 | 롱 q = r·E·p·(1−s)/s, 숏 q = r·E·p·(1+s)/s, 상한 4·E·p, floor. 예정 손실 > 30%·E 이면 ValueError |

- 손익은 인버스 공식(XBT)·gross 다. 수수료·슬리피지·펀딩·강제청산·틱 반올림은 Phase 3 범위.
- 하드 가드(R4) `ValueError` 는 `generate_run` 이 잡지 않고 전파한다(`halted` 는 P4 자본 소진 전용).
- 0행 bars 는 0행 ROUNDTRIPS 를 돌려준다. 난수를 쓰지 않아 같은 입력·파라미터면 결과가 같다.
"""

from __future__ import annotations

import itertools
import math
import numbers
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.analysis.features import compute_features
from src.shared.schema import (
    FILLS,
    ROUNDTRIPS,
    empty_frame,
    validate_fills,
    validate_roundtrips,
)

RULESET_VERSION = "v1"
RISK_GUARD_PCT = 30.0
MAX_LEVERAGE = 10.0
MAX_EXPOSURE = 4.0  # R2(10배)는 R3(4배)에 지배되어 사이징에서는 4배만 자른다.
TRIGGERS = ("h1", "h2", "h3")
ENTRY_REASON = {"h1": "h1_breakout", "h2": "h2_momentum", "h3": "h3_meanrev"}
ONE_MIN = pd.Timedelta(minutes=1)

# 설계 문서 "파라미터 범위" 그리드. 그리드 밖 값 거부는 실행 진입점(T-15)이 한다.
GRID = {
    "trigger": TRIGGERS,
    "n": (15, 60, 240),
    "k": (1.5, 2.0, 2.5),
    "stop_pct": (0.5, 1.0, 2.0),
    "tp_r": (1.0, 2.0, 3.0, None),
    "max_hold": (60, 240, 1440),
    "risk_pct": (1.0, 2.0, 5.0),
}


def _is_int(v) -> bool:
    return isinstance(v, numbers.Integral) and not isinstance(v, bool)


def _is_pos_real(v) -> bool:
    return (isinstance(v, numbers.Real) and not isinstance(v, bool)
            and math.isfinite(v) and v > 0)


@dataclass(frozen=True)
class Params:
    """한 run 의 파라미터. 형식·범위만 검사한다(그리드 소속은 검사하지 않음)."""

    trigger: str
    n: int
    stop_pct: float
    tp_r: float | None
    max_hold: int
    risk_pct: float
    k: float | None = None

    def __post_init__(self):
        if self.trigger not in TRIGGERS:
            raise ValueError(f"trigger 는 {TRIGGERS} 중 하나: {self.trigger!r}")
        if not _is_int(self.n) or self.n < 2:
            raise ValueError(f"n 은 2 이상 정수: {self.n!r}")
        if self.trigger == "h1":
            if self.k is not None:
                raise ValueError("h1 은 k 를 쓰지 않는다")
        elif not _is_pos_real(self.k):
            raise ValueError(f"{self.trigger} 는 k > 0 필요: {self.k!r}")
        if not _is_pos_real(self.stop_pct) or self.stop_pct >= 100:
            raise ValueError(f"stop_pct 는 0 < s < 100: {self.stop_pct!r}")
        if self.tp_r is not None and not _is_pos_real(self.tp_r):
            raise ValueError(f"tp_r 는 > 0 또는 None: {self.tp_r!r}")
        if not _is_int(self.max_hold) or self.max_hold < 1:
            raise ValueError(f"max_hold 는 1 이상 정수: {self.max_hold!r}")
        if not _is_pos_real(self.risk_pct):
            raise ValueError(f"risk_pct 는 > 0: {self.risk_pct!r}")

    @property
    def strategy_id(self) -> str:
        return f"syn-{RULESET_VERSION}-{self.trigger}"

    @property
    def entry_reason(self) -> str:
        return ENTRY_REASON[self.trigger]

    @property
    def param_id(self) -> str:
        """키 알파벳 정렬 `key=value` 를 `;` 로 이은 정규 문자열(H1 은 k 생략, tp_r 없음은 none)."""
        items = {
            "max_hold": self.max_hold, "n": self.n, "risk_pct": self.risk_pct,
            "stop_pct": self.stop_pct, "tp_r": self.tp_r, "trigger": self.trigger,
        }
        if self.trigger != "h1":
            items["k"] = self.k

        def fmt(v):
            if v is None:
                return "none"
            if isinstance(v, str):
                return v
            return f"{v:g}"

        return ";".join(f"{key}={fmt(items[key])}" for key in sorted(items))


def param_grid(trigger: str | None = None) -> list[Params]:
    """설계 그리드 전개(H1 324·H2 972·H3 972). (trigger, param_id) 순으로 정렬."""
    triggers = TRIGGERS if trigger is None else (trigger,)
    out = []
    for trg in triggers:
        if trg not in TRIGGERS:
            raise ValueError(f"trigger 는 {TRIGGERS} 중 하나: {trg!r}")
        ks = (None,) if trg == "h1" else GRID["k"]
        for n, k, s, tp, mh, r in itertools.product(
                GRID["n"], ks, GRID["stop_pct"], GRID["tp_r"], GRID["max_hold"], GRID["risk_pct"]):
            out.append(Params(trigger=trg, n=n, k=k, stop_pct=s, tp_r=tp, max_hold=mh, risk_pct=r))
    out.sort(key=lambda p: (p.trigger, p.param_id))
    return out


def entry_signals(bars: pd.DataFrame, feats: pd.DataFrame, params: Params) -> tuple[np.ndarray, np.ndarray]:
    """봉 t 마감 기준 (롱, 숏) 진입 신호 bool 배열. NaN 지표는 신호 없음."""
    n = params.n
    with np.errstate(invalid="ignore"):
        if params.trigger == "h1":
            close = bars["close"].to_numpy(dtype="float64")
            long = close > feats[f"donchian_high_{n}"].to_numpy(dtype="float64")
            short = close < feats[f"donchian_low_{n}"].to_numpy(dtype="float64")
        elif params.trigger == "h2":
            mom = feats[f"mom_{n}"].to_numpy(dtype="float64")
            sig = feats[f"sigma_{n}"].to_numpy(dtype="float64")
            ok = sig > 0
            long = ok & (mom > params.k * sig)
            short = ok & (mom < -params.k * sig)
        else:
            z = feats[f"z_{n}"].to_numpy(dtype="float64")
            long = z <= -params.k
            short = z >= params.k
    assert not (long & short).any(), "롱·숏 신호가 같은 봉에서 동시에 참"
    return long, short


def inverse_pnl(side: str, qty: float, entry: float, exit_: float) -> float:
    """인버스 계약 손익(XBT): 롱 qty(1/entry − 1/exit), 숏 qty(1/exit − 1/entry)."""
    if side == "long":
        return qty * (1.0 / entry - 1.0 / exit_)
    return qty * (1.0 / exit_ - 1.0 / entry)


def position_size(equity: float, entry_price: float, side: str,
                  stop_pct: float, risk_pct: float) -> tuple[int, bool]:
    """설계 "사이징 공식" 1~5단계 → (계약 수, 4배 상한 절단 여부). 예정 손실 > 30%·E 이면 ValueError."""
    s = stop_pct / 100.0
    r = risk_pct / 100.0
    e, p = equity, entry_price
    if side == "long":
        q_risk = r * e * p * (1 - s) / s
        stop = p * (1 - s)
    else:
        q_risk = r * e * p * (1 + s) / s
        stop = p * (1 + s)
    q_cap = MAX_EXPOSURE * e * p
    qty = math.floor(min(q_risk, q_cap))
    planned_loss = -inverse_pnl(side, qty, p, stop)
    if planned_loss > RISK_GUARD_PCT / 100.0 * e:
        raise ValueError(
            f"R4 하드 가드: 예정 손실 {planned_loss:.6g} XBT > 자본의 {RISK_GUARD_PCT:g}% "
            f"(equity={e:g}, stop_pct={stop_pct:g}, risk_pct={risk_pct:g})")
    return qty, bool(q_risk > q_cap)


@dataclass(frozen=True)
class SyntheticRun:
    """run 결과: 라운드트립 프레임 + P4 요약(patterns.summarize_run 의 skipped_min_qty 입력)."""

    roundtrips: pd.DataFrame
    skipped_min_qty: int
    halted: bool


def _exit_levels(side: str, entry: float, params: Params) -> tuple[float, float]:
    s = params.stop_pct / 100.0
    if side == "long":
        stop = entry * (1 - s)
        tp = entry * (1 + params.tp_r * s) if params.tp_r is not None else math.nan
    else:
        stop = entry * (1 + s)
        tp = entry * (1 - params.tp_r * s) if params.tp_r is not None else math.nan
    return stop, tp


def _intrabar_exit(side, o, h, lo, stop, tp, gap_ok):
    """봉 하나의 X1·X2 판정 → (사유, 가격) 또는 None. 갭(open) → 손절 닿음 → 익절 닿음 순."""
    has_tp = not math.isnan(tp)
    if side == "long":
        if gap_ok and o <= stop:
            return "stop", o
        if gap_ok and has_tp and o >= tp:
            return "take_profit", o
        if lo <= stop:
            return "stop", stop
        if has_tp and h >= tp:
            return "take_profit", tp
    else:
        if gap_ok and o >= stop:
            return "stop", o
        if gap_ok and has_tp and o <= tp:
            return "take_profit", o
        if h >= stop:
            return "stop", stop
        if has_tp and lo <= tp:
            return "take_profit", tp
    return None


def generate_run(bars: pd.DataFrame, params: Params, initial_equity: float = 1.0) -> SyntheticRun:
    """한 심볼 1분봉에 규칙 집합을 적용해 라운드트립을 만든다. 입력은 바꾸지 않는다."""
    if not _is_pos_real(initial_equity):
        raise ValueError(f"initial_equity 는 > 0: {initial_equity!r}")
    feats = compute_features(bars, windows=(params.n,))  # BARS_1M·단일 심볼·1분 연속 검증
    L = len(bars)
    if L == 0:
        return SyntheticRun(empty_frame(ROUNDTRIPS), 0, False)

    long_sig, short_sig = entry_signals(bars, feats, params)
    ts = bars["ts"].to_numpy(dtype="datetime64[ns]")
    o_ = bars["open"].to_numpy(dtype="float64")
    h_ = bars["high"].to_numpy(dtype="float64")
    l_ = bars["low"].to_numpy(dtype="float64")
    c_ = bars["close"].to_numpy(dtype="float64")
    symbol = str(bars["symbol"].iloc[0])
    nat = np.datetime64("NaT", "ns")

    rows = []
    equity = float(initial_equity)
    skipped = 0
    halted = False
    pos = None  # dict: side, e, entry, qty, stop, tp, equity_before, capped, signal_j, time_exit

    for j in range(L):
        can_signal = True
        if pos is not None:
            exit_ = None  # (사유, 가격, exit_signal_ts)
            if pos["time_exit"]:
                exit_ = ("time", o_[j], ts[j - 1])
            else:
                hit = _intrabar_exit(pos["side"], o_[j], h_[j], l_[j], pos["stop"], pos["tp"],
                                     gap_ok=j > pos["e"])
                if hit is not None:
                    exit_ = (hit[0], hit[1], nat)
                elif j == L - 1:
                    exit_ = ("end_of_data", c_[j], ts[j])
                    can_signal = False
                elif j == pos["e"] + params.max_hold - 1:
                    pos["time_exit"] = True
                    can_signal = False
            if exit_ is not None:
                reason, price, exit_sig_ts = exit_
                pnl = inverse_pnl(pos["side"], pos["qty"], pos["entry"], price)
                eb = pos["equity_before"]
                rows.append({
                    "side": pos["side"],
                    "signal_ts": ts[pos["signal_j"]],
                    "entry_ts": ts[pos["e"]],
                    "entry_price": pos["entry"],
                    "exit_signal_ts": exit_sig_ts,
                    "exit_ts": ts[j],
                    "exit_price": float(price),
                    "qty": pos["qty"],
                    "stop_price": pos["stop"],
                    "tp_price": pos["tp"],
                    "exit_reason": reason,
                    "equity_before": eb,
                    "leverage": (pos["qty"] / pos["entry"]) / eb,
                    "size_capped": pos["capped"],
                    "gross_pnl_xbt": pnl,
                    "gross_ret": pnl / eb,
                })
                equity = eb + pnl
                pos = None
                if equity <= 0:
                    halted = True
                    break
            else:
                continue  # 보유 중(또는 X3 대기) — 신호 무시(P2·P3)

        if not can_signal or j + 1 >= L:
            continue
        if long_sig[j]:
            side = "long"
        elif short_sig[j]:
            side = "short"
        else:
            continue
        entry = float(o_[j + 1])
        qty, capped = position_size(equity, entry, side, params.stop_pct, params.risk_pct)
        if qty < 1:
            skipped += 1
            continue
        stop, tp = _exit_levels(side, entry, params)
        pos = {"side": side, "e": j + 1, "entry": entry, "qty": qty, "stop": stop, "tp": tp,
               "equity_before": equity, "capped": capped, "signal_j": j, "time_exit": False}

    return SyntheticRun(_to_frame(rows, params, symbol), skipped, halted)


def _to_frame(rows: list[dict], params: Params, symbol: str) -> pd.DataFrame:
    if not rows:
        return empty_frame(ROUNDTRIPS)
    df = pd.DataFrame(rows)
    n = len(df)
    df["strategy_id"] = params.strategy_id
    df["param_id"] = params.param_id
    df["trade_id"] = np.arange(n, dtype="int64")
    df["symbol"] = symbol
    df["entry_reason"] = params.entry_reason
    df["risk_pct"] = float(params.risk_pct)
    df["notional_usd"] = df["qty"].astype("float64")
    for col in ("signal_ts", "entry_ts", "exit_signal_ts", "exit_ts"):
        df[col] = pd.to_datetime(df[col].to_numpy(dtype="datetime64[ns]")).tz_localize("UTC")
    df["holding_min"] = (df["exit_ts"] - df["entry_ts"]) / ONE_MIN
    df = df[ROUNDTRIPS.column_names].astype(ROUNDTRIPS.dtypes)
    return validate_roundtrips(df)


def roundtrips_to_fills(rt: pd.DataFrame) -> pd.DataFrame:
    """라운드트립 1행 → synthetic fills 2행(진입·청산). `ts`·`source_id` 순 stable 정렬."""
    validate_roundtrips(rt, strict=False)
    if len(rt) == 0:
        return empty_frame(FILLS)
    is_long = rt["side"] == "long"
    base = (rt["strategy_id"] + ":" + rt["param_id"] + ":"
            + rt["trade_id"].astype("string"))

    def leg(ts, side, price, tag):
        return pd.DataFrame({
            "ts": ts.to_numpy(),
            "symbol": rt["symbol"].to_numpy(),
            "side": side,
            "qty": rt["qty"].to_numpy(),
            "price": price.to_numpy(),
            "leverage": rt["leverage"].to_numpy(),
            "fee": np.nan,
            "fee_currency": pd.NA,
            "source": "synthetic",
            "source_id": (base + f":{tag}").to_numpy(),
            "strategy_id": rt["strategy_id"].to_numpy(),
        })

    entry = leg(rt["entry_ts"], np.where(is_long, "buy", "sell"), rt["entry_price"], "entry")
    exit_ = leg(rt["exit_ts"], np.where(is_long, "sell", "buy"), rt["exit_price"], "exit")
    out = pd.concat([entry, exit_], ignore_index=True)
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out = out.astype(FILLS.dtypes)
    out = out.sort_values(["ts", "source_id"], kind="stable").reset_index(drop=True)
    return validate_fills(out)
