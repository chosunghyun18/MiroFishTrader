"""워크포워드 폴드·구간 가드·파라미터 선택·Deflated Sharpe·OOS 접근 가드.

설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase3-backtest.md`
"워크포워드"(폴드·파라미터 선택·검증 곡선·DSR·OOS 접근 규칙)·"모듈". 문서와 이 파일이 다르면 문서를 따른다.
데이터 로드·그리드 실행·리포트/`selection.json` 파일 쓰기는 `run.py`(T-22) 범위라 여기서는 순수 함수와
OOS 접근 로그(append 전용)만 다룬다.

    from src.backtest.walkforward import make_folds, select_params, deflated_sharpe
    folds = make_folds()                      # 기본 표본 [2018-03-01, 2022-01-01), 6폴드
    best = select_params(train_summaries, gate)  # 학습 구간 요약만 입력

- 구간은 모두 반열린 `[start, end)` UTC 자정. 문자열·date·Timestamp 를 받고 tz 없으면 UTC.
  `src/analysis/run.py` 의 동명 `check_sample_range`(date·종료일 포함)와 의미가 다르다.
- OOS [2022-01-01, 2025-01-01) 는 `authorize_oos` → `evaluate_oos` 경로로만 접근한다. 접근 로그를 지우거나
  초기화하는 함수는 두지 않는다(가드 해제는 사람 판단).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from src.backtest.metrics import _day, _resolve_gate, gate_verdict, summarize_net_run
from src.shared import schema as sc

SAMPLE_START = pd.Timestamp("2018-03-01", tz="UTC")
OOS_START = pd.Timestamp("2022-01-01", tz="UTC")  # = 기본 표본 끝(반열린)
OOS_END = pd.Timestamp("2025-01-01", tz="UTC")    # 이후는 어떤 경로로도 금지(Spec 6절)
N_TRIALS = 756
EULER_GAMMA = 0.5772156649
DEFAULT_MIN_DSR = 0.95
OOS_LOG_PATH = Path("data/backtest/oos_access.jsonl")
WALKFORWARD_STRATEGY_ID = "walkforward"
WALKFORWARD_PARAM_ID = "stitched"
SELECTION_KEYS = ("strategy_id", "param_id", "selected_on", "fee_profile", "gate")

_N = NormalDist()


def _fmt(t: pd.Timestamp) -> str:
    return t.strftime("%Y-%m-%d")


def _range(start, end, lo: pd.Timestamp, hi: pd.Timestamp, what: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    s, e = _day(start, "start"), _day(end, "end")
    if e <= s:
        raise ValueError(f"end 는 start 보다 뒤여야 한다: {s} ~ {e}")
    if e > OOS_END:
        raise ValueError(f"{_fmt(OOS_END)} 이후는 어떤 경로로도 금지(유동성 붕괴 구간): {_fmt(s)} ~ {_fmt(e)}")
    if s < lo or e > hi:
        reason = ""
        if what == "기본 표본" and e > OOS_START:
            reason = " — OOS 구간은 evaluate_oos(selection) 로만 접근한다"
        raise ValueError(f"{what} [{_fmt(lo)}, {_fmt(hi)}) 밖 구간: [{_fmt(s)}, {_fmt(e)}){reason}")
    return s, e


def check_sample_range(start, end) -> tuple[pd.Timestamp, pd.Timestamp]:
    """[start, end) ⊂ 기본 표본 [2018-03-01, 2022-01-01) 확인 → UTC 자정 (start, end). 밖이면 `ValueError`."""
    return _range(start, end, SAMPLE_START, OOS_START, "기본 표본")


def check_oos_range(start, end) -> tuple[pd.Timestamp, pd.Timestamp]:
    """[start, end) ⊂ OOS [2022-01-01, 2025-01-01) 확인. `evaluate_oos` 전용."""
    return _range(start, end, OOS_START, OOS_END, "OOS")


# 폴드 -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Fold:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def make_folds(start="2018-03-01", end="2022-01-01", train_months: int = 12, test_months: int = 6,
               step_months: int = 6) -> list[Fold]:
    """앵커드(확장) 워크포워드 폴드. 학습 시작 고정, 마지막 검증은 `end` 에서 자른다."""
    s, e = check_sample_range(start, end)
    for name, v in (("train_months", train_months), ("test_months", test_months),
                    ("step_months", step_months)):
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError(f"{name} 는 양의 정수여야 한다: {v!r}")
    folds = []
    k = 0
    while True:
        train_end = s + pd.DateOffset(months=train_months + k * step_months)
        if train_end >= e:
            break
        test_end = min(train_end + pd.DateOffset(months=test_months), e)
        folds.append(Fold(s, train_end, train_end, test_end))
        k += 1
    if not folds:
        raise ValueError(f"[{_fmt(s)}, {_fmt(e)}) 에서 검증 구간이 하나도 나오지 않는다")
    return folds


# 파라미터 선택 -----------------------------------------------------------------------------------------

def select_params(train_summaries: Iterable[Mapping], gate: Mapping | None = None) -> dict | None:
    """한 폴드의 학습 구간 run 요약 목록 → 선택 요약(dict 복사) 또는 `None`(선택 없음).

    후보 = `n_trades ≥ min_trades`·`mdd ≤ max_drawdown`·`sharpe` 유한. `sharpe` 최대, 동률은
    (`strategy_id`, `param_id`) 사전순 첫 번째. 입력 순서와 무관하다.
    """
    g = _resolve_gate(gate)
    best = None
    best_key = None
    for s in train_summaries:
        sh, mdd = float(s["sharpe"]), float(s["mdd"])
        if s["n_trades"] < g["min_trades"] or not math.isfinite(sh) or not mdd <= g["max_drawdown"]:
            continue
        key = (-sh, str(s["strategy_id"]), str(s["param_id"]))
        if best_key is None or key < best_key:
            best, best_key = s, key
    return None if best is None else dict(best)


# DSR -------------------------------------------------------------------------------------------------

def expected_max_sr(n_trials: int, var_sr: float) -> float:
    """SR0 = √V × ((1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))). 정의 불가 → NaN."""
    if n_trials < 2 or not isinstance(var_sr, Real) or not math.isfinite(var_sr) or var_sr < 0:
        return float("nan")
    z1 = _N.inv_cdf(1 - 1 / n_trials)
    z2 = _N.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe(sr, n_trials, var_sr, t, skew, kurt) -> float:
    """Bailey & López de Prado (2014) DSR. `sr`·`var_sr` 는 일 단위 비연환산, `kurt` 는 비초과(정규 = 3).

    `t < 2`·`n_trials < 2`·`var_sr` NaN/음수·분모 안 ≤ 0·비유한 입력 → NaN.
    """
    vals = (sr, var_sr, t, skew, kurt, n_trials)
    if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in vals):
        return float("nan")
    if t < 2:
        return float("nan")
    sr0 = expected_max_sr(int(n_trials), float(var_sr))
    if math.isnan(sr0):
        return float("nan")
    den = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if den <= 0:
        return float("nan")
    return _N.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(den))


def sr_variance(summaries: Iterable[Mapping]) -> float:
    """요약 목록의 `sr_daily` 유한값 표본 분산(ddof=1). 2개 미만 → NaN. 대표 run 고르기는 호출자 몫."""
    x = np.array([float(s["sr_daily"]) for s in summaries], dtype=float)
    x = x[np.isfinite(x)]
    return float(np.var(x, ddof=1)) if len(x) >= 2 else float("nan")


# 검증 곡선·판정 -----------------------------------------------------------------------------------------

def stitch_test_roundtrips(fold_rts: Iterable[pd.DataFrame | None]) -> pd.DataFrame:
    """폴드별 검증 net roundtrips(`None`·0행 = 선택 없음)를 한 run 으로 이어 붙인다.

    run 키를 (`walkforward`, `stitched`)로 바꾸고, 폴드 순 → `entry_ts` → 원 `trade_id` 로 정렬해
    `trade_id` 를 0..n−1 로 다시 매긴다(폴드마다 따로 만든 run 의 trade_id 충돌 방지). 입력은 바꾸지 않는다.
    """
    parts = []
    for i, rt in enumerate(fold_rts):
        if rt is None or len(rt) == 0:
            continue
        sc.validate_roundtrips_net(rt, strict=True)
        p = rt.copy()
        p["_fold"] = i
        parts.append(p)
    if not parts:
        return sc.empty_frame(sc.ROUNDTRIPS_NET)
    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values(["_fold", "entry_ts", "trade_id"], kind="mergesort").reset_index(drop=True)
    out = out.drop(columns="_fold")
    out["strategy_id"] = WALKFORWARD_STRATEGY_ID
    out["param_id"] = WALKFORWARD_PARAM_ID
    out["trade_id"] = np.arange(len(out), dtype=np.int64)
    out = out[[c.name for c in sc.ROUNDTRIPS_NET.columns]].astype(sc.ROUNDTRIPS_NET.dtypes)
    return sc.validate_roundtrips_net(out, strict=True)


def _min_dsr(gate: Mapping | None) -> float:
    v = (gate or {}).get("min_dsr", DEFAULT_MIN_DSR)
    if isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v):
        raise ValueError(f"gate 'min_dsr' 는 유한 실수여야 한다: {v!r}")
    return float(v)


def resolve_gate(gate: Mapping | None) -> dict:
    """3기준(`metrics` 기본값 채움) + `min_dsr` 정규화 dict. selection 해시 입력으로 쓴다."""
    g = _resolve_gate(gate)
    g["min_dsr"] = _min_dsr(gate)
    return g


def walkforward_verdict(summary: Mapping, dsr: float, gate: Mapping | None = None) -> str:
    """`gate_verdict` 가 insufficient → insufficient, pass 이고 `dsr ≥ min_dsr`(NaN 미달) → pass, 그 외 fail."""
    v = gate_verdict(summary, gate)
    if v == "insufficient":
        return v
    d = float(dsr)
    return "pass" if v == "pass" and math.isfinite(d) and d >= _min_dsr(gate) else "fail"


# 선택 파일·OOS ---------------------------------------------------------------------------------------

def selection_sha256(sel: Mapping) -> str:
    """`sha256` 을 뺀 선택 필드의 정규 JSON(키 정렬, `,` `:`) 해시."""
    body = {k: sel[k] for k in SELECTION_KEYS}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_selection(summary: Mapping, gate: Mapping | None = None) -> dict:
    """기본 표본 전체 학습 요약에서 고른 run 요약 → 선택 dict(+`sha256`). 구간이 표본 전체가 아니면 `ValueError`."""
    s, e = _day(summary["start"], "start"), _day(summary["end"], "end")
    if (s, e) != (SAMPLE_START, OOS_START):
        raise ValueError(f"선택은 기본 표본 전체 [{_fmt(SAMPLE_START)}, {_fmt(OOS_START)}) 에서 해야 한다: "
                         f"[{_fmt(s)}, {_fmt(e)})")
    if summary.get("strategy_id") is None or summary.get("param_id") is None:
        raise ValueError("선택 요약에 strategy_id·param_id 가 없다")
    sel = {
        "strategy_id": str(summary["strategy_id"]),
        "param_id": str(summary["param_id"]),
        "selected_on": [_fmt(s), _fmt(e)],
        "fee_profile": "default",
        "gate": resolve_gate(gate),
    }
    sel["sha256"] = selection_sha256(sel)
    return sel


def authorize_oos(selection: Mapping, log_path=OOS_LOG_PATH) -> tuple[pd.Timestamp, pd.Timestamp]:
    """선택 파일 검증 + 접근 로그 검사(쓰기 없음) → OOS (start, end).

    필수 키 누락·sha256 불일치·표본 전체가 아닌 선택·`default` 아닌 프로필 → `ValueError`.
    로그에 다른 sha256(또는 읽을 수 없는 줄)이 있으면 `PermissionError`. 같은 sha256 은 허용.
    """
    missing = [k for k in (*SELECTION_KEYS, "sha256") if k not in selection]
    if missing:
        raise ValueError(f"선택 파일에 키가 없다: {missing}")
    if selection_sha256(selection) != selection["sha256"]:
        raise ValueError("선택 파일 sha256 이 내용과 다르다(변조·수정된 선택)")
    if list(selection["selected_on"]) != [_fmt(SAMPLE_START), _fmt(OOS_START)]:
        raise ValueError(f"선택 구간이 기본 표본 전체가 아니다: {selection['selected_on']}")
    if selection["fee_profile"] != "default":
        raise ValueError(f"선택 수수료 프로필은 default 여야 한다: {selection['fee_profile']!r}")
    sha = selection["sha256"]
    path = Path(log_path)
    if path.exists():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                other = json.loads(line)["sha256"]
            except (ValueError, KeyError, TypeError) as e:
                raise PermissionError(f"OOS 접근 로그 {path}:{n} 를 읽을 수 없다 — 사람 확인 필요") from e
            if other != sha:
                raise PermissionError(f"OOS 는 이미 다른 선택(sha256 {other[:12]})으로 조회됐다 — "
                                      "가드 해제는 사람 판단")
    return check_oos_range(OOS_START, OOS_END)


def evaluate_oos(selection: Mapping, oos_net_rt: pd.DataFrame, log_path=OOS_LOG_PATH,
                 gate: Mapping | None = None, now: datetime | None = None) -> dict:
    """유일한 OOS 경로. 선택 run 의 OOS net roundtrips → 요약(3기준 `gate_verdict`) + 접근 로그 한 줄 append.

    `gate` 가 없으면 선택 파일의 `gate` 를 쓴다. 0건 입력은 허용하고 요약의 run 키를 선택값으로 채운다.
    입력 run 이 선택과 다르면 `ValueError`(로그 미기록).
    """
    start, end = authorize_oos(selection, log_path)
    runs = oos_net_rt[["strategy_id", "param_id"]].drop_duplicates()
    want = (selection["strategy_id"], selection["param_id"])
    if len(runs) and [tuple(r) for r in runs.itertuples(index=False)] != [want]:
        raise ValueError(f"OOS 입력 run 이 선택 {want} 와 다르다")
    g = selection["gate"] if gate is None else gate
    summary = summarize_net_run(oos_net_rt, start, end, g)
    summary["strategy_id"], summary["param_id"] = want
    ts = (now or datetime.now(timezone.utc)).isoformat()
    rec = {"ts": ts, "sha256": selection["sha256"], "strategy_id": want[0], "param_id": want[1],
           "gate": summary["gate"]}
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return summary
