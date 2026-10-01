"""BitMEX 원본 일 파일 → 정규화 `trades`·`bars_1m` 일별 parquet (재실행 가능·증분 처리·중복 제거).

입력: 다운로더(`bitmex_public`)가 저장한 `data/raw/bitmex/trade/[XBTUSD/]YYYYMMDD.csv.gz`.
출력: `data/raw/normalized/bitmex/{trades,bars_1m}/<SYMBOL>/YYYYMMDD.parquet`,
      manifest `data/raw/normalized/bitmex/_manifest/<SYMBOL>.json`.
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase1-ingest-schema.md` "저장 형식"

    python -m src.ingest.normalize --start 2020-03-12 --end 2020-03-14 --symbol XBTUSD

- 원본은 `<raw-dir>/<SYMBOL>/YYYYMMDD.csv.gz` 를 먼저, 없으면 전 종목 `<raw-dir>/YYYYMMDD.csv.gz` 를 쓴다.
  둘 다 없으면 WARNING 만 남기고 그날은 건너뛴다(출력·manifest 변경 없음).
- 쓰기는 `*.tmp` → `os.replace`(원자적). manifest 항목은 그날 출력 2개가 모두 교체된 뒤에만 갱신한다.
- 건너뛰기 = 원본 크기·mtime_ns 일치 ∧ `version` 일치 ∧ 출력 2개 존재 ∧ 기록된 `prev_close` 가 이번 기대값과 같음.
- `prev_close` 연쇄: 전날 원본이 있고 전날 항목이 유효할 때만 그 `last_close`, 아니면 None(구간이 끊기면 넘기지 않음).
- 처리 오류는 fail-fast(종료코드 1). 그 전까지 끝난 날의 출력·manifest 는 유지된다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

import pandas as pd

from src.ingest.bars import last_close, resample_1m
from src.ingest.bitmex_trades import read_trades

log = logging.getLogger(__name__)

DEFAULT_RAW_DIR = Path("data/raw/bitmex/trade")
DEFAULT_OUT_DIR = Path("data/raw/normalized/bitmex")
NORMALIZER_VERSION = 1


@dataclass
class RunSummary:
    processed: list[date] = field(default_factory=list)
    skipped: list[date] = field(default_factory=list)
    missing: list[date] = field(default_factory=list)


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def find_raw(raw_dir: Path, symbol: str, day: date) -> Path | None:
    """심볼 디렉터리 파일 우선, 없으면 전 종목 파일, 둘 다 없으면 None."""
    for p in (raw_dir / symbol / f"{_ymd(day)}.csv.gz", raw_dir / f"{_ymd(day)}.csv.gz"):
        if p.is_file():
            return p
    return None


def trades_path(out_dir: Path, symbol: str, day: date) -> Path:
    return out_dir / "trades" / symbol / f"{_ymd(day)}.parquet"


def bars_path(out_dir: Path, symbol: str, day: date) -> Path:
    return out_dir / "bars_1m" / symbol / f"{_ymd(day)}.parquet"


def manifest_path(out_dir: Path, symbol: str) -> Path:
    return out_dir / "_manifest" / f"{symbol}.json"


def fingerprint(path: Path) -> dict:
    st = path.stat()
    return {"raw_size": st.st_size, "raw_mtime_ns": st.st_mtime_ns}


def _replace_atomic(write, path: Path) -> None:
    """`write(tmp)` 로 `<path>.tmp` 를 만든 뒤 원자적으로 교체. 실패하면 tmp 를 지운다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    _replace_atomic(lambda tmp: df.to_parquet(tmp, engine="pyarrow", compression="snappy",
                                              index=False), path)


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(manifest: dict, path: Path) -> None:
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _replace_atomic(lambda tmp: tmp.write_text(text, encoding="utf-8"), path)


def _entry_current(entry: dict | None, raw: Path, out_dir: Path, symbol: str, day: date) -> bool:
    """`prev_close` 를 제외한 비재귀 유효 조건: 원본 지문·version 일치 ∧ 출력 2개 존재."""
    return (entry is not None
            and entry.get("raw_path") == str(raw)
            and all(entry.get(k) == v for k, v in fingerprint(raw).items())
            and entry.get("version") == NORMALIZER_VERSION
            and trades_path(out_dir, symbol, day).exists()
            and bars_path(out_dir, symbol, day).exists())


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def run(start: date, end: date, symbol: str, *, raw_dir: Path = DEFAULT_RAW_DIR,
        out_dir: Path = DEFAULT_OUT_DIR) -> RunSummary:
    """`start`~`end`(포함) UTC 일을 오름차순으로 정규화한다. 오류는 그 자리에서 예외."""
    if start > end:
        raise ValueError(f"start 가 end 보다 늦음: {start} > {end}")
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    mpath = manifest_path(out_dir, symbol)
    manifest = load_manifest(mpath)
    summary = RunSummary()

    # 구간 밖 전날은 비재귀 조건만 본다(그 전날의 prev_close 까지 검사하지 않는다).
    before = start - timedelta(days=1)
    before_raw = find_raw(raw_dir, symbol, before)
    before_entry = manifest.get(_ymd(before))
    chain: float | None = (before_entry["last_close"]
                           if before_raw and _entry_current(before_entry, before_raw, out_dir,
                                                            symbol, before)
                           else None)

    for day in _days(start, end):
        key = _ymd(day)
        raw = find_raw(raw_dir, symbol, day)
        if raw is None:
            log.warning("원본 없음, 건너뜀: %s %s (raw_dir=%s)", symbol, day, raw_dir)
            summary.missing.append(day)
            chain = None  # 구간이 끊기면 prev_close 를 넘기지 않는다
            continue

        prev_close = chain
        entry = manifest.get(key)
        if _entry_current(entry, raw, out_dir, symbol, day) and entry.get("prev_close") == prev_close:
            log.info("변경 없음, 건너뜀: %s %s", symbol, day)
            summary.skipped.append(day)
            chain = entry["last_close"]
            continue

        fp = fingerprint(raw)  # 읽기 전 지문: 읽는 도중 바뀌면 다음 실행이 다시 처리한다
        trades = read_trades(raw, symbols=(symbol,))
        if trades["trd_match_id"].duplicated().any():
            raise ValueError(f"trd_match_id 중복 남음: {raw}")
        bars = resample_1m(trades, symbol=symbol, prev_close=prev_close, day=day)
        write_parquet_atomic(trades, trades_path(out_dir, symbol, day))
        write_parquet_atomic(bars, bars_path(out_dir, symbol, day))

        chain = last_close(bars)
        manifest[key] = {
            "raw_path": str(raw),
            **fp,
            "version": NORMALIZER_VERSION,
            "prev_close": prev_close,
            "last_close": chain,
            "trades_rows": len(trades),
            "bars_rows": len(bars),
        }
        save_manifest(manifest, mpath)
        log.info("처리: %s %s trades=%d bars=%d", symbol, day, len(trades), len(bars))
        summary.processed.append(day)

    return summary


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="normalize", description=__doc__.split("\n")[0])
    p.add_argument("--start", type=_parse_date, required=True, help="YYYY-MM-DD (포함, UTC)")
    p.add_argument("--end", type=_parse_date, required=True, help="YYYY-MM-DD (포함, UTC)")
    p.add_argument("--symbol", default="XBTUSD")
    p.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    a = build_parser().parse_args(argv)
    try:
        s = run(a.start, a.end, a.symbol, raw_dir=a.raw_dir, out_dir=a.out)
    except Exception:
        log.exception("정규화 실패")
        return 1
    log.info("완료: 처리 %d일, 건너뜀 %d일, 원본 없음 %d일",
             len(s.processed), len(s.skipped), len(s.missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
