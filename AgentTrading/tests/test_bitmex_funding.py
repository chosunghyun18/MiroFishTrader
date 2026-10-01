"""BitMEX 펀딩 수집기·FUNDING 스키마·로더 테스트 — 네트워크 없이 모의 세션·가짜 sleep 으로 돈다.

모의 응답 형식은 Obsidian `research/bitmex-funding-history` 의 실측 필드·ISO 시각 형식을 따른다.
값은 합성(약관 18.6(a)).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from src.ingest import bitmex_funding as bf
from src.shared.schema import FUNDING, SchemaError, empty_frame, validate_funding

T0 = pd.Timestamp("2018-03-01 04:00", tz="UTC")


def _iso(t: pd.Timestamp) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_records(n: int, t0: pd.Timestamp = T0, symbol: str = "XBTUSD") -> list[dict]:
    return [{"timestamp": _iso(t0 + pd.Timedelta(hours=8 * i)), "symbol": symbol,
             "fundingInterval": "2000-01-01T08:00:00.000Z",
             "fundingRate": round(0.0001 * ((i % 7) - 3), 6),
             "fundingRateDaily": round(0.0003 * ((i % 7) - 3), 6)} for i in range(n)]


class FakeResp:
    def __init__(self, status: int = 200, body=None, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """`startTime ≤ timestamp ≤ endTime`(endTime 포함 경계로 흉내) 레코드를 count 개씩 돌려준다."""

    def __init__(self, records: list[dict], prefix: list[FakeResp] | None = None):
        self.records = records
        self.prefix = list(prefix or [])
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        assert url == bf.API_URL
        self.calls.append(dict(params))
        if self.prefix:
            return self.prefix.pop(0)
        lo, hi = pd.Timestamp(params["startTime"]), pd.Timestamp(params["endTime"])
        sel = [r for r in self.records if lo <= pd.Timestamp(r["timestamp"]) <= hi]
        return FakeResp(200, sel[: int(params["count"])])


class SleepLog(list):
    def __call__(self, s):
        self.append(s)


def frame(times, rates=None, symbol="XBTUSD") -> pd.DataFrame:
    ts = pd.DatetimeIndex(pd.to_datetime(times, utc=True)).as_unit("ns")
    rates = [0.0001] * len(ts) if rates is None else rates
    return pd.DataFrame({"ts": pd.Series(ts, dtype=FUNDING.dtypes["ts"]),
                         "symbol": pd.Series([symbol] * len(ts), dtype="string"),
                         "funding_rate": pd.Series(rates, dtype="float64")})


def grid_frame(start, end) -> pd.DataFrame:
    return frame(list(bf._grid(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"))))


# 1. 스키마 -----------------------------------------------------------------

def test_schema_accepts_valid_frame():
    df = frame(["2021-01-01 04:00", "2021-01-01 12:00"], [0.0001, -0.0002])
    assert validate_funding(df) is df
    assert FUNDING.column_names == ["ts", "symbol", "funding_rate"]
    assert FUNDING.key == ("symbol", "ts")
    validate_funding(empty_frame(FUNDING))


@pytest.mark.parametrize("mutate", [
    lambda d: d.assign(ts=d["ts"].dt.tz_localize(None)),
    lambda d: d.assign(funding_rate=[np.nan, 0.0001]),
    lambda d: d.assign(funding_rate=[np.inf, 0.0001]),
    lambda d: d.assign(funding_rate=[0.0001, -np.inf]),
    lambda d: d.assign(ts=[d["ts"][0], d["ts"][0]]),
    lambda d: d.assign(fundingRateDaily=[0.0003, 0.0003]),
    lambda d: d.drop(columns=["symbol"]),
])
def test_schema_rejects(mutate):
    df = frame(["2021-01-01 04:00", "2021-01-01 12:00"])
    with pytest.raises(SchemaError):
        validate_funding(mutate(df))


# 2·3. 페이지 이어받기·요청 간 지연 ------------------------------------------

def test_pagination_resumes_from_last_timestamp():
    recs = make_records(1200)
    sess, sl = FakeSession(recs), SleepLog()
    start, end = T0.floor("D"), T0 + pd.Timedelta(hours=8 * 1200)
    df = bf.fetch_funding("XBTUSD", start, end, session=sess, sleep=sl)
    assert len(sess.calls) == 3
    assert all(c["count"] == 500 and c["reverse"] == "false" and c["symbol"] == "XBTUSD"
               for c in sess.calls)
    assert sess.calls[0]["startTime"] == "2018-03-01T00:00:00.000Z"
    assert sess.calls[1]["startTime"] == recs[499]["timestamp"]
    assert sess.calls[2]["startTime"] == recs[998]["timestamp"]
    # 페이지 경계 겹침 행(499, 998)이 버려져 정확히 1,200행
    assert len(df) == 1200
    assert not df.duplicated(["symbol", "ts"]).any()
    assert df["ts"].is_monotonic_increasing
    assert df["ts"].min() >= start and df["ts"].max() < end
    assert list(df["ts"]) == [pd.Timestamp(r["timestamp"]) for r in recs]
    assert sl == [1.0, 1.0]  # 요청 수 − 1 번


def test_pagination_stops_when_no_new_rows():
    recs = make_records(500)  # 정확히 한 페이지 → 두 번째 요청은 겹침 1행뿐
    sess = FakeSession(recs)
    df = bf.fetch_funding("XBTUSD", T0, T0 + pd.Timedelta(days=1000), session=sess,
                          sleep=SleepLog())
    assert len(sess.calls) == 2
    assert len(df) == 500


def test_end_boundary_excluded():
    recs = make_records(10)
    end = T0 + pd.Timedelta(hours=8 * 5)  # 6번째 정산 시각 = end, API 는 포함해 돌려줌
    df = bf.fetch_funding("XBTUSD", T0, end, session=FakeSession(recs), sleep=SleepLog())
    assert len(df) == 5
    assert df["ts"].max() < end


# 4. 429 -------------------------------------------------------------------

def test_429_waits_retry_after_then_succeeds():
    sess, sl = FakeSession(make_records(3), [FakeResp(429, None, {"Retry-After": "7"})]), SleepLog()
    df = bf.fetch_funding("XBTUSD", T0, T0 + pd.Timedelta(days=1), session=sess, sleep=sl)
    assert sl == [7.0]
    assert len(sess.calls) == 2 and len(df) == 3


def test_429_without_retry_after_waits_60():
    sess, sl = FakeSession(make_records(3), [FakeResp(429)]), SleepLog()
    bf.fetch_funding("XBTUSD", T0, T0 + pd.Timedelta(days=1), session=sess, sleep=sl)
    assert sl == [60.0]


def test_429_exceeds_retries():
    sess, sl = FakeSession(make_records(3), [FakeResp(429)] * 4), SleepLog()
    with pytest.raises(bf.FundingFetchError):
        bf.fetch_funding("XBTUSD", T0, T0 + pd.Timedelta(days=1), session=sess, sleep=sl)
    assert len(sess.calls) == 4 and sl == [60.0] * 3


def test_non_list_body_and_http_error():
    with pytest.raises(bf.FundingFetchError):
        bf.fetch_funding("XBTUSD", T0, T0 + pd.Timedelta(days=1),
                         session=FakeSession([], [FakeResp(200, {"error": "x"})]), sleep=SleepLog())
    with pytest.raises(requests.HTTPError):
        bf.fetch_funding("XBTUSD", T0, T0 + pd.Timedelta(days=1),
                         session=FakeSession([], [FakeResp(503)]), sleep=SleepLog())


# 5. 정규화 ------------------------------------------------------------------

def test_normalize_columns_and_dtypes():
    df = bf.fetch_funding("XBTUSD", T0, T0 + pd.Timedelta(days=1),
                          session=FakeSession(make_records(3)), sleep=SleepLog())
    assert list(df.columns) == ["ts", "symbol", "funding_rate"]
    assert str(df["ts"].dtype) == "datetime64[ns, UTC]"
    assert str(df["symbol"].dtype) == "string"
    assert df["funding_rate"].dtype == np.float64
    assert df["ts"][0] == T0
    assert df["funding_rate"].tolist() == [-0.0003, -0.0002, -0.0001]


def test_normalize_rejects_other_symbol():
    recs = make_records(3)
    recs[1]["symbol"] = "ETHUSD"
    with pytest.raises(bf.FundingFetchError):
        bf.fetch_funding("XBTUSD", T0, T0 + pd.Timedelta(days=1), session=FakeSession(recs),
                         sleep=SleepLog())


def test_empty_response_gives_empty_frame():
    df = bf.fetch_funding("XBTUSD", T0, T0 + pd.Timedelta(days=1), session=FakeSession([]),
                          sleep=SleepLog())
    assert len(df) == 0 and list(df.columns) == FUNDING.column_names


# 6. 중복·증분 ----------------------------------------------------------------

def _fetch(recs, start, end):
    return bf.fetch_funding("XBTUSD", start, end, session=FakeSession(recs), sleep=SleepLog())


def test_rerun_same_range_is_byte_identical(tmp_path):
    recs = make_records(900)
    start, end = T0, T0 + pd.Timedelta(hours=8 * 900)
    p = bf.write_funding(_fetch(recs, start, end), "XBTUSD", tmp_path)
    first = p.read_bytes()
    bf.write_funding(_fetch(recs, start, end), "XBTUSD", tmp_path)
    assert p.read_bytes() == first
    assert len(pd.read_parquet(p)) == 900
    assert not list(tmp_path.glob("*.tmp"))


def test_incremental_equals_full(tmp_path):
    recs = make_records(900)
    mid, end = T0 + pd.Timedelta(hours=8 * 450), T0 + pd.Timedelta(hours=8 * 900)
    inc, full = tmp_path / "inc", tmp_path / "full"
    bf.write_funding(_fetch(recs, T0, mid), "XBTUSD", inc)
    bf.write_funding(_fetch(recs, T0, end), "XBTUSD", inc)
    bf.write_funding(_fetch(recs, T0, end), "XBTUSD", full)
    a = pd.read_parquet(bf.funding_path(inc, "XBTUSD"))
    b = pd.read_parquet(bf.funding_path(full, "XBTUSD"))
    pd.testing.assert_frame_equal(a, b)
    assert bf.funding_path(inc, "XBTUSD").read_bytes() == bf.funding_path(full, "XBTUSD").read_bytes()


def test_merge_new_value_wins(tmp_path):
    old = frame(["2021-01-01 04:00", "2021-01-01 12:00"], [0.0001, 0.0002])
    new = frame(["2021-01-01 12:00", "2021-01-01 20:00"], [0.0005, 0.0003])
    bf.write_funding(old, "XBTUSD", tmp_path)
    bf.write_funding(new, "XBTUSD", tmp_path)
    df = bf.load_funding("XBTUSD", "2021-01-01", "2021-01-02", tmp_path)
    assert df["funding_rate"].tolist() == [0.0001, 0.0005, 0.0003]


# 7. 간격 검사 ----------------------------------------------------------------

def test_gaps_complete_grid():
    df = grid_frame("2021-01-01", "2021-01-11")
    assert len(df) == 30
    assert bf.check_funding_gaps(df, "2021-01-01", "2021-01-11") == []


def test_gaps_missing_settlement():
    df = grid_frame("2021-01-01", "2021-01-04")
    drop = pd.Timestamp("2021-01-02 12:00", tz="UTC")
    df = df[df["ts"] != drop].reset_index(drop=True)
    assert bf.check_funding_gaps(df, "2021-01-01", "2021-01-04") == [bf.FundingGap(drop, "missing")]


def test_gaps_off_grid_row():
    extra = pd.Timestamp("2021-01-02 05:00", tz="UTC")
    df = pd.concat([grid_frame("2021-01-01", "2021-01-04"), frame([extra])], ignore_index=True)
    assert bf.check_funding_gaps(df, "2021-01-01", "2021-01-04") == [bf.FundingGap(extra, "off_grid")]


def test_gaps_nan_rate_is_missing():
    df = grid_frame("2021-01-01", "2021-01-02")
    df.loc[1, "funding_rate"] = np.nan
    assert bf.check_funding_gaps(df, "2021-01-01", "2021-01-02") == [
        bf.FundingGap(pd.Timestamp("2021-01-01 12:00", tz="UTC"), "missing")]


def test_gaps_half_open_range():
    # end = 2021-01-01 20:00 → 20:00 은 기대 격자에 없음, 04:00 시작은 포함
    df = frame(["2021-01-01 04:00", "2021-01-01 12:00"])
    assert bf.check_funding_gaps(df, "2021-01-01 04:00", "2021-01-01 20:00") == []
    # 구간 밖 행은 무시
    df2 = frame(["2020-12-31 20:00", "2021-01-01 04:00", "2021-01-01 12:00", "2021-01-01 20:00"])
    assert bf.check_funding_gaps(df2, "2021-01-01 04:00", "2021-01-01 20:00") == []
    # end 직후 시작 구간은 20:00 을 기대
    assert bf.check_funding_gaps(df, "2021-01-01", "2021-01-01 20:00:01") == [
        bf.FundingGap(pd.Timestamp("2021-01-01 20:00", tz="UTC"), "missing")]


def test_gaps_empty_frame_reports_all_missing():
    gaps = bf.check_funding_gaps(empty_frame(FUNDING), "2021-01-01", "2021-01-02")
    assert [g.kind for g in gaps] == ["missing"] * 3
    assert all(g.kind in bf.GAP_KINDS for g in gaps)


# 8. 로더 --------------------------------------------------------------------

def test_load_funding_half_open(tmp_path):
    bf.write_funding(grid_frame("2021-01-01", "2021-01-05"), "XBTUSD", tmp_path)
    df = bf.load_funding("XBTUSD", "2021-01-02", "2021-01-03 12:00", tmp_path)
    assert [t.strftime("%d %H") for t in df["ts"]] == ["02 04", "02 12", "02 20", "03 04"]
    validate_funding(df)
    assert df.index.tolist() == list(range(4))


def test_load_funding_errors_and_empty(tmp_path):
    with pytest.raises(FileNotFoundError):
        bf.load_funding("XBTUSD", "2021-01-01", "2021-01-02", tmp_path)
    bf.write_funding(grid_frame("2021-01-01", "2021-01-02"), "XBTUSD", tmp_path)
    with pytest.raises(ValueError):
        bf.load_funding("XBTUSD", "2021-01-02", "2021-01-01", tmp_path)
    empty = bf.load_funding("XBTUSD", "2022-01-01", "2022-02-01", tmp_path)
    assert len(empty) == 0
    assert dict(empty.dtypes) == dict(empty_frame(FUNDING).dtypes)


# 9. CLI ---------------------------------------------------------------------

def _patch(monkeypatch, sess):
    monkeypatch.setattr(bf, "_new_session", lambda: sess)
    sl = SleepLog()
    monkeypatch.setattr(bf, "_sleep", sl)
    return sl


def test_default_dir():
    assert bf.DEFAULT_FUNDING_DIR == Path("data/raw/normalized/bitmex/funding")
    assert bf.funding_path(bf.DEFAULT_FUNDING_DIR, "XBTUSD") == Path(
        "data/raw/normalized/bitmex/funding/XBTUSD.parquet")


def test_cli_ok(monkeypatch, tmp_path):
    sl = _patch(monkeypatch, FakeSession(make_records(1200, pd.Timestamp("2018-03-01 04:00", tz="UTC"))))
    rc = bf.main(["--symbol", "XBTUSD", "--start", "2018-03-01", "--end", "2018-04-01",
                  "--out", str(tmp_path)])
    assert rc == 0
    df = pd.read_parquet(tmp_path / "XBTUSD.parquet")
    assert len(df) == 31 * 3
    assert sl == []  # 한 페이지(< 500)라 요청 간 지연 없음


def test_cli_gap_exit_3(monkeypatch, tmp_path):
    recs = make_records(93)
    del recs[10]
    _patch(monkeypatch, FakeSession(recs))
    rc = bf.main(["--start", "2018-03-01", "--end", "2018-04-01", "--out", str(tmp_path)])
    assert rc == 3
    assert len(pd.read_parquet(tmp_path / "XBTUSD.parquet")) == 92


def test_cli_429_exit_1_file_unchanged(monkeypatch, tmp_path):
    bf.write_funding(grid_frame("2018-03-01", "2018-03-05"), "XBTUSD", tmp_path)
    before = (tmp_path / "XBTUSD.parquet").read_bytes()
    _patch(monkeypatch, FakeSession(make_records(93), [FakeResp(429)] * 4))
    rc = bf.main(["--start", "2018-03-01", "--end", "2018-04-01", "--out", str(tmp_path)])
    assert rc == 1
    assert (tmp_path / "XBTUSD.parquet").read_bytes() == before


def test_cli_connection_error_exit_1(monkeypatch, tmp_path):
    class Boom:
        def get(self, *a, **k):
            raise requests.ConnectionError("down")
    _patch(monkeypatch, Boom())
    rc = bf.main(["--start", "2018-03-01", "--end", "2018-04-01", "--out", str(tmp_path)])
    assert rc == 1
    assert not (tmp_path / "XBTUSD.parquet").exists()


def test_cli_arg_errors_exit_2(monkeypatch, tmp_path):
    _patch(monkeypatch, FakeSession([]))
    assert bf.main(["--start", "2018-04-01", "--end", "2018-04-01", "--out", str(tmp_path)]) == 2
    assert bf.main(["--start", "2018-04-02", "--end", "2018-04-01", "--out", str(tmp_path)]) == 2
    with pytest.raises(SystemExit) as e:
        bf.main(["--start", "2018/04/01", "--end", "2018-04-02"])
    assert e.value.code == 2
