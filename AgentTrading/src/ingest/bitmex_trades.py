"""BitMEX 공개 trade 원본(일별 csv.gz) → 정규화 `trades` 테이블 파서.

입력: 다운로더(`bitmex_public`)가 저장한 `data/raw/bitmex/trade/[XBTUSD/]YYYYMMDD.csv.gz`.
출력: `src.shared.schema.TRADES` 를 따르는 DataFrame(컬럼·dtype 은 schema 모듈에서만 가져온다).
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase1-ingest-schema.md`

    from src.ingest.bitmex_trades import read_trades, iter_trades
    df = read_trades("data/raw/bitmex/trade/XBTUSD/20200312.csv.gz")   # 기본 symbols=("XBTUSD",)
    for chunk in iter_trades(path, symbols=None, chunksize=200_000):   # 전 종목, 청크 단위
        ...

- 원본 헤더는 구버전 10열과 `trdType,pool` 이 붙은 신버전 12열을 둘 다 받는다(구버전은 두 열을 <NA>).
  필수 열 누락·알 수 없는 열·값 변환 실패는 조용히 넘기지 않고 ValueError.
- `iter_trades` 는 청크 내부만 중복 제거(`trd_match_id` 첫 행)·`ts` stable sort 한다.
  파일 전체에 대한 정렬·중복 제거 보장은 `read_trades` 가 맡는다. 파일 간 중복은 T-20261002-05 범위.
"""

from __future__ import annotations

import os
from typing import Iterable, Iterator

import pandas as pd

from src.shared.schema import INT64, FLOAT64, STRING, TRADES, UTC_NS, empty_frame, validate_trades

RAW_TO_NORMALIZED = {
    "timestamp": "ts",
    "symbol": "symbol",
    "side": "side",
    "size": "size",
    "price": "price",
    "tickDirection": "tick_direction",
    "trdMatchID": "trd_match_id",
    "grossValue": "gross_value",
    "homeNotional": "home_notional",
    "foreignNotional": "foreign_notional",
    "trdType": "trd_type",
    "pool": "pool",
}
OLD_COLUMNS = tuple(list(RAW_TO_NORMALIZED)[:10])
NEW_COLUMNS = tuple(RAW_TO_NORMALIZED)
OPTIONAL_COLUMNS = frozenset(NEW_COLUMNS) - frozenset(OLD_COLUMNS)

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
DEFAULT_SYMBOLS = ("XBTUSD",)
DEFAULT_CHUNKSIZE = 500_000
KEY = list(TRADES.key)


def parse_timestamp(series: pd.Series) -> pd.Series:
    """'YYYY-MM-DDDhh:mm:ss.fffffffff' → datetime64[ns, UTC]. 형식이 다르면 ValueError."""
    s = series.astype(STRING).str.replace("D", "T", n=1, regex=False)
    try:
        ts = pd.to_datetime(s, format=TIMESTAMP_FORMAT, utc=True)
    except (ValueError, TypeError) as e:
        raise ValueError(f"timestamp 파싱 실패: {e}") from e
    return ts.astype(UTC_NS)


def _check_columns(columns: Iterable[str]) -> None:
    cols = list(columns)
    unknown = [c for c in cols if c not in RAW_TO_NORMALIZED]
    if unknown:
        raise ValueError(f"알 수 없는 원본 열: {unknown}")
    missing = [c for c in OLD_COLUMNS if c not in cols]
    if missing:
        raise ValueError(f"필수 원본 열 누락: {missing}")
    if len(set(cols)) != len(cols):
        raise ValueError(f"원본 열 이름 중복: {cols}")


def _convert(s: pd.Series, dtype: object, name: str) -> pd.Series:
    try:
        if dtype is UTC_NS:
            return parse_timestamp(s)
        if dtype == STRING:
            return s.astype(STRING)
        if dtype in (INT64, FLOAT64):
            if s.isna().any():
                raise ValueError(f"결측 {int(s.isna().sum())}건")
            return s.astype(dtype)
    except (ValueError, TypeError) as e:
        raise ValueError(f"열 {name!r} 를 {dtype} 로 변환 실패: {e}") from e
    raise AssertionError(f"처리하지 않은 dtype {dtype}")


def normalize_chunk(raw: pd.DataFrame) -> pd.DataFrame:
    """원본 열(문자열로 읽은) 청크 → TRADES 컬럼 순서·dtype 프레임. 검증·정렬은 하지 않는다."""
    _check_columns(raw.columns)
    df = raw.rename(columns=RAW_TO_NORMALIZED)
    out = {}
    for col in TRADES.columns:
        if col.name in df.columns:
            out[col.name] = _convert(df[col.name], col.dtype, col.name)
        else:  # 구버전 파일의 trd_type / pool
            out[col.name] = pd.Series(pd.NA, index=df.index, dtype=col.dtype)
    norm = pd.DataFrame(out, index=df.index)
    norm["side"] = norm["side"].str.lower()
    return norm


def _finish(df: pd.DataFrame) -> pd.DataFrame:
    """키 중복은 원본 순서 기준 첫 행만 남기고 `ts` stable sort 후 검증."""
    df = df.drop_duplicates(subset=KEY, keep="first")
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    return validate_trades(df)


def iter_trades(
    path: str | os.PathLike,
    symbols: Iterable[str] | None = DEFAULT_SYMBOLS,
    chunksize: int = DEFAULT_CHUNKSIZE,
) -> Iterator[pd.DataFrame]:
    """원본 파일을 청크로 읽어 정규화·검증된 청크를 낸다. 필터 후 0행인 청크는 건너뛴다.

    symbols=None 이면 종목 필터 없음. 정렬·중복 제거는 청크 내부에서만 한다.
    """
    wanted = None if symbols is None else set(symbols)
    reader = pd.read_csv(
        path,
        compression="gzip",
        dtype=str,
        keep_default_na=False,
        na_values=[""],
        chunksize=chunksize,
    )
    with reader:
        for raw in reader:
            _check_columns(raw.columns)
            if wanted is not None:
                raw = raw[raw["symbol"].isin(wanted)]
            if raw.empty:
                continue
            yield _finish(normalize_chunk(raw))


def read_trades(
    path: str | os.PathLike,
    symbols: Iterable[str] | None = DEFAULT_SYMBOLS,
    chunksize: int = DEFAULT_CHUNKSIZE,
) -> pd.DataFrame:
    """원본 파일 하나를 정규화된 한 프레임으로. 파일 전체 기준 `ts` stable sort·`trd_match_id` 중복 제거.

    헤더만 있는 파일이나 필터 후 0행이면 `empty_frame(TRADES)` 를 돌려준다.
    """
    chunks = list(iter_trades(path, symbols=symbols, chunksize=chunksize))
    if not chunks:
        return validate_trades(empty_frame(TRADES))
    return _finish(pd.concat(chunks, ignore_index=True))
