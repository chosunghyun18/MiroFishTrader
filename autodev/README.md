# autodev — Claude Code 자율 개발 루프

주제를 주거나 프로젝트 TODO 를 가리키면, 사용량 한도나 시간 상한에 닿을 때까지 아래 사이클을 사람 없이 반복한다.

```
분해(SPLIT) → 계획(PLAN) → 독립 검토(REVIEW) → 구현·테스트(BUILD) → 기계 검증 → 문서화 → commit · push
```

설계와 결정 배경은 Obsidian 볼트의 `Private/업무 자동화/autodev 설계와 운영.md` 에 있다.

## 빠른 시작

```bash
cd ~/Desktop/work

# 남은 사용량과 리셋 시각
autodev/autodev quota

# 새 주제를 주고 끝까지 돌리기
autodev/autodev run -P AgentTrading -t "Phase 0 데이터 소스 조사를 끝낸다"

# 주제 없이: 큐에 있는 태스크를 처리하고, 큐가 비면 프로젝트 TODO 에서 다음 작업을 꺼낸다
autodev/autodev run -P MiroFishTrader

# 큐 현황 / 멈추기
autodev/autodev status
autodev/autodev stop
```

터미널을 닫아도 계속 돌리려면:

```bash
nohup autodev/autodev run -P AgentTrading > /tmp/autodev.out 2>&1 &
```

## 명령

| 명령 | 하는 일 |
|---|---|
| `run -P <프로젝트> [-t "주제"]` | 태스크를 연속 처리한다. 5시간 창이 차면 리셋까지 기다렸다가 이어간다 |
| `task -P <프로젝트> --id T-…` | 태스크 1개만 실행한다 |
| `split -P <프로젝트> [-t "주제"]` | 태스크 파일로 분해만 한다 (실행하지 않음) |
| `status [-P <프로젝트>] [--all]` | 큐 현황 |
| `quota [--cached]` | 5시간 창·주간 창 사용률과 리셋 시각. 종료코드 0=여유, 1=상한 도달 |
| `stop` | 현재 단계가 끝나면 루프를 멈춘다 |

`run` 의 주요 옵션: `--max-hours 10`, `--max-tasks 30`, `--weekly-budget 25`(한 번의 실행이 쓸 주간 사용량 %p),
`--no-wait`, `--no-push`, `--pr`(끝나면 main 대상 PR 생성).

## 어디에 무엇이 쌓이나

| 무엇 | 위치 |
|---|---|
| 태스크 큐 (계획·검토·구현 로그·완료 보고 포함) | 볼트 `Projects/work/<프로젝트>/task/autodev/T-*.md` |
| 실행 요약 (날짜별) | 볼트 `Private/업무 자동화/runs/` |
| 세션 핸드오프 기록 | 볼트 `Private/업무 자동화/handoff/<저장소>/`, 최신본은 `.claude/handoff/current.md` |
| 코드 변경 | worktree `~/Desktop/work-autodev`, 브랜치 `autodev/<프로젝트>` |
| 단계별 원본 로그 | `autodev/logs/<날짜>/` (git 제외) |

## 태스크 직접 넣기

볼트의 `Projects/work/<프로젝트>/task/autodev/` 에 `T-YYYYMMDD-NN 제목.md` 파일을 만들면 큐에 들어간다.
틀은 볼트 `Private/업무 자동화/templates/autodev-task.md`. `status` 값:

- `todo` 대기 · `planned` 계획됨 · `in-progress` 진행 중 · `done` 완료
- `blocked` 자동 진행 실패 (문서의 "사람이 할 일" 참고). 조치 후 `todo` 로 되돌리면 다시 시도한다
- `manual` 사람만 할 수 있는 일

## 프로젝트 추가

`autodev/projects/<이름>.json` 을 만든다.

```json
{
  "name": "MyProject",
  "dir": "MyProject",
  "vault": "Projects/work/MyProject",
  "ssot": ["Design Spec.md", "task/todo.md"],
  "verify": ".venv/bin/pytest -q",
  "setup": {"unless_exists": ".venv", "cmd": "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"},
  "link": [".env"],
  "notes": "모든 단계에 전달할 프로젝트 고유 제약"
}
```

## 환경변수

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `AUTODEV_MODEL` / `AUTODEV_MODEL_{SPLIT,PLAN,REVIEW,BUILD}` | 사용자 기본 모델 | 단계별 모델 |
| `AUTODEV_5H_LIMIT` | 90 | 5시간 창 사용률이 이 값 이상이면 리셋까지 대기 |
| `AUTODEV_7D_LIMIT` | 85 | 주간 창 사용률이 이 값 이상이면 종료 |
| `AUTODEV_PERMISSION_MODE` | auto | 헤드리스 권한 모드 |
| `AUTODEV_WORKTREE` | `../work-autodev` | 자동 루프 전용 worktree 경로 |

## 안전장치

- 코드는 전용 worktree 의 `autodev/<프로젝트>` 브랜치에서만 바뀐다. `main` 과 작업 중인 체크아웃은 건드리지 않는다.
- 완료 판정은 모델의 자기 보고가 아니라 검증 명령의 종료코드로 한다.
- 검증을 2회 연속 통과하지 못하면 그 태스크를 `blocked` 로 두고 다음 태스크로 넘어간다. 미완성 코드는 stash 에 보관한다.
- 태스크 3개가 연속으로 중단되면 루프를 멈춘다.
- 모델은 커밋·푸시·브랜치 전환을 직접 하지 못한다. 드라이버가 태스크당 1커밋으로 처리한다.
- 볼트에 쓰려면 실행하는 터미널 앱에 전체 디스크 접근 권한이 있어야 한다. 없으면 시작할 때 바로 알려준다.
