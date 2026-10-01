"""BitMEX 공식 REST `/api/v1/funding` → 정규화 `funding` 테이블 수집기·로더.

출력: `src.shared.schema.FUNDING`(ts·symbol·funding_rate)를 따르는 심볼별 parquet
`data/raw/normalized/bitmex/funding/<SYMBOL>.parquet`(`.gitignore` 의 `data/raw/*` 대상, 약관 18.6(a)).
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase3-backtest.md` "펀딩 모델" 1·5·6·7항.

    python -m src.ingest.bitmex_funding --symbol XBTUSD --start 2018-03-01 --end 2025-01-01 [--out DIR]

    from src.ingest.bitmex_funding import load_funding, check_funding_gaps
    df = load_funding("XBTUSD", "2021-01-01", "2021-02-01")     # [start, end) UTC
    gaps = check_funding_gaps(df, "2021-01-01", "2021-02-01")    # 빈 목록 = 정상

- 구간은 모두 반열린 `[start, end)` UTC 다(CLI `--end` 날짜 미포함).
- 요청: `count=500`·`reverse=false`, 직전 페이지 마지막 `timestamp` 부터 이어받고 그 이하 행은 버린다.
  요청 간 ≥ 1 s, 429 는 `Retry-After`(정수 초, 없으면 60 s) 대기 후 재시도 최대 3회.
- 쓰기: 기존 파일과 키 (`symbol`, `ts`) 병합(충돌 시 새 값 우선) → `*.tmp` → 원자적 rename.
- CLI 종료코드: 0 정상, 1 수집 실패(파일 미변경), 2 인자 오류, 3 저장은 했으나 간격 위반.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
import pandas as pd
import requests

from src.ingest.normalize import DEFAULT_OUT_DIR, write_parquet_atomic
from src.shared.schema import FUNDING, STRING, UTC_NS, SchemaError, empty_frame, validate_funding

log = logging.getLogger(__name__)

API_URL = "https://www.bitmex.com/api/v1/funding"
PAGE_COUNT = 500
MIN_INTERVAL_S = 1.0
RETRY_AFTER_DEFAULT_S = 60
MAX_RETRIES = 3
REQUEST_TIMEOUT_S = 60
DEFAULT_FUNDING_DIR = DEFAULT_OUT_DIR / "funding"
GRID_HOURS = (4, 12, 20)
GAP_KINDS = frozenset({"missing", "off_grid"})


class FundingFetchError(RuntimeError):
    """펀딩 API 수집 실패(429 재시도 초과·응답 형식 오류)."""


@dataclass(frozen=True)
class FundingGap:
    ts: pd.Timestamp
    kind: str  # "missing"(격자 시각에 유효 행 없음) | "off_grid"(격자 밖 행 = 8시간 간격 위반)


def funding_path(data_dir: Path | str, symbol: str) -> Path:
    return Path(data_dir) / f"{symbol}.parquet"


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(t: pd.Timestamp) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _retry_after(resp) -> float:
    try:
        return float(int(resp.headers.get("Retry-After")))
    except (TypeError, ValueError):
        return float(RETRY_AFTER_DEFAULT_S)


def _get_page(session, params: dict, sleep: Callable[[float], None]) -> list:
    """한 페이지 요청. 429 는 Retry-After 만큼 기다려 최대 MAX_RETRIES 회 재시도."""
    for attempt in range(MAX_RETRIES + 1):
        resp = session.get(API_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        if resp.status_code == 429:
            if attempt == MAX_RETRIES:
                raise FundingFetchError(f"HTTP 429 재시도 {MAX_RETRIES}회 초과: {params}")
            wait = _retry_after(resp)
            log.warning("HTTP 429 — %.0f s 대기 후 재시도(%d/%d)", wait, attempt + 1, MAX_RETRIES)
            sleep(wait)
            continue
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, list):
            raise FundingFetchError(f"응답이 목록이 아님: {type(body).__name__}")
        return body
    raise AssertionError("unreachable")


def _normalize(records: list, symbol: str) -> pd.DataFrame:
    """API 레코드 → FUNDING 3열(ts 정렬·키 중복 제거). 검증은 호출자가 한다."""
    if not records:
        return empty_frame(FUNDING)
    try:
        raw = pd.DataFrame.from_records(records)[["timestamp", "symbol", "fundingRate"]]
    except KeyError as e:
        raise FundingFetchError(f"응답 필드 누락: {e}") from e
    other = sorted(set(raw["symbol"].astype(str)) - {symbol})
    if other:
        raise FundingFetchError(f"요청 심볼 {symbol} 과 다른 행: {other}")
    df = pd.DataFrame({
        "ts": pd.to_datetime(raw["timestamp"], utc=True, format="ISO8601").astype(UTC_NS),
        "symbol": raw["symbol"].astype(STRING),
        "funding_rate": pd.to_numeric(raw["fundingRate"], errors="coerce").astype("float64"),
    })
    df = df.drop_duplicates(subset=list(FUNDING.key), keep="last")
    return df.sort_values("ts", kind="stable").reset_index(drop=True)


def fetch_funding(symbol: str, start, end, *, session=None,
                  sleep: Callable[[float], None] = time.sleep,
                  min_interval: float = MIN_INTERVAL_S) -> pd.DataFrame:
    """`[start, end)` 펀딩 이력을 페이지네이션으로 받아 검증된 FUNDING 프레임으로 돌려준다."""
    start, end = _utc(start), _utc(end)
    if start >= end:
        raise ValueError(f"start 가 end 보다 같거나 늦음: {start} >= {end}")
    s = session or requests.Session()
    params = {"symbol": symbol, "count": PAGE_COUNT, "startTime": _iso(start),
              "endTime": _iso(end), "reverse": "false"}
    rows: list = []
    last_ts: pd.Timestamp | None = None
    n_req = 0
    while True:
        if n_req:
            sleep(min_interval)
        page = _get_page(s, params, sleep)
        n_req += 1
        new = [r for r in page if last_ts is None or _utc(r["timestamp"]) > last_ts]
        rows.extend(new)
        if len(page) < PAGE_COUNT or not new:
            break
        last_ts = _utc(new[-1]["timestamp"])
        params = {**params, "startTime": new[-1]["timestamp"]}
    log.info("%s 펀딩 %d건 수신(요청 %d회)", symbol, len(rows), n_req)
    df = _normalize(rows, symbol)
    df = df[(df["ts"] >= start) & (df["ts"] < end)].reset_index(drop=True)
    return validate_funding(df)


def merge_funding(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """키 (`symbol`, `ts`) 병합. 충돌은 new 우선, ts 정렬."""
    df = pd.concat([old, new], ignore_index=True)
    df = df.drop_duplicates(subset=list(FUNDING.key), keep="last")
    df = df.sort_values(["ts", "symbol"], kind="stable").reset_index(drop=True)
    return validate_funding(df)


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path, engine="pyarrow")
    df = df.astype({c.name: c.dtype for c in FUNDING.columns})
    return validate_funding(df)


def write_funding(df: pd.DataFrame, symbol: str, data_dir: Path | str = DEFAULT_FUNDING_DIR) -> Path:
    """기존 파일과 병합해 `<data_dir>/<symbol>.parquet` 에 원자적으로 쓴다."""
    validate_funding(df)
    path = funding_path(data_dir, symbol)
    merged = merge_funding(_read(path), df) if path.exists() else merge_funding(empty_frame(FUNDING), df)
    write_parquet_atomic(merged, path)
    return path


def _grid(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    day0 = start.floor("D")
    times = pd.DatetimeIndex([], dtype=UTC_NS)
    for h in GRID_HOURS:
        times = times.append(pd.date_range(day0 + pd.Timedelta(hours=h), end, freq="24h"))
    times = times[(times >= start) & (times < end)]
    return times.sort_values()


def check_funding_gaps(df: pd.DataFrame, start, end) -> list[FundingGap]:
    """`[start, end)` 의 기대 격자(매일 04·12·20 UTC) 대비 결측·격자 밖 행 목록(ts 오름차순).

    입력을 validate 하지 않는다 — `funding_rate` NaN 행은 그 시각의 `missing` 으로 본다.
    """
    start, end = _utc(start), _utc(end)
    ts = pd.DatetimeIndex(df["ts"])
    inside = (ts >= start) & (ts < end)
    ts, rate = ts[inside], df["funding_rate"].to_numpy()[inside]
    grid = _grid(start, end)
    on_grid = ts.isin(grid)
    gaps = [FundingGap(t, "off_grid") for t in ts[~on_grid]]
    present = set(ts[on_grid & ~np.isnan(rate.astype("float64"))])
    gaps += [FundingGap(t, "missing") for t in grid if t not in present]
    gaps.sort(key=lambda g: (g.ts, g.kind))
    return gaps


def load_funding(symbol: str, start, end, data_dir: Path | str = DEFAULT_FUNDING_DIR) -> pd.DataFrame:
    """`[start, end)` 정산 행(ts 정렬, FUNDING 검증). 파일이 없으면 FileNotFoundError.

    결측 판정은 하지 않는다 — 호출자가 `check_funding_gaps` 를 쓴다.
    """
    start, end = _utc(start), _utc(end)
    if start > end:
        raise ValueError(f"start 가 end 보다 늦음: {start} > {end}")
    path = funding_path(data_dir, symbol)
    if not path.exists():
        raise FileNotFoundError(f"펀딩 파일 없음: {path}")
    df = _read(path)
    df = df[(df["symbol"] == symbol) & (df["ts"] >= start) & (df["ts"] < end)]
    return validate_funding(df.sort_values("ts", kind="stable").reset_index(drop=True))


def _new_session() -> requests.Session:
    return requests.Session()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _parse_date(s: str) -> pd.Timestamp:
    return _utc(datetime.strptime(s, "%Y-%m-%d"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bitmex_funding", description=__doc__.split("\n")[0])
    p.add_argument("--symbol", default="XBTUSD")
    p.add_argument("--start", type=_parse_date, required=True, help="YYYY-MM-DD (포함, UTC)")
    p.add_argument("--end", type=_parse_date, required=True, help="YYYY-MM-DD (미포함, UTC)")
    p.add_argument("--out", type=Path, default=DEFAULT_FUNDING_DIR)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    a = build_parser().parse_args(argv)
    if a.start >= a.end:
        log.error("--start(%s) 가 --end(%s) 보다 같거나 늦음", a.start.date(), a.end.date())
        return 2
    try:
        df = fetch_funding(a.symbol, a.start, a.end, session=_new_session(), sleep=_sleep)
    except (FundingFetchError, requests.RequestException, SchemaError) as e:
        log.error("펀딩 수집 실패(파일 미변경): %s", e)
        return 1
    path = write_funding(df, a.symbol, a.out)
    gaps = check_funding_gaps(load_funding(a.symbol, a.start, a.end, a.out), a.start, a.end)
    log.info("저장: %s (구간 %d건)", path, len(df))
    if gaps:
        for g in gaps[:20]:
            log.warning("간격 위반 %s %s", g.kind, g.ts.isoformat())
        log.warning("간격 위반 총 %d건", len(gaps))
        return 3
    log.info("간격 검사 정상")
    return 0


if __name__ == "__main__":
    sys.exit(main())
