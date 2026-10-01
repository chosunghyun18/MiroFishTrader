"""정규화·분석 테이블 스키마(trades, bars_1m, fills, roundtrips)와 DataFrame 검증.

파서(ingest)·리샘플·분석 모듈은 컬럼·dtype 정의를 여기서만 가져온다.
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase1-ingest-schema.md`
(roundtrips 는 `design/phase2-synthetic-strategy.md` "라운드트립 스키마").
문서와 이 파일이 다르면 문서를 따르고, 문서를 먼저 고친 뒤 이 파일을 맞춘다.

    from src.shared.schema import TRADES, validate_trades, empty_frame
    df = validate_trades(df)          # 위반 시 SchemaError, 통과 시 같은 객체 반환
    empty = empty_frame(TRADES)       # 올바른 dtype 의 0행 프레임

검증 항목(첫 위반에서 중단): 누락 컬럼 → 예상 밖 컬럼 → dtype(시각은 datetime64[ns, UTC])
→ non-nullable 결측 → 허용값 → 중복 키.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

UTC_NS = pd.DatetimeTZDtype("ns", "UTC")
STRING = "string"
INT64 = "int64"
FLOAT64 = "float64"
BOOL = "bool"

SIDES = frozenset({"buy", "sell"})
SOURCES = frozenset({"aoa", "synthetic"})
RT_SIDES = frozenset({"long", "short"})
ENTRY_REASONS = frozenset({"h1_breakout", "h2_momentum", "h3_meanrev"})
EXIT_REASONS = frozenset({"stop", "take_profit", "time", "end_of_data"})


class SchemaError(ValueError):
    """정규화 스키마 위반."""


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    dtype: object  # UTC_NS | "string" | "int64" | "float64" | "bool"
    nullable: bool = False
    allowed: frozenset[str] | None = None


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[ColumnSpec, ...]
    key: tuple[str, ...]
    # (컬럼, 값) 에 해당하는 행은 키 판정에서 뺀다. 나머지 행의 키 컬럼은 결측이면 안 된다.
    key_exempt: tuple[tuple[str, str], ...] = ()

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def dtypes(self) -> dict[str, object]:
        return {c.name: c.dtype for c in self.columns}


TRADES = TableSchema(
    name="trades",
    columns=(
        ColumnSpec("ts", UTC_NS),
        ColumnSpec("symbol", STRING),
        ColumnSpec("side", STRING, allowed=SIDES),
        ColumnSpec("size", INT64),
        ColumnSpec("price", FLOAT64),
        ColumnSpec("tick_direction", STRING),
        ColumnSpec("trd_match_id", STRING),
        ColumnSpec("gross_value", INT64),
        ColumnSpec("home_notional", FLOAT64),
        ColumnSpec("foreign_notional", FLOAT64),
        ColumnSpec("trd_type", STRING, nullable=True),
        ColumnSpec("pool", STRING, nullable=True),
    ),
    key=("trd_match_id",),
)

BARS_1M = TableSchema(
    name="bars_1m",
    columns=(
        ColumnSpec("ts", UTC_NS),
        ColumnSpec("symbol", STRING),
        ColumnSpec("open", FLOAT64),
        ColumnSpec("high", FLOAT64),
        ColumnSpec("low", FLOAT64),
        ColumnSpec("close", FLOAT64),
        ColumnSpec("volume", INT64),
        ColumnSpec("volume_xbt", FLOAT64),
        ColumnSpec("trade_count", INT64),
        ColumnSpec("buy_volume", INT64),
        ColumnSpec("sell_volume", INT64),
    ),
    key=("symbol", "ts"),
)

# 잠정 스키마 — aoa 원본 매핑 확인(T-20261002-07) 후 갱신.
FILLS = TableSchema(
    name="fills",
    columns=(
        ColumnSpec("ts", UTC_NS),
        ColumnSpec("symbol", STRING),
        ColumnSpec("side", STRING, allowed=SIDES),
        ColumnSpec("qty", INT64),
        ColumnSpec("price", FLOAT64),
        ColumnSpec("leverage", FLOAT64, nullable=True),
        ColumnSpec("fee", FLOAT64, nullable=True),
        ColumnSpec("fee_currency", STRING, nullable=True),
        ColumnSpec("source", STRING, allowed=SOURCES),
        ColumnSpec("source_id", STRING, nullable=True),
        ColumnSpec("strategy_id", STRING, nullable=True),
    ),
    key=("source", "source_id"),
    key_exempt=(("source", "aoa"),),
)

# 라운드트립(진입 1회 + 청산 1회 = 1행). 설계 근거: phase2-synthetic-strategy.md "라운드트립 스키마".
# entry_reason 허용값은 aoa 확장 시 추가한다.
ROUNDTRIPS = TableSchema(
    name="roundtrips",
    columns=(
        ColumnSpec("strategy_id", STRING),
        ColumnSpec("param_id", STRING),
        ColumnSpec("trade_id", INT64),
        ColumnSpec("symbol", STRING),
        ColumnSpec("side", STRING, allowed=RT_SIDES),
        ColumnSpec("signal_ts", UTC_NS),
        ColumnSpec("entry_ts", UTC_NS),
        ColumnSpec("entry_price", FLOAT64),
        ColumnSpec("exit_signal_ts", UTC_NS, nullable=True),
        ColumnSpec("exit_ts", UTC_NS),
        ColumnSpec("exit_price", FLOAT64),
        ColumnSpec("qty", INT64),
        ColumnSpec("stop_price", FLOAT64),
        ColumnSpec("tp_price", FLOAT64, nullable=True),
        ColumnSpec("entry_reason", STRING, allowed=ENTRY_REASONS),
        ColumnSpec("exit_reason", STRING, allowed=EXIT_REASONS),
        ColumnSpec("holding_min", FLOAT64),
        ColumnSpec("equity_before", FLOAT64),
        ColumnSpec("notional_usd", FLOAT64),
        ColumnSpec("leverage", FLOAT64),
        ColumnSpec("risk_pct", FLOAT64),
        ColumnSpec("size_capped", BOOL),
        ColumnSpec("gross_pnl_xbt", FLOAT64),
        ColumnSpec("gross_ret", FLOAT64),
    ),
    key=("strategy_id", "param_id", "trade_id"),
)


def _dtype_problem(actual, expected) -> str | None:
    """dtype 이 맞으면 None, 아니면 위반 설명."""
    if expected is UTC_NS or isinstance(expected, pd.DatetimeTZDtype):
        if isinstance(actual, pd.DatetimeTZDtype):
            if str(actual.tz) != "UTC":
                return f"UTC 아님(tz={actual.tz})"
            if actual.unit != "ns":
                return f"시각 단위가 ns 아님({actual})"
            return None
        if isinstance(actual, np.dtype) and actual.kind == "M":
            return f"UTC 아님(tz-naive {actual})"
        return f"시각 dtype 아님({actual})"
    if expected == STRING:
        return None if isinstance(actual, pd.StringDtype) else f"dtype {actual}, string 필요"
    if isinstance(actual, np.dtype) and actual == np.dtype(expected):
        return None
    if isinstance(actual, pd.api.extensions.ExtensionDtype) and str(actual).lower() == expected:
        return f"nullable 확장 dtype {actual}, numpy {expected} 필요"
    return f"dtype {actual}, {expected} 필요"


def validate(df: pd.DataFrame, schema: TableSchema, strict: bool = True) -> pd.DataFrame:
    """df 가 schema 를 따르는지 검사한다. 위반 시 SchemaError, 통과 시 df 를 그대로 돌려준다."""
    t = schema.name
    names = schema.column_names

    missing = [c for c in names if c not in df.columns]
    if missing:
        raise SchemaError(f"[{t}] 누락 컬럼: {missing}")
    if strict:
        extra = [c for c in df.columns if c not in names]
        if extra:
            raise SchemaError(f"[{t}] 예상 밖 컬럼: {extra}")

    for col in schema.columns:
        problem = _dtype_problem(df[col.name].dtype, col.dtype)
        if problem:
            raise SchemaError(f"[{t}] 컬럼 {col.name!r} dtype 위반: {problem}")

    for col in schema.columns:
        if not col.nullable:
            n = int(df[col.name].isna().sum())
            if n:
                raise SchemaError(f"[{t}] 컬럼 {col.name!r} 결측 {n}건 (null 불허)")

    for col in schema.columns:
        if col.allowed is not None:
            s = df[col.name].dropna()
            bad = sorted(set(s[~s.isin(col.allowed)].astype(str)))
            if bad:
                raise SchemaError(
                    f"[{t}] 컬럼 {col.name!r} 허용값 위반: {bad} (허용 {sorted(col.allowed)})")

    keyed = df
    for c, v in schema.key_exempt:
        keyed = keyed[keyed[c] != v]
    key = list(schema.key)
    null_key = keyed[key].isna().any(axis=1)
    if null_key.any():
        raise SchemaError(f"[{t}] 키 {key} 결측 {int(null_key.sum())}건")
    dup = keyed.duplicated(subset=key, keep=False)
    if dup.any():
        sample = keyed.loc[dup, key].head(3).to_dict("records")
        raise SchemaError(f"[{t}] 중복 키 {key} {int(dup.sum())}행: {sample}")

    return df


def validate_trades(df: pd.DataFrame) -> pd.DataFrame:
    return validate(df, TRADES)


def validate_bars_1m(df: pd.DataFrame) -> pd.DataFrame:
    return validate(df, BARS_1M)


def validate_fills(df: pd.DataFrame) -> pd.DataFrame:
    return validate(df, FILLS)


def validate_roundtrips(df: pd.DataFrame, strict: bool = True) -> pd.DataFrame:
    return validate(df, ROUNDTRIPS, strict=strict)


def empty_frame(schema: TableSchema) -> pd.DataFrame:
    """schema 의 컬럼 순서·dtype 을 가진 0행 DataFrame."""
    return pd.DataFrame({c.name: pd.Series([], dtype=c.dtype) for c in schema.columns})
