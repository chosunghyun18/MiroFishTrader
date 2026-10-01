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

Phase 0 — 데이터 소스 조사 중. BitMEX 공개 체결 장부 확인·다운로더 완료 (`src/ingest/bitmex_public.py`).

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
