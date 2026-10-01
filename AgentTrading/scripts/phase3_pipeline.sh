#!/usr/bin/env bash
# Phase 3 데이터 확보 → 워크포워드 판정 → OOS 데이터 확보 (세션과 독립 실행)
#   nohup caffeinate -i scripts/phase3_pipeline.sh > data/logs/phase3_pipeline.log 2>&1 &
# 단계마다 data/logs/phase3_pipeline.state 에 진행 상태를 남긴다. 실패 시 같은 명령으로 다시 실행하면 이어서 한다.
# OOS 최종 1회 실행(--oos-final)은 사람 판단이라 여기서 하지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
STATE=data/logs/phase3_pipeline.state
step() { echo "$(date '+%F %T') $*" | tee -a "$STATE"; }
alarm() { osascript -e "display notification \"$1\" with title \"AgentTrading Phase 3\" sound name \"Glass\"" || true
          afplay /System/Library/Sounds/Glass.aiff || true; }
trap 'step "실패 (exit $?) — 로그 확인 후 다시 실행하면 이어서 진행"; alarm "파이프라인 실패 — 로그 확인"' ERR

step "1/5 기본 표본 다운로드 시작 (2018-03-01~2021-12-31, XBTUSD)"
$PY -m src.ingest.bitmex_public download --dataset trade --start 2018-03-01 --end 2021-12-31 --symbols XBTUSD --max-gb 40
step "2/5 기본 표본 정규화"
$PY -m src.ingest.normalize --start 2018-03-01 --end 2021-12-31 --symbol XBTUSD
$PY -c "import sys; from datetime import date; from src.ingest.store import missing_days; m=missing_days(date(2018,3,1), date(2021,12,31)); print('missing', len(m), m[:5]); sys.exit(1 if m else 0)"
step "3/5 펀딩 결측 확인"
$PY -m src.ingest.bitmex_funding --start 2018-03-01 --end 2025-01-01
step "4/5 워크포워드 판정 (--funding --jobs 4)"
$PY -m src.backtest.run --walkforward --funding --jobs 4
step "4/5 완료 — 결과: data/out/backtest/walkforward/"
alarm "워크포워드 판정 완료 — 결과 확인"
step "5/5 OOS 데이터 다운로드·정규화 (2022-01-01~2024-12-31) — 판정 실행은 하지 않음"
$PY -m src.ingest.bitmex_public download --dataset trade --start 2022-01-01 --end 2024-12-31 --symbols XBTUSD --max-gb 15
$PY -m src.ingest.normalize --start 2022-01-01 --end 2024-12-31 --symbol XBTUSD
step "전체 완료 — OOS 최종 1회 실행은 사람 판단"
alarm "Phase 3 데이터·워크포워드 완료"
