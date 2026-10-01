"""analysis.features — 지표를 손 계산 기대값과 비교하고 미래 정보 누수가 없음을 확인."""

import math

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src.analysis.features import (
    DEFAULT_WINDOWS,
    compute_features,
    donchian_high,
    feature_columns,
)
from src.shared.schema import BARS_1M, SchemaError, empty_frame

T0 = pd.Timestamp("2020-03-12 00:00", tz="UTC")
NAN = float("nan")

# 손 계산용 짧은 봉(N=3). high/low 는 close 와 독립적으로 정해 Donchian 을 따로 확인한다.
CLOSE = [100.0, 102.0, 101.0, 104.0, 103.0, 107.0, 105.0, 106.0, 110.0]
HIGH = [101.0, 103.0, 102.0, 105.0, 104.0, 120.0, 106.0, 107.0, 111.0]
LOW = [99.0, 100.0, 99.5, 101.0, 102.0, 103.0, 90.0, 104.0, 105.0]


def _bars(close, high=None, low=None, open_=None, symbol="XBTUSD", ts=None):
    n = len(close)
    close = [float(c) for c in close]
    high = close if high is None else high
    low = close if low is None else low
    open_ = close if open_ is None else open_
    if ts is None:
        ts = [T0 + pd.Timedelta(minutes=i) for i in range(n)]
    df = pd.DataFrame({
        "ts": pd.Series(ts, dtype="datetime64[ns, UTC]"),
        "symbol": [symbol] * n,
        "open": [float(x) for x in open_],
        "high": [float(x) for x in high],
        "low": [float(x) for x in low],
        "close": close,
        "volume": [10] * n,
        "volume_xbt": [0.1] * n,
        "trade_count": [1] * n,
        "buy_volume": [5] * n,
        "sell_volume": [5] * n,
    })
    return df.astype(BARS_1M.dtypes)


def _random_bars(n=300, seed=7):
    rng = np.random.default_rng(seed)
    close = 8000.0 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    spread = np.abs(rng.normal(0, 3.0, (2, n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + spread[0]
    low = np.minimum(open_, close) - spread[1]
    return _bars(close, high, low, open_)


def _std(xs):
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _assert_close_list(actual, expected):
    assert len(actual) == len(expected)
    for i, (a, e) in enumerate(zip(actual, expected)):
        if math.isnan(e):
            assert math.isnan(a), f"행 {i}: {a} != NaN"
        else:
            assert a == pytest.approx(e, rel=1e-12, abs=1e-15), f"행 {i}"


# --- 손 계산 기대값 -----------------------------------------------------------

def test_hand_computed_values_n3():
    n = 3
    f = compute_features(_bars(CLOSE, HIGH, LOW), windows=(n,))
    rows = range(len(CLOSE))

    r1 = [NAN] + [math.log(CLOSE[t] / CLOSE[t - 1]) for t in rows if t >= 1]
    _assert_close_list(f["r1"].tolist(), r1)

    dh = [max(HIGH[t - n:t]) if t >= n else NAN for t in rows]
    dl = [min(LOW[t - n:t]) if t >= n else NAN for t in rows]
    _assert_close_list(f["donchian_high_3"].tolist(), dh)
    _assert_close_list(f["donchian_low_3"].tolist(), dl)

    mom = [math.log(CLOSE[t] / CLOSE[t - n]) if t >= n else NAN for t in rows]
    _assert_close_list(f["mom_3"].tolist(), mom)

    sig = [_std(r1[t - n + 1:t + 1]) * math.sqrt(n) if t >= n else NAN for t in rows]
    _assert_close_list(f["sigma_3"].tolist(), sig)

    sma = [sum(CLOSE[t - n + 1:t + 1]) / n if t >= n - 1 else NAN for t in rows]
    sd = [_std(CLOSE[t - n + 1:t + 1]) if t >= n - 1 else NAN for t in rows]
    z = [(CLOSE[t] - sma[t]) / sd[t] if t >= n - 1 else NAN for t in rows]
    _assert_close_list(f["sma_3"].tolist(), sma)
    _assert_close_list(f["sd_3"].tolist(), sd)
    _assert_close_list(f["z_3"].tolist(), z)

    # 구체 값 몇 개를 숫자로 고정
    assert f["sma_3"].iloc[2] == pytest.approx(101.0)
    assert f["sd_3"].iloc[2] == pytest.approx(1.0)
    assert f["z_3"].iloc[2] == pytest.approx(0.0)
    assert f["mom_3"].iloc[3] == pytest.approx(math.log(1.04))


def test_donchian_excludes_current_bar():
    # 행 5 의 high 120 은 그 행의 기준선에 들어가지 않고, 다음 행부터 들어간다.
    f = compute_features(_bars(CLOSE, HIGH, LOW), windows=(3,))
    assert f["donchian_high_3"].iloc[5] == 105.0  # max(HIGH[2:5]) = max(102, 105, 104)
    assert f["donchian_high_3"].iloc[6] == 120.0
    # 행 6 의 low 90 도 마찬가지
    assert f["donchian_low_3"].iloc[6] == 101.0  # min(LOW[3:6]) = min(101, 102, 103)
    assert f["donchian_low_3"].iloc[7] == 90.0
    # 시리즈 함수 단독 호출도 같은 값
    s = donchian_high(pd.Series(HIGH), 3)
    assert s.iloc[5] == 105.0 and math.isnan(s.iloc[2])


# --- 워밍업 -------------------------------------------------------------------

def _leading_nans(s: pd.Series) -> int:
    valid = s.notna().to_numpy()
    return int(valid.argmax()) if valid.any() else len(s)


@pytest.mark.parametrize("n", [3, 15])
def test_warmup_nan_counts(n):
    f = compute_features(_random_bars(60), windows=(n,))
    expected = {
        "r1": 1,
        f"donchian_high_{n}": n, f"donchian_low_{n}": n,
        f"mom_{n}": n, f"sigma_{n}": n,
        f"sma_{n}": n - 1, f"sd_{n}": n - 1, f"z_{n}": n - 1,
    }
    for col, k in expected.items():
        assert _leading_nans(f[col]) == k, col
        assert f[col].iloc[k:].notna().all(), col


def test_single_row_all_window_features_nan():
    f = compute_features(_bars([100.0]), windows=(3,))
    assert len(f) == 1
    assert f[feature_columns((3,))].isna().all(axis=None)


# --- 평탄 구간 ----------------------------------------------------------------

def test_flat_segment_sd_zero_gives_z_nan():
    close = [100.0, 101.0] + [101.0] * 6 + [102.0]
    f = compute_features(_bars(close), windows=(3,))
    flat_rows = range(3, 8)  # 행 3..7 의 창 [t-2..t] 은 모두 101
    for t in flat_rows:
        assert f["sd_3"].iloc[t] == 0.0
        assert math.isnan(f["z_3"].iloc[t])
    # r1 창 [t-2..t] 이 모두 0 인 행은 sigma 가 정확히 0
    for t in range(4, 8):
        assert f["sigma_3"].iloc[t] == 0.0
    # 평탄 구간이 끝나면 z 는 다시 값이 있다
    assert f["sd_3"].iloc[8] > 0 and not math.isnan(f["z_3"].iloc[8])


# --- 미래 정보 누수 -----------------------------------------------------------

@pytest.mark.parametrize("windows", [(3,), DEFAULT_WINDOWS])
def test_no_lookahead_prefix_equals_full(windows):
    bars = _random_bars(300)
    full = compute_features(bars, windows=windows)
    for t in (0, 1, 2, 3, 14, 15, 59, 60, 61, 150, 239, 240, 241, 299):
        part = compute_features(bars.iloc[:t + 1], windows=windows)
        pdt.assert_frame_equal(part.iloc[[t]], full.iloc[[t]], check_exact=True)


@pytest.mark.parametrize("t", [5, 100, 240, 260])
def test_no_lookahead_future_perturbation(t):
    bars = _random_bars(300)
    full = compute_features(bars)

    bumped = bars.copy()
    future = bumped.index > t
    for col in ("open", "high", "low", "close"):
        bumped.loc[future, col] = bumped.loc[future, col] * 2.0
    # 급락 봉도 하나 섞는다
    crash = t + 2
    bumped.loc[crash, ["open", "high", "low", "close"]] = [100.0, 120.0, 50.0, 60.0]
    pert = compute_features(bumped)

    pdt.assert_frame_equal(pert.iloc[:t + 1], full.iloc[:t + 1], check_exact=True)
    # 테스트가 실제로 민감한지: 미래 구간은 달라져야 한다
    after_p = pert.iloc[t + 1:].drop(columns="ts")
    after_f = full.iloc[t + 1:].drop(columns="ts")
    diff = ~((after_p == after_f) | (after_p.isna() & after_f.isna()))
    assert diff.any(axis=None)


# --- 입력 검증 ----------------------------------------------------------------

def test_rejects_multiple_symbols():
    a = _bars([1.0, 2.0])
    b = _bars([1.0, 2.0], symbol="ETHUSD")
    with pytest.raises(ValueError, match="한 심볼"):
        compute_features(pd.concat([a, b], ignore_index=True), windows=(3,))


@pytest.mark.parametrize("offsets", [
    [0, 1, 3, 4],      # 구멍
    [0, 2, 1, 3],      # 역순
    [0, 1, 1, 2],      # 같은 시각(심볼 1개면 스키마 키 중복으로도 걸림)
])
def test_rejects_non_contiguous_ts(offsets):
    ts = [T0 + pd.Timedelta(minutes=m) for m in offsets]
    bars = _bars([1.0, 2.0, 3.0, 4.0], ts=ts)
    with pytest.raises(ValueError):
        compute_features(bars, windows=(3,))


def test_rejects_sub_minute_step():
    ts = [T0 + pd.Timedelta(seconds=30 * i) for i in range(4)]
    with pytest.raises(ValueError, match="1분 연속"):
        compute_features(_bars([1.0, 2.0, 3.0, 4.0], ts=ts), windows=(3,))


@pytest.mark.parametrize("bad", [1, 0, -3, 2.5, 3.0, "3", True, None])
def test_rejects_bad_window(bad):
    with pytest.raises(ValueError, match="창 길이"):
        compute_features(_bars([1.0, 2.0, 3.0]), windows=(bad,))


def test_rejects_duplicate_windows():
    with pytest.raises(ValueError, match="중복"):
        compute_features(_bars([1.0, 2.0, 3.0]), windows=(3, 3))


def test_rejects_schema_violation():
    bars = _bars([1.0, 2.0, 3.0]).drop(columns="volume")
    with pytest.raises(SchemaError):
        compute_features(bars, windows=(3,))


def test_input_not_mutated():
    bars = _random_bars(50)
    before = bars.copy(deep=True)
    compute_features(bars)
    pdt.assert_frame_equal(bars, before, check_exact=True)


def test_empty_input():
    f = compute_features(empty_frame(BARS_1M))
    assert len(f) == 0
    assert list(f.columns) == ["ts", *feature_columns(DEFAULT_WINDOWS)]


# --- 출력 형태 ----------------------------------------------------------------

def test_output_shape_and_dtypes():
    bars = _random_bars(300)
    bars.index = bars.index + 1000  # 0 시작이 아닌 인덱스도 보존
    f = compute_features(bars)
    cols = feature_columns(DEFAULT_WINDOWS)
    assert list(f.columns) == ["ts", *cols]
    assert len(cols) == 1 + 7 * len(DEFAULT_WINDOWS)
    assert all(f[c].dtype == np.float64 for c in cols)
    pdt.assert_index_equal(f.index, bars.index)
    pdt.assert_series_equal(f["ts"], bars["ts"])
