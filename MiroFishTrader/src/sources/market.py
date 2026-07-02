"""EOD 시세(ETF/지수) + FRED 매크로 지표 조회.

비용 최소화: 무료 티어만 사용, 신규 필수 의존성 없음(requests만 사용).
시세는 Stooq(키 불필요) → AlphaVantage → Finnhub 순으로 폴백하는 체인으로 가져오고,
매크로 지표는 FRED에서 가져온다. 네트워크/파싱 실패 시 None/빈 리스트를 돌려
시드 생성이 degrade 하도록 한다. (news.py/polymarket.py와 동일한 DI 패턴:
Protocol 클라이언트 주입 → 테스트 시 HTTP 없이.)
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple

import requests

logger = logging.getLogger(__name__)

STOOQ_BASE = "https://stooq.com/q/l/"
ALPHA_VANTAGE_BASE = "https://www.alphavantage.co/query"
FINNHUB_BASE = "https://finnhub.io/api/v1/quote"
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# 반도체/AI/금리 중심 워치리스트 (v1 최소 세트).
DEFAULT_SYMBOLS: List[str] = ["spy.us", "qqq.us", "soxx.us"]
DEFAULT_MACRO_SERIES: List[Tuple[str, str]] = [
    ("DFF", "Fed Funds Rate"),
    ("CPIAUCSL", "CPI"),
]


@dataclass
class Quote:
    symbol: str
    price: float
    day_change_pct: Optional[float]
    asof: str  # 날짜 문자열, 소스마다 포맷이 다를 수 있음


@dataclass
class MacroIndicator:
    series_id: str
    name: str
    value: float
    asof: str


def _opt_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 시세 (Stooq 우선 → AlphaVantage → Finnhub)
# ---------------------------------------------------------------------------


def parse_stooq_csv(text: str) -> Optional[Quote]:
    """Stooq CSV(`f=sd2ohlcv`) 한 줄을 Quote로 파싱. 실패/데이터없음 시 None.

    헤더 예: Symbol,Date,Open,High,Low,Close,Volume
    존재하지 않는 심볼이면 Stooq가 값 자리에 "N/D"를 채워 돌려준다.
    """
    try:
        reader = csv.DictReader(io.StringIO(text))
        row = next(reader, None)
    except csv.Error as exc:
        logger.warning("Stooq CSV 파싱 실패: %s", exc)
        return None
    if not row:
        return None
    close = _opt_float(row.get("Close"))
    if close is None:
        return None
    open_ = _opt_float(row.get("Open"))
    day_change_pct: Optional[float] = None
    if open_ is not None and open_ != 0:
        day_change_pct = (close - open_) / open_ * 100
    symbol = str(row.get("Symbol", "")).strip()
    asof = str(row.get("Date", "")).strip()
    return Quote(symbol=symbol, price=close, day_change_pct=day_change_pct, asof=asof)


class SupportsFetchQuote(Protocol):
    def fetch_quote(self, symbol: str) -> Optional[Quote]: ...


class StooqClient:
    """Stooq 무료 CSV 시세. 키 불필요. 미국 티커는 소문자 + `.us` 접미사(예: spy.us)."""

    def __init__(
        self,
        base_url: str = STOOQ_BASE,
        timeout: int = 30,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._session = session or requests.Session()

    def fetch_quote(self, symbol: str) -> Optional[Quote]:
        params = {"s": symbol, "f": "sd2ohlcv", "h": "", "e": "csv"}
        try:
            resp = self._session.get(self.base_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return parse_stooq_csv(resp.text)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Stooq 시세 조회 실패(%s): %s", symbol, exc)
            return None


class AlphaVantageClient:
    """AlphaVantage GLOBAL_QUOTE. 무료 티어지만 API 키 필요(없으면 항상 None)."""

    def __init__(
        self,
        api_key: Optional[str],
        base_url: str = ALPHA_VANTAGE_BASE,
        timeout: int = 30,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._session = session or requests.Session()

    def fetch_quote(self, symbol: str) -> Optional[Quote]:
        if not self.api_key:
            return None
        params = {
            "function": "GLOBAL_QUOTE",
            "symbol": symbol,
            "apikey": self.api_key,
        }
        try:
            resp = self._session.get(self.base_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("AlphaVantage 시세 조회 실패(%s): %s", symbol, exc)
            return None
        quote = data.get("Global Quote") if isinstance(data, dict) else None
        if not isinstance(quote, dict) or not quote:
            return None
        price = _opt_float(quote.get("05. price"))
        if price is None:
            return None
        change_pct_raw = str(quote.get("10. change percent", "")).strip().rstrip("%")
        return Quote(
            symbol=symbol,
            price=price,
            day_change_pct=_opt_float(change_pct_raw),
            asof=str(quote.get("07. latest trading day", "")).strip(),
        )


class FinnhubClient:
    """Finnhub /quote. 무료 티어지만 API 키 필요(없으면 항상 None)."""

    def __init__(
        self,
        api_key: Optional[str],
        base_url: str = FINNHUB_BASE,
        timeout: int = 30,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._session = session or requests.Session()

    def fetch_quote(self, symbol: str) -> Optional[Quote]:
        if not self.api_key:
            return None
        params = {"symbol": symbol, "token": self.api_key}
        try:
            resp = self._session.get(self.base_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Finnhub 시세 조회 실패(%s): %s", symbol, exc)
            return None
        if not isinstance(data, dict):
            return None
        price = _opt_float(data.get("c"))
        if not price:  # 0 또는 None → 데이터 없음
            return None
        ts = data.get("t")
        return Quote(
            symbol=symbol,
            price=price,
            day_change_pct=_opt_float(data.get("dp")),
            asof=str(ts) if ts is not None else "",
        )


def fetch_quotes(symbols: List[str], clients: List[SupportsFetchQuote]) -> List[Quote]:
    """심볼마다 클라이언트를 순서대로 시도해 첫 성공값을 사용(폴백 체인).

    모든 클라이언트가 실패하면 해당 심볼은 건너뛴다. 어떤 클라이언트가 예외를
    던지더라도(네트워크 오류 포함) 전체 흐름은 절대 중단되지 않는다(graceful).
    """
    out: List[Quote] = []
    for symbol in symbols:
        quote: Optional[Quote] = None
        for client in clients:
            try:
                quote = client.fetch_quote(symbol)
            except Exception as exc:  # noqa: BLE001 - 폴백 체인은 어떤 예외에도 죽지 않아야 함
                logger.warning("시세 클라이언트 예외(%s): %s", symbol, exc)
                quote = None
            if quote is not None:
                break
        if quote is not None:
            out.append(quote)
    return out


# ---------------------------------------------------------------------------
# FRED 매크로 지표
# ---------------------------------------------------------------------------


class SupportsFetchSeries(Protocol):
    def fetch_series(self, series_id: str) -> Dict[str, Any]: ...


class FredClient:
    """FRED(세인트루이스 연은) 시계열 최신 관측치. API 키 필요(무료 발급)."""

    def __init__(
        self,
        api_key: Optional[str],
        base_url: str = FRED_BASE,
        timeout: int = 30,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._session = session or requests.Session()

    def fetch_series(self, series_id: str) -> Dict[str, Any]:
        """observations 엔드포인트 원본 JSON(dict)을 그대로 반환. 키 없으면 빈 dict."""
        if not self.api_key:
            return {}
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        }
        resp = self._session.get(self.base_url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}


def parse_fred_observations(
    data: Dict[str, Any], series_id: str, name: str
) -> Optional[MacroIndicator]:
    """FRED observations 응답(JSON dict)을 최신 MacroIndicator로. 없으면 None."""
    observations = data.get("observations") if isinstance(data, dict) else None
    if not isinstance(observations, list) or not observations:
        return None
    latest = observations[0]
    if not isinstance(latest, dict):
        return None
    value = _opt_float(latest.get("value"))
    if value is None:  # FRED는 결측값을 "." 문자열로 표기
        return None
    return MacroIndicator(
        series_id=series_id,
        name=name,
        value=value,
        asof=str(latest.get("date", "")).strip(),
    )


def fetch_macro(
    series: List[Tuple[str, str]], client: SupportsFetchSeries
) -> List[MacroIndicator]:
    """(series_id, 표시이름) 목록을 조회. 실패한 항목은 건너뛴다(graceful)."""
    out: List[MacroIndicator] = []
    for series_id, name in series:
        try:
            raw = client.fetch_series(series_id)
        except Exception as exc:  # noqa: BLE001 - 어떤 예외에도 파이프라인을 죽이지 않음
            logger.warning("FRED 매크로 조회 실패(%s): %s", series_id, exc)
            raw = {}
        indicator = parse_fred_observations(raw, series_id, name)
        if indicator is not None:
            out.append(indicator)
    return out
