"""ingest.store — 정규화 일별 parquet 구간 로더·결측 일 판정 (tmp_path 에 normalize.run 으로 만든 픽스처)."""

import logging
from datetime import date
from pathlib import Path

import pandas as pd
import pandas.testing as pdt
import pytest

from src.ingest import normalize as nz
from src.ingest import store
from src.ingest.bars import resample_1m
from src.ingest.store import MissingDaysError, load_bars, load_trades, missing_days
from src.shared.schema import BARS_1M, TRADES, validate_bars_1m, validate_trades
from tests.test_normalize_cli import D1, D2, D3, D4, D5, SYM, _row, day_rows, write_raw


@pytest.fixture
def out(tmp_path):
    """원본 D1·D2·D3·D5(D4 결측)를 정규화한 출력 디렉터리."""
    raw, out = tmp_path / "raw", tmp_path / "out"
    for day, base, tag in ((D1, 7900.0, "a"), (D2, 8000.0, "b"), (D3, 8100.0, "c"), (D5, 8300.0, "e")):
        write_raw(raw / SYM / f"{day:%Y%m%d}.csv.gz", day_rows(day, base, tag))
    nz.run(D1, D5, SYM, raw_dir=raw, out_dir=out)
    return out


def _concat_daily(path_of, out, days):
    return pd.concat([pd.read_parquet(path_of(out, SYM, d)) for d in days], ignore_index=True)


# 1. 연속 구간 ---------------------------------------------------------------

def test_load_trades_contiguous(out):
    t = load_trades(D1, D3, SYM, out_dir=out)
    validate_trades(t)
    assert len(t) == 9 and set(t["symbol"]) == {SYM}
    assert t["ts"].is_monotonic_increasing
    pdt.assert_frame_equal(t, _concat_daily(nz.trades_path, out, (D1, D2, D3)))


def test_load_bars_contiguous_equals_batch_resample(out):
    b = load_bars(D1, D3, SYM, out_dir=out)
    validate_bars_1m(b)
    assert len(b) == 3 * 1440
    assert b["ts"].is_monotonic_increasing and b["ts"].is_unique
    assert b["ts"].iloc[0] == pd.Timestamp("2020-03-12 00:00", tz="UTC")
    assert b["ts"].iloc[-1] == pd.Timestamp("2020-03-14 23:59", tz="UTC")
    batch = resample_1m(load_trades(D1, D3, SYM, out_dir=out), symbol=SYM)
    pdt.assert_frame_equal(b, batch)


def test_single_day(out):
    assert len(load_trades(D2, D2, SYM, out_dir=out)) == 3
    b = load_bars(D2, D2, SYM, out_dir=out)
    assert len(b) == 1440 and b["close"].iloc[-1] == 8005.0


# 2. 결측 일 -----------------------------------------------------------------

@pytest.mark.parametrize("table", ["trades", "bars_1m"])
def test_missing_days_reports_gap(out, table):
    assert missing_days(D1, D5, SYM, table=table, out_dir=out) == [D4]
    assert missing_days(D1, D3, SYM, table=table, out_dir=out) == []


@pytest.mark.parametrize("loader", [load_bars, load_trades])
def test_missing_day_raises_by_default(out, loader):
    with pytest.raises(MissingDaysError) as ei:
        loader(D1, D5, SYM, out_dir=out)
    assert ei.value.days == [D4]
    assert isinstance(ei.value, ValueError) and "2020-03-15" in str(ei.value)


def test_allow_missing_warns_and_returns_present_days(out, caplog):
    with caplog.at_level(logging.WARNING, logger="src.ingest.store"):
        b = load_bars(D1, D5, SYM, out_dir=out, allow_missing=True)
    assert "2020-03-15" in caplog.text
    validate_bars_1m(b)
    # D1~D3 는 각 1440분. D5 는 prev_close 없음(D4 결측) → 첫 체결 00:00:05 의 분(00:00)부터
    # 23:59 까지라 역시 1440분. 합 4×1440, D4 의 분은 없다.
    assert len(b) == 4 * 1440
    assert not b["ts"].dt.date.eq(D4).any()
    d5 = b[b["ts"].dt.date == D5]
    assert d5["ts"].iloc[0] == pd.Timestamp("2020-03-16 00:00", tz="UTC")
    assert d5["open"].iloc[0] == 8300.0  # 전날 close 로 채우지 않은 첫 체결가

    t = load_trades(D1, D5, SYM, out_dir=out, allow_missing=True)
    assert len(t) == 12 and t["ts"].is_monotonic_increasing


def test_deleted_file_is_reported_via_shared_path_rule(out):
    nz.bars_path(out, SYM, D2).unlink()
    assert missing_days(D1, D3, SYM, out_dir=out) == [D2]
    assert missing_days(D1, D3, SYM, table="trades", out_dir=out) == []


def test_store_defines_no_path_literals():
    src = Path(store.__file__).read_text(encoding="utf-8")
    assert ".parquet" not in src and '"trades" /' not in src and '"bars_1m" /' not in src
    assert store.DEFAULT_OUT_DIR is nz.DEFAULT_OUT_DIR


# 3. 빈 구간 -----------------------------------------------------------------

def test_range_without_any_data(out):
    s, e = date(2020, 4, 1), date(2020, 4, 2)
    with pytest.raises(MissingDaysError) as ei:
        load_trades(s, e, SYM, out_dir=out)
    assert ei.value.days == [s, e]
    for loader, schema in ((load_trades, TRADES), (load_bars, BARS_1M)):
        df = loader(s, e, SYM, out_dir=out, allow_missing=True)
        assert len(df) == 0
        assert list(df.columns) == schema.column_names
        assert {c: str(t) for c, t in df.dtypes.items()} == {
            c.name: str(pd.Series([], dtype=c.dtype).dtype) for c in schema.columns}


def test_day_with_zero_target_trades(tmp_path):
    """D2 원본에 다른 종목 체결만 있음 → trades 0행, bars 는 D1 close 로 평탄 1440분."""
    raw, out = tmp_path / "raw", tmp_path / "out"
    write_raw(raw / SYM / f"{D1:%Y%m%d}.csv.gz", day_rows(D1, 7900.0, "a"))
    write_raw(raw / SYM / f"{D2:%Y%m%d}.csv.gz",
              [_row(D2, "01:00:00", "Buy", 5, 190.0, "x-e", symbol="ETHUSD")])
    nz.run(D1, D2, SYM, raw_dir=raw, out_dir=out)

    t2 = load_trades(D2, D2, SYM, out_dir=out)
    assert len(t2) == 0 and list(t2.columns) == TRADES.column_names
    assert len(load_trades(D1, D2, SYM, out_dir=out)) == 3

    b = load_bars(D1, D2, SYM, out_dir=out)
    assert len(b) == 2 * 1440
    flat = b[b["ts"].dt.date == D2]
    assert len(flat) == 1440
    assert (flat[["open", "high", "low", "close"]] == 7905.0).all().all()
    assert (flat["volume"] == 0).all()


# 4. 오류 --------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda o: missing_days(D3, D1, SYM, out_dir=o),
    lambda o: load_bars(D3, D1, SYM, out_dir=o),
    lambda o: load_trades(D3, D1, SYM, out_dir=o),
])
def test_start_after_end_raises(out, call):
    with pytest.raises(ValueError, match="start 가 end 보다 늦음"):
        call(out)


def test_unknown_table_raises(out):
    with pytest.raises(ValueError, match="알 수 없는 table"):
        missing_days(D1, D3, SYM, table="fills", out_dir=out)
