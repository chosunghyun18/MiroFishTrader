# ETF & 분석 지표 용어 사전

> 프로젝트에서 사용하는 도메인 용어 정의.
> 새 지표나 전략 추가 시 여기에 먼저 정의할 것.

---

## ETF 기본 용어

| 용어 | 정의 |
|------|------|
| ETF (Exchange Traded Fund) | 거래소에 상장된 펀드. 주식처럼 거래 가능 |
| NAV (Net Asset Value) | 순자산가치. ETF의 실제 보유 자산 기준 가격 |
| AUM (Assets Under Management) | 운용 자산 규모 |
| Expense Ratio | 연간 운용 비용 비율 (낮을수록 유리) |
| Tracking Error | ETF가 추종 지수 대비 얼마나 벗어나는지 |

---

## 투자 기간 분류

이 프로젝트의 신호 기준:

| 기간 | 정의 |
|------|------|
| 단기 | 1~2주 |
| 중기 | 2주~2개월 |
| 장기 | 2개월 초과 (이 프로젝트 범위 밖) |

---

## 기술적 분석 지표 (최소 사용)

| 지표 | 용도 | 사용 여부 |
|------|------|-----------|
| RSI (Relative Strength Index) | 과매수/과매도 판단 | 선택적 |
| SMA/EMA (이동평균) | 추세 확인 | 선택적 |
| Volume Profile | 거래량 분포 | 선택적 |

> **원칙**: 기술적 분석은 매크로/펀더멘털 신호를 보조하는 용도로만 사용.

---

## 매크로 지표

| 지표 | 소스 | 용도 |
|------|------|------|
| Federal Funds Rate | FRED | 금리 환경 판단 |
| CPI (Consumer Price Index) | FRED | 인플레이션 측정 |
| VIX | Yahoo Finance | 시장 변동성/공포 지수 |
| Dollar Index (DXY) | Yahoo Finance | 달러 강세/약세 |
