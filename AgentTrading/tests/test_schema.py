"""shared.schema — 설계 문서(phase1-ingest-schema) 표와 정의 일치, 위반 유형별 검증."""

import numpy as np
import pandas as pd
import pytest

from src.shared import schema as sc
from src.shared.schema import SchemaError

U, S, I, F = "ts", "string", "int64", "float64"

# 문서 표에서 옮긴 기대값: (컬럼, dtype, nullable)
DOC_TRADES = [
    ("ts", U, False), ("symbol", S, False), ("side", S, False), ("size", I, False),
    ("price", F, False), ("tick_direction", S, False), ("trd_match_id", S, False),
    ("gross_value", I, False), ("home_notional", F, False), ("foreign_notional", F, False),
    ("trd_type", S, True), ("pool", S, True),
]
DOC_BARS = [
    ("ts", U, False), ("symbol", S, False), ("open", F, False), ("high", F, False),
    ("low", F, False), ("close", F, False), ("volume", I, False), ("volume_xbt", F, False),
    ("trade_count", I, False), ("buy_volume", I, False), ("sell_volume", I, False),
]
DOC_FILLS = [
    ("ts", U, False), ("symbol", S, False), ("side", S, False), ("qty", I, False),
    ("price", F, False), ("leverage", F, True), ("fee", F, True), ("fee_currency", S, True),
    ("source", S, False), ("source_id", S, True), ("strategy_id", S, True),
]


@pytest.mark.parametrize("schema,doc,key", [
    (sc.TRADES, DOC_TRADES, ("trd_match_id",)),
    (sc.BARS_1M, DOC_BARS, ("symbol", "ts")),
    (sc.FILLS, DOC_FILLS, ("source", "source_id")),
])
def test_definition_matches_doc(schema, doc, key):
    got = [(c.name, "ts" if c.dtype is sc.UTC_NS else c.dtype, c.nullable) for c in schema.columns]
    assert got == doc
    assert schema.key == key
    assert schema.column_names == [d[0] for d in doc]


def test_allowed_values_and_counts():
    assert len(sc.TRADES.columns) == 12 and len(sc.BARS_1M.columns) == 11
    assert len(sc.FILLS.columns) == 11
    assert sc.SIDES == {"buy", "sell"} and sc.SOURCES == {"aoa", "synthetic"}
    assert sc.TRADES.dtypes["side"] == "string"
    assert sc.UTC_NS == pd.DatetimeTZDtype("ns", "UTC")


def ts(*values):
    return pd.Series(pd.to_datetime(list(values), utc=True, format="ISO8601")).astype(sc.UTC_NS)


def trades():
    return pd.DataFrame({
        "ts": ts("2020-03-12T00:00:26.142291001Z", "2020-03-12T00:00:26.142291001Z",
                 "2020-03-12T00:00:27.000000000Z"),
        "symbol": pd.array(["XBTUSD"] * 3, dtype="string"),
        "side": pd.array(["buy", "sell", "buy"], dtype="string"),
        "size": np.array([100, 200, 300], dtype="int64"),
        "price": [5000.0, 5000.5, 5001.0],
        "tick_direction": pd.array(["PlusTick", "MinusTick", "PlusTick"], dtype="string"),
        "trd_match_id": pd.array(["a", "b", "c"], dtype="string"),
        "gross_value": np.array([2000000, 3999600, 5998800], dtype="int64"),
        "home_notional": [0.02, 0.039996, 0.059988],
        "foreign_notional": [100.0, 200.0, 300.0],
        "trd_type": pd.array([pd.NA, "Regular", pd.NA], dtype="string"),
        "pool": pd.array([pd.NA, pd.NA, pd.NA], dtype="string"),
    })


def bars():
    return pd.DataFrame({
        "ts": ts("2020-03-12T00:00:00Z", "2020-03-12T00:01:00Z"),
        "symbol": pd.array(["XBTUSD"] * 2, dtype="string"),
        "open": [5000.0, 5001.0], "high": [5001.0, 5001.0],
        "low": [5000.0, 5001.0], "close": [5001.0, 5001.0],
        "volume": np.array([600, 0], dtype="int64"),
        "volume_xbt": [0.12, 0.0],
        "trade_count": np.array([3, 0], dtype="int64"),
        "buy_volume": np.array([400, 0], dtype="int64"),
        "sell_volume": np.array([200, 0], dtype="int64"),
    })


def fills():
    return pd.DataFrame({
        "ts": ts("2020-03-12T00:00:26.000000001Z", "2020-03-12T00:05:00Z",
                 "2020-03-12T00:06:00Z", "2020-03-12T00:07:00Z"),
        "symbol": pd.array(["XBTUSD"] * 4, dtype="string"),
        "side": pd.array(["buy", "sell", "buy", "sell"], dtype="string"),
        "qty": np.array([100, 100, 50, 50], dtype="int64"),
        "price": [5000.0, 5100.0, 5050.0, 5060.0],
        "leverage": [10.0, np.nan, np.nan, np.nan],
        "fee": [0.000015, -0.0000025, np.nan, 0.00001],
        "fee_currency": pd.array(["XBT", "XBT", pd.NA, "XBT"], dtype="string"),
        "source": pd.array(["synthetic", "synthetic", "aoa", "aoa"], dtype="string"),
        "source_id": pd.array(["run1-1", "run1-2", pd.NA, pd.NA], dtype="string"),
        "strategy_id": pd.array(["s-v1", "s-v1", pd.NA, pd.NA], dtype="string"),
    })


@pytest.mark.parametrize("make,fn", [
    (trades, sc.validate_trades), (bars, sc.validate_bars_1m), (fills, sc.validate_fills),
])
def test_valid_frames_pass_and_return_same_object(make, fn):
    df = make()
    assert fn(df) is df


def _set(col, value):
    def mutate(df):
        df[col] = value
        return df
    return mutate


def _drop(col):
    return lambda df: df.drop(columns=[col])


TRADE_VIOLATIONS = {
    "missing": (_drop("price"), "누락 컬럼"),
    "extra": (_set("extra", 1), "예상 밖 컬럼"),
    "size_float": (lambda df: df.assign(size=df["size"].astype("float64")), "'size' dtype"),
    "size_nullable_int": (lambda df: df.assign(size=df["size"].astype("Int64")), "nullable 확장"),
    "symbol_object": (lambda df: df.assign(symbol=df["symbol"].astype(object)), "'symbol' dtype"),
    "price_int": (lambda df: df.assign(price=np.array([5000, 5000, 5001], dtype="int64")),
                  "'price' dtype"),
    "ts_naive": (lambda df: df.assign(ts=df["ts"].dt.tz_localize(None)), "UTC 아님"),
    "ts_seoul": (lambda df: df.assign(ts=df["ts"].dt.tz_convert("Asia/Seoul")), "UTC 아님"),
    "ts_string": (lambda df: df.assign(ts=df["ts"].astype(str)), "시각 dtype 아님"),
    "price_nan": (lambda df: df.assign(price=[5000.0, np.nan, 5001.0]), "'price' 결측"),
    "ts_nat": (lambda df: df.assign(ts=df["ts"].where(df.index != 1)), "'ts' 결측"),
    "side_capital": (lambda df: df.assign(side=pd.array(["Buy", "sell", "buy"], dtype="string")),
                     "허용값 위반"),
    "dup_match_id": (lambda df: df.assign(trd_match_id=pd.array(["a", "a", "c"], dtype="string")),
                     "중복 키"),
}


@pytest.mark.parametrize("name", list(TRADE_VIOLATIONS))
def test_trades_violations(name):
    mutate, pattern = TRADE_VIOLATIONS[name]
    with pytest.raises(SchemaError, match=pattern):
        sc.validate_trades(mutate(trades()))


def test_ts_microsecond_unit_rejected():
    df = trades()
    df["ts"] = df["ts"].astype("datetime64[us, UTC]")
    assert df["ts"].dtype.unit == "us"  # pandas 가 실제로 단위를 바꿨는지 선행 확인
    with pytest.raises(SchemaError, match="ns 아님"):
        sc.validate_trades(df)


def test_utc_variants_accepted():
    import datetime as dt
    df = trades()
    df["ts"] = df["ts"].dt.tz_convert(dt.timezone.utc)
    sc.validate_trades(df)


def test_non_strict_allows_extra_columns():
    df = trades().assign(extra=1)
    assert sc.validate(df, sc.TRADES, strict=False) is df


def test_bars_duplicate_key():
    df = bars()
    df["ts"] = ts("2020-03-12T00:00:00Z", "2020-03-12T00:00:00Z")
    with pytest.raises(SchemaError, match="중복 키"):
        sc.validate_bars_1m(df)
    # 심볼이 다르면 같은 ts 여도 키가 다르다
    df["symbol"] = pd.array(["XBTUSD", "ETHUSD"], dtype="string")
    sc.validate_bars_1m(df)


def test_fills_bad_source():
    df = fills()
    df["source"] = pd.array(["synthetic", "synthetic", "other", "aoa"], dtype="string")
    with pytest.raises(SchemaError, match="허용값 위반"):
        sc.validate_fills(df)


def test_fills_synthetic_duplicate_key():
    df = fills()
    df["source_id"] = pd.array(["run1-1", "run1-1", pd.NA, pd.NA], dtype="string")
    with pytest.raises(SchemaError, match="중복 키"):
        sc.validate_fills(df)


def test_fills_synthetic_requires_source_id():
    df = fills()
    df["source_id"] = pd.array(["run1-1", pd.NA, pd.NA, pd.NA], dtype="string")
    with pytest.raises(SchemaError, match="키 .* 결측"):
        sc.validate_fills(df)


def test_fills_aoa_rows_exempt_from_key():
    df = fills()
    df["source_id"] = pd.array(["run1-1", "run1-2", "x", "x"], dtype="string")
    sc.validate_fills(df)  # aoa 중복 source_id 허용
    df["source_id"] = pd.array(["run1-1", "run1-2", pd.NA, pd.NA], dtype="string")
    sc.validate_fills(df)  # aoa 결측 source_id 허용


@pytest.mark.parametrize("schema", [sc.TRADES, sc.BARS_1M, sc.FILLS])
def test_empty_frame(schema):
    df = sc.empty_frame(schema)
    assert len(df) == 0
    assert list(df.columns) == schema.column_names
    assert sc.validate(df, schema) is df


@pytest.mark.parametrize("make,fn", [(trades, sc.validate_trades), (fills, sc.validate_fills),
                                     (bars, sc.validate_bars_1m)])
def test_parquet_roundtrip(tmp_path, make, fn):
    df = make()
    path = tmp_path / "x.parquet"
    df.to_parquet(path, engine="pyarrow", index=False)
    back = pd.read_parquet(path, engine="pyarrow")
    fn(back)
    assert back["ts"].equals(df["ts"])
    assert back["ts"].iloc[0].nanosecond == df["ts"].iloc[0].nanosecond
    pd.testing.assert_frame_equal(back, df, check_dtype=False)


# phase2-synthetic-strategy.md "라운드트립 스키마" 표에서 옮긴 기대값
B = "bool"
DOC_ROUNDTRIPS = [
    ("strategy_id", S, False), ("param_id", S, False), ("trade_id", I, False),
    ("symbol", S, False), ("side", S, False), ("signal_ts", U, False), ("entry_ts", U, False),
    ("entry_price", F, False), ("exit_signal_ts", U, True), ("exit_ts", U, False),
    ("exit_price", F, False), ("qty", I, False), ("stop_price", F, False), ("tp_price", F, True),
    ("entry_reason", S, False), ("exit_reason", S, False), ("holding_min", F, False),
    ("equity_before", F, False), ("notional_usd", F, False), ("leverage", F, False),
    ("risk_pct", F, False), ("size_capped", B, False), ("gross_pnl_xbt", F, False),
    ("gross_ret", F, False),
]


def test_roundtrips_definition_matches_doc():
    got = [(c.name, "ts" if c.dtype is sc.UTC_NS else c.dtype, c.nullable)
           for c in sc.ROUNDTRIPS.columns]
    assert got == DOC_ROUNDTRIPS
    assert len(sc.ROUNDTRIPS.columns) == 24
    assert sc.ROUNDTRIPS.key == ("strategy_id", "param_id", "trade_id")
    allowed = {c.name: c.allowed for c in sc.ROUNDTRIPS.columns if c.allowed is not None}
    assert allowed == {
        "side": {"long", "short"},
        "entry_reason": {"h1_breakout", "h2_momentum", "h3_meanrev"},
        "exit_reason": {"stop", "take_profit", "time", "end_of_data"},
    }


def test_roundtrips_bool_dtype():
    empty = sc.empty_frame(sc.ROUNDTRIPS)
    assert empty["size_capped"].dtype == np.dtype("bool")
    assert sc.validate_roundtrips(empty) is empty
    nullable_bool = empty.assign(size_capped=pd.array([], dtype="boolean"))
    with pytest.raises(SchemaError, match="size_capped"):
        sc.validate_roundtrips(nullable_bool)
    as_object = empty.assign(size_capped=pd.Series([], dtype=object))
    with pytest.raises(SchemaError, match="size_capped"):
        sc.validate_roundtrips(as_object)


# phase3-backtest.md "net 열" 표에서 옮긴 기대값
DOC_ROUNDTRIPS_NET_EXTRA = [
    ("entry_liquidity", S, False), ("exit_liquidity", S, False),
    ("entry_fill_price", F, False), ("exit_fill_price", F, False), ("fee_xbt", F, False),
    ("slippage_xbt", F, False), ("net_pnl_xbt", F, False), ("net_ret", F, False),
]


def test_roundtrips_net_definition_matches_doc():
    got = [(c.name, "ts" if c.dtype is sc.UTC_NS else c.dtype, c.nullable)
           for c in sc.ROUNDTRIPS_NET.columns]
    assert got == DOC_ROUNDTRIPS + DOC_ROUNDTRIPS_NET_EXTRA
    assert len(sc.ROUNDTRIPS_NET.columns) == 32
    assert sc.ROUNDTRIPS_NET.key == ("strategy_id", "param_id", "trade_id")
    assert sc.LIQUIDITY == {"maker", "taker"}
    allowed = {c.name: c.allowed for c in sc.ROUNDTRIPS_NET.columns[24:] if c.allowed is not None}
    assert allowed == {"entry_liquidity": {"maker", "taker"}, "exit_liquidity": {"maker", "taker"}}


def test_roundtrips_net_empty_and_violations():
    empty = sc.empty_frame(sc.ROUNDTRIPS_NET)
    assert sc.validate_roundtrips_net(empty) is empty
    with pytest.raises(SchemaError, match="예상 밖"):
        sc.validate_roundtrips(empty)
    assert sc.validate_roundtrips(empty, strict=False) is empty
    with pytest.raises(SchemaError, match="누락"):
        sc.validate_roundtrips_net(sc.empty_frame(sc.ROUNDTRIPS))

    from tests.test_backtest_costs import EX_A, make_rt
    from src.backtest.costs import apply_costs
    net = apply_costs(make_rt([EX_A]), "default")
    bad = net.copy()
    bad.loc[0, "fee_xbt"] = np.nan
    with pytest.raises(SchemaError, match="fee_xbt"):
        sc.validate_roundtrips_net(bad)
    bad = net.copy()
    bad["exit_liquidity"] = pd.array(["post_only"], dtype="string")
    with pytest.raises(SchemaError, match="허용값"):
        sc.validate_roundtrips_net(bad)
