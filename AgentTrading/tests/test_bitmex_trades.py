"""ingest.bitmex_trades — 소형 gzip 픽스처로 파싱·필터·청크·검증 (네트워크 사용 안 함)."""

import gzip

import pandas as pd
import pandas.testing as pdt
import pytest

from src.ingest import bitmex_trades as bt
from src.shared.schema import TRADES, UTC_NS, empty_frame, validate_trades

OLD_HEADER = "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional"
NEW_HEADER = OLD_HEADER + ",trdType,pool"

OLD_ROWS = [
    "2020-03-12D00:00:26.142291000,XBTUSD,Buy,100,7900.5,PlusTick,id-1,1265743,0.01265743,100",
    "2020-03-12D00:00:26.142291000,ETHUSD,Sell,5,190.1,MinusTick,id-2,95050,0.00095,5",
    "2020-03-12D00:00:27.000000001,XBTUSD,Sell,2500,7899,MinusTick,id-3,31649575,0.31649575,2500",
    "2020-03-12D23:59:59.999999999,XBTUSD,Buy,1,7950,ZeroPlusTick,id-4,12578,0.00012578,1",
]
NEW_ROWS = [
    "2024-01-01D00:00:00.123456789,XBTUSD,Buy,10,42000,PlusTick,n-1,23810,0.0002381,10,Regular,",
    "2024-01-01D00:00:01.000000000,XBTUSD,Sell,20,41999.5,MinusTick,n-2,47619,0.00047619,20,,",
    "2024-01-01D00:00:02.500000000,ETHUSD,Buy,3,2300,PlusTick,n-3,1,0.001,3,Regular,poolA",
]


def write_gz(tmp_path, name, header, rows):
    p = tmp_path / name
    with gzip.open(p, "wt", newline="") as f:
        f.write("\n".join([header, *rows]) + "\n")
    return p


# 1. 타임스탬프 ------------------------------------------------------------

def test_parse_timestamp_nanosecond_precision():
    raw = pd.Series(["2020-03-12D00:00:26.142291000", "2020-03-12D23:59:59.999999999",
                     "2020-03-12D00:00:26.142291001"])
    ts = bt.parse_timestamp(raw)
    assert ts.dtype == UTC_NS
    assert ts[0] == pd.Timestamp("2020-03-12T00:00:26.142291000", tz="UTC")
    assert ts[1] == pd.Timestamp("2020-03-12T23:59:59.999999999", tz="UTC")
    assert ts[1].value % 1_000_000_000 == 999_999_999
    assert ts[2].value - ts[0].value == 1


@pytest.mark.parametrize("bad", ["2020-03-12 00:00:26.142291000", "2020-13-12D00:00:26.1", "garbage"])
def test_parse_timestamp_rejects_bad_format(bad):
    with pytest.raises(ValueError):
        bt.parse_timestamp(pd.Series([bad]))


# 2·3. 구·신 컬럼 -----------------------------------------------------------

def test_old_format(tmp_path):
    p = write_gz(tmp_path, "20200312.csv.gz", OLD_HEADER, OLD_ROWS)
    df = bt.read_trades(p)
    validate_trades(df)
    assert list(df.columns) == TRADES.column_names
    assert df.dtypes.to_dict() == empty_frame(TRADES).dtypes.to_dict()
    assert df["trd_match_id"].tolist() == ["id-1", "id-3", "id-4"]
    assert df["trd_type"].isna().all() and df["pool"].isna().all()
    r = df.iloc[0]
    assert r["ts"] == pd.Timestamp("2020-03-12T00:00:26.142291", tz="UTC")
    assert (r["symbol"], r["side"], r["size"], r["price"]) == ("XBTUSD", "buy", 100, 7900.5)
    assert (r["tick_direction"], r["gross_value"]) == ("PlusTick", 1265743)
    assert (r["home_notional"], r["foreign_notional"]) == (0.01265743, 100.0)
    assert df["size"].dtype == "int64" and df["gross_value"].dtype == "int64"
    assert df["side"].tolist() == ["buy", "sell", "buy"]


def test_new_format_keeps_trd_type_pool_and_empty_is_na(tmp_path):
    p = write_gz(tmp_path, "20240101.csv.gz", NEW_HEADER, NEW_ROWS)
    df = bt.read_trades(p, symbols=None)
    validate_trades(df)
    assert df["trd_type"].tolist()[0] == "Regular"
    assert df["trd_type"].isna().tolist() == [False, True, False]
    assert df["pool"].isna().tolist() == [True, True, False]
    assert df["pool"].iloc[2] == "poolA"
    assert df["ts"].iloc[0].value % 1_000_000_000 == 123_456_789


# 4. 빈 파일 ---------------------------------------------------------------

@pytest.mark.parametrize("header", [OLD_HEADER, NEW_HEADER])
def test_header_only_file_gives_empty_frame(tmp_path, header):
    p = write_gz(tmp_path, "20200101.csv.gz", header, [])
    df = bt.read_trades(p)
    assert len(df) == 0
    pdt.assert_frame_equal(df, empty_frame(TRADES))
    validate_trades(df)
    assert list(bt.iter_trades(p)) == []


# 5. symbol 필터 -----------------------------------------------------------

def test_symbol_filter(tmp_path):
    p = write_gz(tmp_path, "20200312.csv.gz", OLD_HEADER, OLD_ROWS)
    assert set(bt.read_trades(p)["symbol"]) == {"XBTUSD"}
    assert len(bt.read_trades(p, symbols=None)) == 4
    assert bt.read_trades(p, symbols=["ETHUSD"])["trd_match_id"].tolist() == ["id-2"]
    none = bt.read_trades(p, symbols=["SOLUSD"])
    pdt.assert_frame_equal(none, empty_frame(TRADES))


# 6. 청크 -----------------------------------------------------------------

def test_chunked_read_equals_single_read(tmp_path):
    p = write_gz(tmp_path, "20200312.csv.gz", OLD_HEADER, OLD_ROWS)
    small = bt.read_trades(p, symbols=None, chunksize=2)
    big = bt.read_trades(p, symbols=None, chunksize=1_000)
    pdt.assert_frame_equal(small, big)
    chunks = list(bt.iter_trades(p, symbols=None, chunksize=2))
    assert len(chunks) == 2
    for c in chunks:
        validate_trades(c)
    assert sum(len(c) for c in chunks) == 4


# 7. 정렬·중복 -------------------------------------------------------------

def test_stable_sort_and_dedup(tmp_path):
    rows = [
        "2020-03-12D00:00:02.000000000,XBTUSD,Buy,1,100,PlusTick,c,100,0.01,1",
        "2020-03-12D00:00:01.000000000,XBTUSD,Buy,2,100,PlusTick,a,100,0.02,2",
        "2020-03-12D00:00:01.000000000,XBTUSD,Sell,3,100,MinusTick,b,100,0.03,3",
        "2020-03-12D00:00:01.000000000,XBTUSD,Buy,4,100,ZeroPlusTick,a,100,0.04,4",
        "2020-03-12D00:00:00.500000000,XBTUSD,Sell,5,100,MinusTick,d,100,0.05,5",
    ]
    p = write_gz(tmp_path, "20200312.csv.gz", OLD_HEADER, rows)
    for cs in (1, 2, 100):
        df = bt.read_trades(p, chunksize=cs)
        assert df["trd_match_id"].tolist() == ["d", "a", "b", "c"], cs
        assert df.loc[df["trd_match_id"] == "a", "size"].item() == 2  # 중복은 첫 행
        assert df["ts"].is_monotonic_increasing


# 8. 오류 ----------------------------------------------------------------

def test_unknown_column_raises(tmp_path):
    p = write_gz(tmp_path, "x.csv.gz", NEW_HEADER + ",extra", [NEW_ROWS[0] + ",1"])
    with pytest.raises(ValueError, match="알 수 없는"):
        bt.read_trades(p)


def test_missing_required_column_raises(tmp_path):
    header = OLD_HEADER.replace(",grossValue", "")
    row = "2020-03-12D00:00:26.142291000,XBTUSD,Buy,100,7900.5,PlusTick,id-1,0.01,100"
    p = write_gz(tmp_path, "x.csv.gz", header, [row])
    with pytest.raises(ValueError, match="누락"):
        bt.read_trades(p)


def test_missing_required_column_raises_on_header_only(tmp_path):
    p = write_gz(tmp_path, "x.csv.gz", OLD_HEADER.replace(",side", ""), [])
    with pytest.raises(ValueError, match="누락"):
        bt.read_trades(p)


@pytest.mark.parametrize("field,value", [(3, "1.5"), (7, "abc"), (3, "")])
def test_bad_numeric_raises(tmp_path, field, value):
    parts = OLD_ROWS[0].split(",")
    parts[field] = value
    p = write_gz(tmp_path, "x.csv.gz", OLD_HEADER, [",".join(parts)])
    with pytest.raises(ValueError, match="변환 실패"):
        bt.read_trades(p)


def test_bad_side_fails_schema(tmp_path):
    p = write_gz(tmp_path, "x.csv.gz", OLD_HEADER, [OLD_ROWS[0].replace("Buy", "Hold")])
    with pytest.raises(ValueError, match="허용값"):
        bt.read_trades(p)
