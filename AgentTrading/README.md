# AgentTrading

워뇨띠의 공개 거래 기록을 읽어 거래 방식을 정량화하고, **백테스트로 검증한 뒤 자율 트레이딩 에이전트로 돌리는** 시스템.

> 설계 단일 소스(SSOT): Obsidian `Projects/work/AgentTrading/`.
> 코드와 문서가 충돌하면 문서가 우선한다. (dash / MiroFishTrader 컨벤션)

## 목표

**1순위 — 워뇨띠 데이터를 읽고 트레이딩하는 에이전트.**

거기까지 가기 위한 순서:

1. 공개 거래 기록 확보 (수집·다운로드)
2. 진입/청산·포지션 사이징 등 거래 패턴 정량화
3. 백테스트 가능한 시스템 구축 ← 에이전트의 전제조건
4. 페이퍼 트레이딩 → 실거래 에이전트

3번을 통과하지 못한 전략은 4번으로 보내지 않는다.

## 구조

```
AgentTrading/
├── src/
│   ├── ingest/      공개 거래 기록 수집·정규화 → data/raw
│   ├── analysis/    거래 패턴 정량화 (진입·청산·보유시간·사이징·레버리지)
│   ├── backtest/    체결/수수료/슬리피지 포함 백테스트 엔진
│   ├── agent/       시그널 생성 + 주문 실행 (페이퍼 먼저)
│   └── shared/      공통 모델·설정·로깅
├── config/          설정 (실제 키는 .env, 커밋 금지)
├── data/raw/        원본 거래 기록 (커밋 금지)
└── tests/
```

## 상태

Phase 1(수집 파이프라인) 종료 2026-10-02 — 시장 데이터(BitMEX XBTUSD) 기준. BitMEX 공개 체결 다운로더(`src/ingest/bitmex_public.py`),
정규화 파서·1분봉 리샘플·증분 정규화 CLI(`src/ingest/normalize.py`)·구간 로더(`src/ingest/store.py`) 완료.
Phase 2(패턴 정량화) 종료 2026-10-02 — 합성 전략 기준. 1분봉 지표(`features`)·합성 전략 생성기(`synthetic`)·분포 지표(`patterns`)·분석 CLI(`run`),
실데이터 2구간(XBTUSD 2019-06-01~07·2020-03) 전체 그리드 분포 산출 완료.
Phase 3 진행 중 — 비용 모델(`costs`)·게이트 지표(`metrics`)·워크포워드(`walkforward`)·백테스트 CLI 기본 모드(`run`) 완료. 행동 표본은 합성 전략, aoa 원본은 진위 확인 후 조건부.

## 데이터 — BitMEX 공개 거래 장부

`https://s3-eu-west-1.amazonaws.com/public.bitmex.com/data/trade/` 의 일별 `YYYYMMDD.csv.gz`
(전 종목 체결, 총 ~50GB). **익명 테이프**라 개인 체결 추출은 불가 — 시장 데이터(백테스트 입력)로 쓴다.

```bash
# 위치·용량·스키마·적재 가능 여부 확인 (다운로드 없음)
python -m src.ingest.bitmex_public inspect --start 2018-01-01 --end 2020-12-31

# 다운로드 — 이어받기·크기/gzip 검증·용량 가드. --symbols 지정 시 해당 종목만 남기고 원본 삭제
python -m src.ingest.bitmex_public download --start 2019-06-01 --end 2019-06-30 --symbols XBTUSD
```

기본 상한 `--max-gb 5`, 다운로드 후 최소 여유 `--min-free-gb 20`. 넘으면 시작 전에 거부한다.
저장 위치 `data/raw/bitmex/<dataset>/` (커밋 금지).

### 정규화 (증분·재실행)

받은 원본 일 파일을 정규화 체결(`trades`)과 1분봉(`bars_1m`)으로 바꿔 UTC 일별 parquet 로 저장한다.

```bash
python -m src.ingest.normalize --start 2019-06-01 --end 2019-06-30 --symbol XBTUSD
# 옵션: --raw-dir data/raw/bitmex/trade (기본)  --out data/raw/normalized/bitmex (기본)
```

- 원본: `<raw-dir>/XBTUSD/YYYYMMDD.csv.gz`(`download --symbols XBTUSD` 결과) 우선, 없으면 전 종목 `<raw-dir>/YYYYMMDD.csv.gz`.
  원본이 없는 날은 경고만 남기고 건너뛴다(종료코드 0).
- 출력: `<out>/trades/XBTUSD/YYYYMMDD.parquet`, `<out>/bars_1m/XBTUSD/YYYYMMDD.parquet` — 임시 파일 후 원자적 교체.
- 증분: `<out>/_manifest/XBTUSD.json` 에 원본 크기·mtime 을 기록해 변경 없는 날은 건너뛰고, 바뀐 날(과
  마지막 close 가 바뀌어 영향받는 다음 날)만 다시 만든다. 같은 명령을 다시 실행하면 아무것도 다시 쓰지 않는다.
- 중복: `trdMatchID` 기준 첫 행만 남기고, 다시 처리할 때는 그날 파일을 통째로 다시 쓴다.
- 강제 재처리: manifest 파일(또는 그 안의 날짜 항목)을 지우고 다시 실행.
- 처리 오류(그날 밖 체결·스키마 위반 등)는 즉시 종료코드 1. 그 전까지 끝난 날은 유지된다.

읽기 — 구간(양끝 포함 UTC 일)을 한 프레임으로 읽고 스키마 검증까지 한다.

```python
from datetime import date
from src.ingest.store import load_bars, load_trades, missing_days
bars = load_bars(date(2019, 6, 1), date(2019, 6, 30))         # ts 오름차순, validate 통과
gaps = missing_days(date(2019, 6, 1), date(2019, 6, 30))      # 파일 없는 날 [date, ...]
```

- 결측 일이 있으면 기본은 `MissingDaysError`(`.days`). `allow_missing=True` 면 경고 후 있는 날만 읽는데,
  결과가 끊긴 구간을 포함하므로 `missing_days` 로 연속 구간을 나눠 쓴다.

## 분석 — Phase 2 합성 전략 분포

정규화 1분봉 구간을 읽어 합성 전략(H1 돌파·H2 모멘텀·H3 평균회귀) 파라미터 그리드별로 가상 라운드트립을 만들고,
run(`strategy_id`, `param_id`)별 분포 요약을 저장한다.

```bash
python -m src.analysis.run --start 2019-06-01 --end 2019-06-07 --symbol XBTUSD
# 옵션: --grid grid.json (생략 시 설계 전체 그리드 2,268 run)
#       --data-dir data/raw/normalized/bitmex (기본)  --out data/out/analysis (기본)
```

`--grid` 는 축 → 값 목록 JSON 이다. 설계 그리드의 부분집합만 허용하고(그리드 밖 값·알 수 없는 축은 종료코드 2),
지정하지 않은 축은 설계 값 전체를 쓴다. H1 은 `k` 축을 무시한다.

```json
{"trigger": ["h1", "h2"], "n": [15], "k": [2.0], "stop_pct": [0.5], "tp_r": [2, null], "max_hold": [60], "risk_pct": [1]}
```

- 출력(`data/out/` 은 커밋 안 됨):
  - `<out>/roundtrips/<strategy_id>/<YYYYMMDD>_<YYYYMMDD>.parquet` — 이번에 실행한 트리거의 모든 param_id(거래 0건이면 0행)
  - `<out>/distributions/<YYYYMMDD>_<YYYYMMDD>.json` — `meta`(구간·심볼·봉 수·그리드) + `runs`(run 별 요약 + `halted`)
  - 같은 이름 `.md` — run 별 핵심 지표 표
- 표본 구간은 2018-03-01~2021-12-31. 2022-01-01 이후(OOS)나 표본 시작 이전 요청은 데이터를 읽기 전에 거부한다(종료코드 2).
- 결측 일이 있으면 `MissingDaysError` 로 종료코드 1, 산출물은 쓰지 않는다(모든 run 을 끝낸 뒤 한꺼번에 원자적 쓰기).
- 모든 손익 지표는 gross(수수료·슬리피지·펀딩 전). 같은 입력이면 같은 결과(JSON·MD 바이트 동일, 실행 시각은 로그에만).
- `--grid` 로 일부 트리거만 돌리면 다른 트리거의 이전 roundtrips 파일은 지우지 않는다. 기준은 JSON 의 `meta.grid`·`runs`.
- 전체 그리드는 run 마다 봉 순차 루프라 구간이 길면 오래 걸린다(벡터화·병렬화는 후속).

## 백테스트 — Phase 3

`python -m src.backtest.run` 은 정규화 1분봉 구간에서 합성 전략 그리드를 돌리고, 라운드트립에 수수료·슬리피지를
적용(`costs`)한 뒤 run 별 net 지표(Sharpe·MDD·누적 수익)와 게이트 판정(`metrics`)을 저장한다.

```bash
# 구간은 반열린 [start, end) — --end 날짜는 포함하지 않는다(분석 CLI 와 다름)
python -m src.backtest.run --start 2019-06-01 --end 2019-06-08 --symbol XBTUSD
python -m src.backtest.run --start 2020-03-01 --end 2020-04-01 --fee-profile bybit
# 옵션: --grid grid.json (분석 CLI 와 같은 형식, 생략 시 2,268 run)
#       --fee-profile {default,bybit} (기본 default)
#       --data-dir data/raw/normalized/bitmex (기본)  --out data/out/backtest (기본)
```

- 출력(`data/out/` 은 커밋 안 됨, 파일명 두 번째 날짜는 반열린 end):
  - `<out>/roundtrips_net/<fee_profile>/<strategy_id>/<YYYYMMDD>_<YYYYMMDD>.parquet` — gross 24열 + net 8열
  - `<out>/summary/<fee_profile>/<YYYYMMDD>_<YYYYMMDD>.json` — `meta`(구간·심볼·그리드·수수료 프로필 값·게이트 기준 4키·통과/실패/부족 수) + `runs`(run 별 요약 15키)
  - 같은 이름 `.md` — run 별 핵심 지표 표와 통과 run 수
- 기본 표본 [2018-03-01, 2022-01-01) 밖, OOS(2022-01-01 이후), 2025 이후, `start ≥ end` 는 데이터를 읽기 전에 거부(종료코드 2).
- 결측 일이면 종료코드 1, 산출물은 쓰지 않는다. 같은 입력이면 JSON·MD 바이트 동일.
- 게이트 기준은 코드 기본값(거래 ≥ 100 · Sharpe ≥ 1.0 · MDD ≤ 30%, DSR ≥ 0.95 는 워크포워드용)을 쓴다.
- **최종 판정 아님** — 단일 구간 3기준 판정이다. 워크포워드·DSR(`--walkforward`)과 OOS 1회 평가(`--oos-final`)는 후속. 펀딩·강제청산 미모델링.

로드맵과 단계별 완료 기준은 Obsidian `Projects/work/AgentTrading/task/todo.md` 참고.

## 개발

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
```

## 전제 / 리스크

- **데이터 가용성이 최대 불확실성.** 워뇨띠의 체결 단위 기록은 공개돼 있지 않을 수 있다.
  거래소 리더보드·SNS 캡처는 표본이 치우치고 해상도가 낮다. Phase 0에서 "무엇을 실제로
  얻을 수 있는지" 확정하기 전까지 이후 단계 설계를 고정하지 않는다.
- 한 사람의 거래 기록은 표본이 작고 생존 편향이 있다. 백테스트는 전략 복제가 아니라
  **가설 검증** 용도로 쓴다.
- 레버리지 선물은 파산 리스크가 실재한다. 실거래 전환은 별도 판단을 거친다.
