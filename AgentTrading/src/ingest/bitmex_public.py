"""BitMEX 공개 거래 장부(S3 일별 덤프) 확인·다운로드.

장부 위치: https://s3-eu-west-1.amazonaws.com/public.bitmex.com
  data/trade/YYYYMMDD.csv.gz  전 종목 체결 테이프 (총 ~50GB, 일 최대 ~130MB)
  data/quote/YYYYMMDD.csv.gz  최우선 호가 (총 ~330GB, 일 최대 ~530MB)

체결 테이프는 익명이다(계정 식별자 없음). 개인 거래 추출용이 아니라 시장 데이터로 쓴다.
설계·실측 근거: Obsidian `Projects/work/AgentTrading/task/phase0-bitmex-public-data.md`

    python -m src.ingest.bitmex_public inspect --start 2020-03-01 --end 2020-03-31
    python -m src.ingest.bitmex_public download --start 2020-03-12 --end 2020-03-13 --symbols XBTUSD

대용량 대응:
  - 청크 스트리밍 저장, `.part` + HTTP Range 이어받기, 완료 시 원자적 rename
  - listing Size와 크기 대조, gzip 끝까지 풀어보는 무결성 검사
  - 총량 상한(--max-gb)과 디스크 최소 여유(--min-free-gb)를 시작 전에 검사
  - --symbols 지정 시 종목별로 스트리밍 필터링 후 원본 삭제
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

import requests
import urllib3

log = logging.getLogger("bitmex_public")

BUCKET_URL = "https://s3-eu-west-1.amazonaws.com/public.bitmex.com"
DATASETS = ("trade", "quote")
S3_NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
CHUNK = 1 << 20  # 1 MiB
GB = 1_000_000_000
DAY_FILE = re.compile(r"^\d{8}\.csv\.gz$")
HEADER_ONLY_BYTES = 200  # 헤더만 있는 날(거래 0건) 판정 기준


@dataclass(frozen=True)
class RemoteFile:
    key: str
    size: int
    etag: str

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    @property
    def day(self) -> date:
        return datetime.strptime(self.name[:8], "%Y%m%d").date()


class DiskGuardError(RuntimeError):
    """용량 상한·디스크 여유 조건을 만족하지 못함."""


class IntegrityError(RuntimeError):
    """받은 파일의 크기 또는 gzip 무결성이 맞지 않음."""


# ---------------------------------------------------------------- listing


def list_files(
    dataset: str,
    start: date | None = None,
    end: date | None = None,
    *,
    base_url: str = BUCKET_URL,
    session: requests.Session | None = None,
) -> list[RemoteFile]:
    """S3 ListObjects(v1)를 marker로 끝까지 넘기며 날짜 구간의 일별 파일을 돌려준다."""
    if dataset not in DATASETS:
        raise ValueError(f"dataset must be one of {DATASETS}")
    s = session or requests.Session()
    prefix = f"data/{dataset}/"
    # 파일명이 YYYYMMDD 라 사전순 = 날짜순. 시작일 직전 키부터 받아 불필요한 페이지를 건너뛴다.
    marker = f"{prefix}{start:%Y%m%d}" if start else ""
    files: list[RemoteFile] = []
    while True:
        resp = _get(s, base_url, params={"prefix": prefix, "marker": marker})
        root = ET.fromstring(resp.content)
        keys = []
        for c in root.findall("s:Contents", S3_NS):
            key = c.findtext("s:Key", namespaces=S3_NS)
            keys.append(key)
            if not DAY_FILE.match(key.rsplit("/", 1)[-1]):  # "data/trade/" 같은 디렉터리 키 제외
                continue
            f = RemoteFile(key, int(c.findtext("s:Size", namespaces=S3_NS)),
                           c.findtext("s:ETag", namespaces=S3_NS).strip('"'))
            if start and f.day < start:
                continue
            if end and f.day > end:
                return files
            files.append(f)
        if root.findtext("s:IsTruncated", namespaces=S3_NS) != "true" or not keys:
            return files
        marker = root.findtext("s:NextMarker", namespaces=S3_NS) or keys[-1]


def peek_header(f: RemoteFile, *, base_url: str = BUCKET_URL,
                session: requests.Session | None = None, nbytes: int = 65536,
                nlines: int = 3) -> list[str]:
    """파일 앞부분만 Range로 받아 압축을 풀고 첫 줄들을 돌려준다 (전체 다운로드 없음)."""
    s = session or requests.Session()
    resp = _get(s, f"{base_url}/{f.key}", headers={"Range": f"bytes=0-{nbytes - 1}"})
    text = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(resp.content)
    lines = text.decode("utf-8", "replace").splitlines()
    # 잘린 마지막 줄은 버린다
    return lines[: min(nlines, max(len(lines) - 1, 1))]


# ---------------------------------------------------------------- plan / guard


@dataclass
class Plan:
    todo: list[RemoteFile]
    done: list[RemoteFile]
    todo_bytes: int
    partial_bytes: int  # 이미 받아 둔 .part 크기 (이어받기로 절약되는 양)
    free_bytes: int

    @property
    def need_bytes(self) -> int:
        return self.todo_bytes - self.partial_bytes


def raw_dir(out_dir: Path, symbols: Sequence[str] | None) -> Path:
    """필터 모드에서는 원본을 `_raw/` 에 임시로 받는다."""
    return out_dir / "_raw" if symbols else out_dir


def target_path(out_dir: Path, f: RemoteFile, symbols: Sequence[str] | None) -> Path:
    if symbols:
        return out_dir / "_".join(sorted(symbols)) / f.name
    return out_dir / f.name


def make_plan(files: Iterable[RemoteFile], out_dir: Path,
              symbols: Sequence[str] | None = None) -> Plan:
    out_dir.mkdir(parents=True, exist_ok=True)
    todo, done, partial = [], [], 0
    for f in files:
        final = target_path(out_dir, f, symbols)
        if final.exists() and (symbols or final.stat().st_size == f.size):
            done.append(f)
            continue
        todo.append(f)
        part = _part_path(raw_dir(out_dir, symbols), f)
        if part.exists():
            partial += min(part.stat().st_size, f.size)
    return Plan(todo, done, sum(f.size for f in todo), partial,
                shutil.disk_usage(out_dir).free)


def check_guard(plan: Plan, *, max_bytes: int, min_free_bytes: int) -> None:
    """총량 상한과 '다 받은 뒤에도 남을 여유'를 시작 전에 검사한다."""
    if plan.need_bytes > max_bytes:
        raise DiskGuardError(
            f"받을 양 {plan.need_bytes / GB:.2f}GB 가 상한 {max_bytes / GB:.2f}GB 를 넘는다. "
            "기간을 줄이거나 --max-gb 를 올려라.")
    # 필터 모드도 최악의 경우(원본 전부 보관)를 기준으로 본다 — 보수적으로.
    left = plan.free_bytes - plan.need_bytes
    if left < min_free_bytes:
        raise DiskGuardError(
            f"다운로드 후 여유 {left / GB:.2f}GB < 최소 {min_free_bytes / GB:.2f}GB "
            f"(현재 여유 {plan.free_bytes / GB:.2f}GB).")


# ---------------------------------------------------------------- download


def download_file(f: RemoteFile, out_dir: Path, *, base_url: str = BUCKET_URL,
                  session: requests.Session | None = None, verify_gzip: bool = True,
                  min_free_bytes: int = 0, retries: int = 4) -> Path:
    """한 파일을 `.part` 로 이어받기하며 받고, 검증 후 원자적으로 rename 한다."""
    s = session or requests.Session()
    final = out_dir / f.name
    if final.exists() and final.stat().st_size == f.size:
        return final
    part = _part_path(out_dir, f)
    part.parent.mkdir(parents=True, exist_ok=True)
    # 원격 객체가 다시 쓰였으면(ETag 변경) 받아 둔 조각은 버린다
    meta = part.with_name(part.name + ".etag")
    if part.exists() and (not meta.exists() or meta.read_text() != f.etag):
        part.unlink()
    meta.write_text(f.etag)

    for attempt in range(retries + 1):
        try:
            _fetch_into(s, f"{base_url}/{f.key}", part, f.size, f.etag, min_free_bytes)
            break
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError, urllib3.exceptions.HTTPError) as e:
            if attempt == retries:
                raise
            wait = 2 ** attempt
            log.warning("%s: %s — %ds 후 재시도 (%d/%d)", f.name, e, wait, attempt + 1, retries)
            time.sleep(wait)

    got = part.stat().st_size
    if got != f.size:
        part.unlink()
        meta.unlink()
        raise IntegrityError(f"{f.name}: 크기 불일치 {got} != {f.size}")
    if verify_gzip:
        try:
            _gzip_check(part)
        except (OSError, EOFError, zlib.error) as e:
            part.unlink()
            meta.unlink()
            raise IntegrityError(f"{f.name}: gzip 손상 — {e}") from e
    os.replace(part, final)
    meta.unlink()
    return final


def filter_symbols(src: Path, dst: Path, symbols: Sequence[str]) -> int:
    """gzip CSV를 스트리밍으로 읽어 symbol 이 일치하는 행만 남긴다. 남긴 행 수를 반환."""
    want = set(symbols)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    kept = 0
    with gzip.open(src, "rt", newline="") as fin, gzip.open(tmp, "wt", newline="") as fout:
        reader = csv.reader(fin)
        header = next(reader)
        col = header.index("symbol")
        writer = csv.writer(fout, lineterminator="\n")
        writer.writerow(header)
        for row in reader:
            if row[col] in want:
                writer.writerow(row)
                kept += 1
    os.replace(tmp, dst)
    return kept


def run_download(files: Sequence[RemoteFile], out_dir: Path, *,
                 symbols: Sequence[str] | None = None, keep_raw: bool = False,
                 base_url: str = BUCKET_URL, session: requests.Session | None = None,
                 verify_gzip: bool = True, min_free_bytes: int = 0) -> list[Path]:
    s = session or requests.Session()
    raw_out = raw_dir(out_dir, symbols)
    paths = []
    for i, f in enumerate(files, 1):
        t0 = time.monotonic()
        # 필터 모드는 원본과 필터 결과(≤ 원본)가 잠시 함께 존재한다
        raw = download_file(f, raw_out, base_url=base_url, session=s, verify_gzip=verify_gzip,
                            min_free_bytes=min_free_bytes + (f.size if symbols else 0))
        msg = f"[{i}/{len(files)}] {f.name} {f.size / 1e6:.1f}MB {time.monotonic() - t0:.1f}s"
        if symbols:
            dst = target_path(out_dir, f, symbols)
            kept = filter_symbols(raw, dst, symbols)
            msg += f" → {kept:,} rows {dst.stat().st_size / 1e6:.1f}MB"
            if not keep_raw:
                raw.unlink()
            paths.append(dst)
        else:
            paths.append(raw)
        log.info(msg)
    return paths


# ---------------------------------------------------------------- internals


def _part_path(raw: Path, f: RemoteFile) -> Path:
    return raw / (f.name + ".part")


def _get(s: requests.Session, url: str, **kw) -> requests.Response:
    resp = s.get(url, timeout=60, **kw)
    resp.raise_for_status()
    return resp


def _fetch_into(s: requests.Session, url: str, part: Path, size: int, etag: str,
                min_free_bytes: int) -> None:
    have = part.stat().st_size if part.exists() else 0
    if have > size:  # 깨진 조각
        part.unlink()
        have = 0
    if have == size:
        return
    if shutil.disk_usage(part.parent).free - (size - have) < min_free_bytes:
        raise DiskGuardError(f"{part.name}: 디스크 여유 부족")
    # If-Range: 그 사이 객체가 바뀌었으면 서버가 206 대신 200(전체)을 준다
    headers = {"Range": f"bytes={have}-", "If-Range": f'"{etag}"'} if have else {}
    with s.get(url, headers=headers, stream=True, timeout=60) as resp:
        if resp.status_code == 416:  # 범위가 맞지 않음 — 조각을 버리고 처음부터
            part.unlink()
            return _fetch_into(s, url, part, size, etag, min_free_bytes)
        resp.raise_for_status()
        mode = "ab"
        if have and (resp.status_code != 206 or
                     not resp.headers.get("Content-Range", "").startswith(f"bytes {have}-")):
            log.warning("%s: 이어받기 불가 응답(%d) — 처음부터 받는다", part.name, resp.status_code)
            mode = "wb"
        with open(part, mode) as out:
            # decode_content=False: 서버가 Content-Encoding 을 붙여도 .gz 바이트를 그대로 저장
            for chunk in resp.raw.stream(CHUNK, decode_content=False):
                out.write(chunk)


def _gzip_check(path: Path) -> None:
    with gzip.open(path, "rb") as g:
        while g.read(CHUNK * 8):
            pass


def _year_summary(files: Sequence[RemoteFile]) -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {}
    for f in files:
        n, b = out.get(f.day.year, (0, 0))
        out[f.day.year] = (n + 1, b + f.size)
    return out


# ---------------------------------------------------------------- CLI


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def cmd_inspect(a: argparse.Namespace) -> int:
    files = list_files(a.dataset, a.start, a.end, base_url=a.base_url)
    print(f"장부 위치 : {a.base_url}/data/{a.dataset}/")
    if not files:
        print("해당 구간 파일 없음")
        return 1
    total = sum(f.size for f in files)
    biggest = max(files, key=lambda f: f.size)
    print(f"구간      : {files[0].day} ~ {files[-1].day} ({len(files)} files)")
    print(f"총 용량   : {total / GB:.2f} GB (gzip)  최대 일 파일 {biggest.name} "
          f"{biggest.size / 1e6:.1f} MB")
    empty = [f for f in files if f.size <= HEADER_ONLY_BYTES]
    if empty:
        print(f"빈 날짜   : {len(empty)}일은 헤더만 있음(거래 0건) — 예: "
              + ", ".join(str(f.day) for f in empty[-5:]))
    print("연도별    :")
    for y, (n, b) in sorted(_year_summary(files).items()):
        print(f"  {y}  {n:4d} files  {b / GB:7.2f} GB")
    print("스키마    :")
    for line in peek_header(biggest, base_url=a.base_url):
        print(f"  {line}")
    plan = make_plan(files, a.out, a.symbols)
    print(f"로컬      : {a.out}  완료 {len(plan.done)} / 남음 {len(plan.todo)} "
          f"({plan.need_bytes / GB:.2f} GB)  디스크 여유 {plan.free_bytes / GB:.1f} GB")
    try:
        check_guard(plan, max_bytes=int(a.max_gb * GB), min_free_bytes=int(a.min_free_gb * GB))
        print("판정      : 다운로드 가능")
    except DiskGuardError as e:
        print(f"판정      : 불가 — {e}")
    return 0


def cmd_download(a: argparse.Namespace) -> int:
    files = list_files(a.dataset, a.start, a.end, base_url=a.base_url)
    if not files:
        log.error("해당 구간 파일 없음")
        return 1
    plan = make_plan(files, a.out, a.symbols)
    log.info("총 %d files: 완료 %d, 받을 %d (%.2f GB), 디스크 여유 %.1f GB",
             len(files), len(plan.done), len(plan.todo), plan.need_bytes / GB,
             plan.free_bytes / GB)
    try:
        check_guard(plan, max_bytes=int(a.max_gb * GB), min_free_bytes=int(a.min_free_gb * GB))
    except DiskGuardError as e:
        log.error("%s", e)
        return 2
    if a.dry_run:
        for f in plan.todo:
            print(f"{f.key}\t{f.size}")
        return 0
    run_download(plan.todo, a.out, symbols=a.symbols, keep_raw=a.keep_raw,
                 base_url=a.base_url, verify_gzip=not a.no_verify,
                 min_free_bytes=int(a.min_free_gb * GB))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bitmex_public", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (("inspect", cmd_inspect, "위치·용량·스키마·적재 가능 여부 확인"),
                            ("download", cmd_download, "날짜 구간 다운로드")):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=fn)
        sp.add_argument("--dataset", choices=DATASETS, default="trade")
        sp.add_argument("--start", type=_parse_date, help="YYYY-MM-DD (포함)")
        sp.add_argument("--end", type=_parse_date, help="YYYY-MM-DD (포함)")
        sp.add_argument("--symbols", nargs="+", help="예: XBTUSD — 해당 종목만 남기고 원본 삭제")
        sp.add_argument("--out", type=Path, default=Path("data/raw/bitmex"))
        sp.add_argument("--max-gb", type=float, default=5.0, help="받을 총량 상한 (기본 5)")
        sp.add_argument("--min-free-gb", type=float, default=20.0,
                        help="다운로드 후 남겨야 할 디스크 여유 (기본 20)")
        sp.add_argument("--base-url", default=BUCKET_URL, help=argparse.SUPPRESS)
    d = sub.choices["download"]
    d.add_argument("--keep-raw", action="store_true", help="--symbols 사용 시 원본도 보관")
    d.add_argument("--no-verify", action="store_true", help="gzip 무결성 검사 생략")
    d.add_argument("--dry-run", action="store_true", help="받을 목록만 출력")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    a = build_parser().parse_args(argv)
    if a.dataset:
        a.out = a.out / a.dataset
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
