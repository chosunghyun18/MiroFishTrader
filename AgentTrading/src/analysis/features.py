"""1분봉 `bars_1m` → 합성 전략 진입 트리거용 지표.

입력: 한 심볼의 1분봉(`src.shared.schema.BARS_1M`, validate_bars_1m 통과), `ts` 오름차순·정확히 1분 연속.
출력: 입력과 같은 인덱스·행 수의 DataFrame(`ts` + float64 지표 열).
설계 근거(단일 기준): Obsidian `Projects/work/AgentTrading/design/phase2-synthetic-strategy.md`
"진입 트리거 가설 (H)" 지표 표, "미래 정보 누수 방지" 1·5·8.

    from src.analysis.features import compute_features
    feats = compute_features(bars)                    # 기본 창 (15, 60, 240)
    feats = compute_features(bars, windows=(60,))

| 지표 | 정의 | 워밍업(NaN) |
|---|---|---|
| donchian_high_N / donchian_low_N | high.shift(1).rolling(N).max() / low.shift(1).rolling(N).min() | 첫 N 행 |
| r1 | log(close[t] / close[t-1]) | 첫 1 행 |
| mom_N | log(close[t] / close[t-N]) | 첫 N 행 |
| sigma_N | r1.rolling(N).std(ddof=1) × sqrt(N) | 첫 N 행 |
| sma_N / sd_N | close.rolling(N).mean() / close.rolling(N).std(ddof=1) | 첫 N−1 행 |
| z_N | (close − sma_N) / sd_N, sd_N == 0 이면 NaN | 첫 N−1 행 |

- 창 N 은 행 수(= 분). 롤링은 모두 `min_periods=N` 이라 부분 창 값은 내지 않는다.
- 봉 t 의 값은 봉 t 마감까지의 데이터만 쓴다. Donchian 만 현재 봉을 제외(shift(1)).
  `center=True`·음수 shift·전체 구간 정규화는 쓰지 않는다.
- ATR 은 설계 문서가 제외했으므로 없다. 신호 판정(NaN·sigma == 0 처리)은 생성기 범위다.
"""

from __future__ import annotations

import math
import numbers

import numpy as np
import pandas as pd

from src.shared.schema import validate_bars_1m

DEFAULT_WINDOWS = (15, 60, 240)
ONE_MIN = pd.Timedelta(minutes=1)
_PER_WINDOW = ("donchian_high", "donchian_low", "mom", "sigma", "sma", "sd", "z")


def _check_window(n) -> int:
    if isinstance(n, bool) or not isinstance(n, numbers.Integral) or n < 2:
        raise ValueError(f"창 길이는 2 이상의 정수여야 함: {n!r}")
    return int(n)


def feature_columns(windows=DEFAULT_WINDOWS) -> list[str]:
    """compute_features 가 내는 지표 열 이름(`ts` 제외) 순서."""
    cols = ["r1"]
    for n in windows:
        n = _check_window(n)
        cols.extend(f"{name}_{n}" for name in _PER_WINDOW)
    return cols


def log_return(close: pd.Series) -> pd.Series:
    """r1 = log(close[t] / close[t-1])."""
    return np.log(close / close.shift(1))


def donchian_high(high: pd.Series, n: int) -> pd.Series:
    """직전 n 봉 최고가(현재 봉 제외)."""
    n = _check_window(n)
    return high.shift(1).rolling(n, min_periods=n).max()


def donchian_low(low: pd.Series, n: int) -> pd.Series:
    """직전 n 봉 최저가(현재 봉 제외)."""
    n = _check_window(n)
    return low.shift(1).rolling(n, min_periods=n).min()


def momentum(close: pd.Series, n: int) -> pd.Series:
    """mom_N = log(close[t] / close[t-n])."""
    n = _check_window(n)
    return np.log(close / close.shift(n))


def sigma(close: pd.Series, n: int) -> pd.Series:
    """sigma_N = r1 의 n 봉 표본표준편차 × sqrt(n)."""
    n = _check_window(n)
    return log_return(close).rolling(n, min_periods=n).std(ddof=1) * math.sqrt(n)


def rolling_mean(close: pd.Series, n: int) -> pd.Series:
    """sma_N = close 의 n 봉 평균(현재 봉 포함)."""
    n = _check_window(n)
    return close.rolling(n, min_periods=n).mean()


def rolling_std(close: pd.Series, n: int) -> pd.Series:
    """sd_N = close 의 n 봉 표본표준편차(현재 봉 포함)."""
    n = _check_window(n)
    return close.rolling(n, min_periods=n).std(ddof=1)


def zscore(close: pd.Series, n: int) -> pd.Series:
    """z_N = (close − sma_N) / sd_N. sd_N == 0 이면 NaN."""
    sd = rolling_std(close, n)
    return (close - rolling_mean(close, n)) / sd.where(sd != 0)


def _validate_input(bars: pd.DataFrame) -> None:
    validate_bars_1m(bars)
    symbols = bars["symbol"].unique()
    if len(symbols) > 1:
        raise ValueError(f"한 심볼만 받는다: {sorted(map(str, symbols))}")
    if len(bars) > 1:
        bad = bars["ts"].diff().iloc[1:] != ONE_MIN
        if bad.any():
            i = int(np.flatnonzero(bad.to_numpy())[0]) + 1
            prev, cur = bars["ts"].iloc[i - 1], bars["ts"].iloc[i]
            raise ValueError(f"ts 가 1분 연속 오름차순이 아님: {prev} → {cur} (행 {i})")


def compute_features(bars: pd.DataFrame, windows=DEFAULT_WINDOWS) -> pd.DataFrame:
    """한 심볼 1분봉에서 설계 문서 지표 표의 지표를 계산한다. 입력은 바꾸지 않는다.

    windows: 창 길이(행 수) 묶음. 각 값은 2 이상 정수. 기본값은 설계 문서 그리드 n.
    """
    windows = tuple(_check_window(n) for n in windows)
    if len(set(windows)) != len(windows):
        raise ValueError(f"창 길이 중복: {windows}")
    _validate_input(bars)

    close = bars["close"].astype("float64")
    high = bars["high"].astype("float64")
    low = bars["low"].astype("float64")

    out = {"ts": bars["ts"].copy(), "r1": log_return(close)}
    for n in windows:
        out[f"donchian_high_{n}"] = donchian_high(high, n)
        out[f"donchian_low_{n}"] = donchian_low(low, n)
        out[f"mom_{n}"] = momentum(close, n)
        out[f"sigma_{n}"] = sigma(close, n)
        out[f"sma_{n}"] = rolling_mean(close, n)
        out[f"sd_{n}"] = rolling_std(close, n)
        out[f"z_{n}"] = zscore(close, n)
    result = pd.DataFrame(out, index=bars.index)
    cols = feature_columns(windows)
    result[cols] = result[cols].astype("float64")
    return result[["ts", *cols]]
