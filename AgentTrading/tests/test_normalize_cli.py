"""ingest.normalize — 재실행 가능·증분 처리·중복 제거 CLI (네트워크 사용 안 함, tmp_path 픽스처)."""

import gzip
import json
import logging
import os
from datetime import date

import pandas as pd
import pandas.testing as pdt
import pytest

from src.ingest import normalize as nz
from src.ingest.bars import resample_1m
from src.shared.schema import validate_bars_1m, validate_trades

HEADER = "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional"
SYM = "XBTUSD"
D1, D2, D3, D4, D5 = (date(2020, 3, d) for d in (12, 13, 14, 15, 16))


def _row(day, hms, side, size, price, mid, symbol=SYM):
    return (f"{day:%Y-%m-%d}D{hms}.000000000,{symbol},{side},{size},{price},PlusTick,{mid},"
            f"{size * 1000},{size / price},{size}")


def write_raw(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="") as f:
        f.write("\n".join([HEADER, *rows]) + "\n")
    return path


def day_rows(day, base, tag):
    """그날 체결 3건(첫 체결 00:00 분) + 다른 종목 1건."""
    return [
        _row(day, "00:00:05", "Buy", 100, base, f"{tag}-1"),
        _row(day, "00:00:06", "Sell", 5, 190.0, f"{tag}-e", symbol="ETHUSD"),
        _row(day, "12:30:00", "Sell", 50, base + 10, f"{tag}-2"),
        _row(day, "23:59:30", "Buy", 20, base + 5, f"{tag}-3"),
    ]


@pytest.fixture
def env(tmp_path):
    raw = tmp_path / "raw"
    out = tmp_path / "out"
    for day, base, tag in ((D1, 7900.0, "a"), (D2, 8000.0, "b"), (D3, 8100.0, "c"), (D5, 8300.0, "e")):
        write_raw(raw / SYM / f"{day:%Y%m%d}.csv.gz", day_rows(day, base, tag))
    return raw, out


def cli(raw, out, start, end):
    return nz.main(["--start", f"{start:%Y-%m-%d}", "--end", f"{end:%Y-%m-%d}", "--symbol", SYM,
                    "--raw-dir", str(raw), "--out", str(out)])


def mtimes(out):
    return {p: p.stat().st_mtime_ns for p in sorted(out.rglob("*")) if p.is_file()}


# 1. CLI·경로·포맷 -----------------------------------------------------------

def test_cli_writes_daily_parquet_equal_to_batch(env):
    raw, out = env
    assert cli(raw, out, D1, D3) == 0
    trades, bars = [], []
    for day in (D1, D2, D3):
        t = pd.read_parquet(out / "trades" / SYM / f"{day:%Y%m%d}.parquet")
        b = pd.read_parquet(out / "bars_1m" / SYM / f"{day:%Y%m%d}.parquet")
        validate_trades(t)
        validate_bars_1m(b)
        assert len(t) == 3 and set(t["symbol"]) == {SYM}
        assert len(b) == 1440
        trades.append(t)
        bars.append(b)
    batch = resample_1m(pd.concat(trades, ignore_index=True), symbol=SYM)
    pdt.assert_frame_equal(pd.concat(bars, ignore_index=True), batch)
    m = json.loads((out / "_manifest" / f"{SYM}.json").read_text())
    assert sorted(m) == ["20200312", "20200313", "20200314"]
    assert m["20200312"]["prev_close"] is None
    assert m["20200313"]["prev_close"] == m["20200312"]["last_close"] == 7905.0
    assert m["20200312"]["version"] == nz.NORMALIZER_VERSION


# 2. 원자적 쓰기 -------------------------------------------------------------

def test_atomic_write_failure_leaves_nothing(env, monkeypatch):
    raw, out = env
    real = nz.write_parquet_atomic

    def boom(df, path):
        if "bars_1m" in str(path) and path.name == "20200313.parquet":
            def bad_write(tmp):
                tmp.write_bytes(b"half")  # 반쯤 쓰다 실패
                raise OSError("disk full")
            nz._replace_atomic(bad_write, path)
        real(df, path)

    monkeypatch.setattr(nz, "write_parquet_atomic", boom)
    assert cli(raw, out, D1, D3) == 1
    assert not (out / "bars_1m" / SYM / "20200313.parquet").exists()
    assert not list(out.rglob("*.tmp"))
    m = json.loads((out / "_manifest" / f"{SYM}.json").read_text())
    assert sorted(m) == ["20200312"]  # 실패한 날·이후 날은 기록 없음
    assert not (out / "trades" / SYM / "20200314.parquet").exists()

    monkeypatch.setattr(nz, "write_parquet_atomic", real)
    s = nz.run(D1, D3, SYM, raw_dir=raw, out_dir=out)
    assert s.skipped == [D1] and s.processed == [D2, D3]


# 3. 증분 --------------------------------------------------------------------

def test_changed_raw_reprocesses_only_that_day_and_chain(env):
    raw, out = env
    nz.run(D1, D3, SYM, raw_dir=raw, out_dir=out)
    before = mtimes(out)
    d1_files = [nz.trades_path(out, SYM, D1), nz.bars_path(out, SYM, D1)]

    # 둘째 날 마지막 가격 변경 → last_close 가 바뀌어 셋째 날 bars 도 연쇄 재처리
    rows = day_rows(D2, 8000.0, "b")
    rows[-1] = _row(D2, "23:59:30", "Buy", 20, 8888.0, "b-3")
    write_raw(raw / SYM / "20200313.csv.gz", rows)
    s = nz.run(D1, D3, SYM, raw_dir=raw, out_dir=out)
    assert s.processed == [D2, D3] and s.skipped == [D1]
    for p in d1_files:
        assert p.stat().st_mtime_ns == before[p]
    b3 = pd.read_parquet(nz.bars_path(out, SYM, D3))
    assert b3["open"].iloc[0] == 8100.0  # 셋째 날 첫 분은 자체 체결
    m = nz.load_manifest(nz.manifest_path(out, SYM))
    assert m["20200314"]["prev_close"] == 8888.0


def test_mtime_only_change_reprocesses_that_day(env):
    raw, out = env
    nz.run(D1, D3, SYM, raw_dir=raw, out_dir=out)
    p = raw / SYM / "20200313.csv.gz"
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    s = nz.run(D1, D3, SYM, raw_dir=raw, out_dir=out)
    # 내용이 같아 last_close 도 같으므로 셋째 날은 건너뛴다
    assert s.processed == [D2] and s.skipped == [D1, D3]


def test_version_bump_reprocesses_all(env, monkeypatch):
    raw, out = env
    nz.run(D1, D3, SYM, raw_dir=raw, out_dir=out)
    monkeypatch.setattr(nz, "NORMALIZER_VERSION", nz.NORMALIZER_VERSION + 1)
    assert nz.run(D1, D3, SYM, raw_dir=raw, out_dir=out).processed == [D1, D2, D3]


def test_later_backfill_of_previous_day_fixes_prev_close(env):
    raw, out = env
    nz.run(D2, D3, SYM, raw_dir=raw, out_dir=out)
    m = nz.load_manifest(nz.manifest_path(out, SYM))
    assert m["20200313"]["prev_close"] is None  # 전날 미처리
    s = nz.run(D1, D3, SYM, raw_dir=raw, out_dir=out)
    assert s.processed == [D1, D2] and s.skipped == [D3]
    m = nz.load_manifest(nz.manifest_path(out, SYM))
    assert m["20200313"]["prev_close"] == m["20200312"]["last_close"] == 7905.0


def test_previous_day_outside_range_used_non_recursively(env):
    raw, out = env
    nz.run(D1, D2, SYM, raw_dir=raw, out_dir=out)
    s = nz.run(D3, D3, SYM, raw_dir=raw, out_dir=out)
    assert s.processed == [D3]
    m = nz.load_manifest(nz.manifest_path(out, SYM))
    assert m["20200314"]["prev_close"] == m["20200313"]["last_close"]


# 4. 중복 제거 ---------------------------------------------------------------

def test_duplicate_trd_match_id_removed(env):
    raw, out = env
    rows = day_rows(D1, 7900.0, "a")
    rows.insert(1, _row(D1, "00:00:07", "Sell", 999, 1.0, "a-1"))  # 같은 trdMatchID 의 둘째 행
    write_raw(raw / SYM / "20200312.csv.gz", rows)
    nz.run(D1, D1, SYM, raw_dir=raw, out_dir=out)
    t = pd.read_parquet(nz.trades_path(out, SYM, D1))
    assert t["trd_match_id"].is_unique and len(t) == 3
    assert t.loc[t["trd_match_id"] == "a-1", "size"].item() == 100  # 첫 행 유지

    os.utime(raw / SYM / "20200312.csv.gz", ns=(0, 10**18))  # 강제 재처리
    assert nz.run(D1, D1, SYM, raw_dir=raw, out_dir=out).processed == [D1]
    assert len(pd.read_parquet(nz.trades_path(out, SYM, D1))) == 3


# 5. 재실행 무변경 -----------------------------------------------------------

def test_second_run_rewrites_nothing(env, monkeypatch):
    raw, out = env
    assert cli(raw, out, D1, D5) == 0
    before = mtimes(out)
    calls = []
    monkeypatch.setattr(nz, "write_parquet_atomic", lambda *a: calls.append(a))
    monkeypatch.setattr(nz, "save_manifest", lambda *a: calls.append(a))
    s = nz.run(D1, D5, SYM, raw_dir=raw, out_dir=out)
    assert s.processed == [] and s.skipped == [D1, D2, D3, D5] and s.missing == [D4]
    assert calls == []
    assert mtimes(out) == before
    assert cli(raw, out, D1, D5) == 0


# 6. 원본 결측 ---------------------------------------------------------------

def test_missing_day_warns_and_breaks_chain(env, caplog):
    raw, out = env
    with caplog.at_level(logging.WARNING, logger="src.ingest.normalize"):
        assert cli(raw, out, D3, D5) == 0
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and "2020-03-15" in warns[0].getMessage()
    assert not nz.trades_path(out, SYM, D4).exists() and not nz.bars_path(out, SYM, D4).exists()
    b5 = pd.read_parquet(nz.bars_path(out, SYM, D5))
    assert b5["ts"].iloc[0] == pd.Timestamp("2020-03-16 00:00", tz="UTC")  # 첫 체결 분부터
    assert nz.load_manifest(nz.manifest_path(out, SYM))["20200316"]["prev_close"] is None


def test_missing_day_does_not_delete_existing_output(env):
    raw, out = env
    nz.run(D1, D1, SYM, raw_dir=raw, out_dir=out)
    (raw / SYM / "20200312.csv.gz").unlink()
    s = nz.run(D1, D1, SYM, raw_dir=raw, out_dir=out)
    assert s.missing == [D1]
    assert nz.trades_path(out, SYM, D1).exists()


def test_zero_symbol_trades_without_prev_close_writes_empty_and_breaks_chain(env):
    raw, out = env
    write_raw(raw / SYM / "20200312.csv.gz", [_row(D1, "00:00:01", "Buy", 1, 190.0, "x", "ETHUSD")])
    nz.run(D1, D2, SYM, raw_dir=raw, out_dir=out)
    assert len(pd.read_parquet(nz.bars_path(out, SYM, D1))) == 0
    m = nz.load_manifest(nz.manifest_path(out, SYM))
    assert m["20200312"]["last_close"] is None and m["20200313"]["prev_close"] is None


def test_zero_symbol_trades_with_prev_close_fills_flat_day(env):
    raw, out = env
    write_raw(raw / SYM / "20200313.csv.gz", [_row(D2, "00:00:01", "Buy", 1, 190.0, "x", "ETHUSD")])
    nz.run(D1, D2, SYM, raw_dir=raw, out_dir=out)
    b2 = pd.read_parquet(nz.bars_path(out, SYM, D2))
    assert len(b2) == 1440 and (b2["close"] == 7905.0).all() and (b2["trade_count"] == 0).all()


# 7. 원본 위치 우선순위 -------------------------------------------------------

def test_symbol_dir_preferred_over_all_symbols_file(env):
    raw, out = env
    write_raw(raw / "20200312.csv.gz", [_row(D1, "00:00:01", "Buy", 1, 1.0, "z")])
    assert nz.find_raw(raw, SYM, D1) == raw / SYM / "20200312.csv.gz"
    nz.run(D1, D1, SYM, raw_dir=raw, out_dir=out)
    assert len(pd.read_parquet(nz.trades_path(out, SYM, D1))) == 3


def test_all_symbols_file_used_and_filtered(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    write_raw(raw / "20200312.csv.gz", day_rows(D1, 7900.0, "a"))
    s = nz.run(D1, D1, SYM, raw_dir=raw, out_dir=out)
    assert s.processed == [D1]
    t = pd.read_parquet(nz.trades_path(out, SYM, D1))
    assert set(t["symbol"]) == {SYM} and len(t) == 3


# 기타 ------------------------------------------------------------------------

def test_out_of_day_trade_fails_fast(env):
    raw, out = env
    rows = day_rows(D2, 8000.0, "b") + [_row(D3, "00:00:01", "Buy", 1, 1.0, "late")]
    write_raw(raw / SYM / "20200313.csv.gz", rows)
    assert cli(raw, out, D1, D3) == 1
    assert sorted(nz.load_manifest(nz.manifest_path(out, SYM))) == ["20200312"]


def test_start_after_end_rejected(env):
    raw, out = env
    with pytest.raises(ValueError):
        nz.run(D3, D1, SYM, raw_dir=raw, out_dir=out)
