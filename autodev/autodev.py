#!/usr/bin/env python3
"""autodev — Claude Code 자율 개발 루프 드라이버 (표준 라이브러리만 사용).

한 태스크의 사이클:  PLAN → REVIEW → BUILD → (기계 검증) → 문서화 → commit/push
태스크 큐의 단일 소스는 Obsidian 볼트의 `<프로젝트>/task/autodev/*.md` 이다.
코드는 전용 git worktree(`../work-autodev`)의 `autodev/<프로젝트>` 브랜치에서만 바뀐다.

사용법은 `autodev --help` 또는 autodev/README.md 참고.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── 경로·설정 ────────────────────────────────────────────────────────────────
TOOL_DIR = Path(__file__).resolve().parent
REPO = TOOL_DIR.parent
VAULT = Path(os.environ.get("AUTODEV_VAULT", "/Users/jo/Documents/Obsidian Vault"))
AUTO_VAULT = VAULT / "Private" / "업무 자동화"
WORKTREE = REPO   # 별도 worktree 없이 이 저장소(본 체크아웃)에서 직접 작업한다
SNAPSHOT = Path.home() / ".claude" / "usage-snapshot.json"
STOP_FILE = TOOL_DIR / "STOP"
CLAUDE = os.environ.get("AUTODEV_CLAUDE", shutil.which("claude") or "claude")

PERMISSION_MODE = os.environ.get("AUTODEV_PERMISSION_MODE", "auto")
MODEL = {  # 비우면 사용자 기본 모델을 그대로 쓴다
    "split": os.environ.get("AUTODEV_MODEL_SPLIT") or os.environ.get("AUTODEV_MODEL"),
    "plan": os.environ.get("AUTODEV_MODEL_PLAN") or os.environ.get("AUTODEV_MODEL"),
    "review": os.environ.get("AUTODEV_MODEL_REVIEW") or os.environ.get("AUTODEV_MODEL"),
    "build": os.environ.get("AUTODEV_MODEL_BUILD") or os.environ.get("AUTODEV_MODEL"),
    "handoff": os.environ.get("AUTODEV_MODEL_HANDOFF", "sonnet"),
    "probe": "haiku",
}
MAX_TURNS = {"split": 60, "plan": 60, "review": 40, "build": 200}
TIMEOUT_S = {"split": 30 * 60, "plan": 30 * 60, "review": 20 * 60, "build": 120 * 60}
VERIFY_TIMEOUT_S = 20 * 60
MAX_BUILD_ATTEMPTS = 2
MAX_PLAN_ROUNDS = 2

LIMIT_5H = float(os.environ.get("AUTODEV_5H_LIMIT", "90"))   # 5시간 창 사용률(%) 상한
LIMIT_7D = float(os.environ.get("AUTODEV_7D_LIMIT", "85"))   # 주간 창 사용률(%) 상한
SNAPSHOT_FRESH_S = 15 * 60

# 모델이 직접 하면 안 되는 git 동작 (커밋·푸시·브랜치 전환은 드라이버 담당)
DENY_TOOLS = [
    "Bash(git push *)", "Bash(git commit *)", "Bash(git switch *)", "Bash(git checkout -b *)",
    "Bash(git reset --hard *)", "Bash(git rebase *)", "Bash(git worktree *)",
    "Bash(git branch -D *)", "Bash(git clean *)", "Bash(git stash *)",
]
NO_MCP = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

SECTIONS = ["목표", "완료 기준", "계획", "결정 기록", "검토", "구현 로그", "완료 보고", "사람이 할 일"]


class StopRun(Exception):
    """루프를 정상 종료시키는 사유."""


# ── 공통 유틸 ────────────────────────────────────────────────────────────────
_log_file: Path | None = None


def now() -> dt.datetime:
    return dt.datetime.now()


def log(msg: str) -> None:
    line = f"[{now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if _log_file:
        with _log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def sh(cmd: list[str] | str, *, cwd: Path | None = None, timeout: int = 120,
       env: dict | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), text=True,
                          capture_output=True, timeout=timeout, env=env, check=check)


def git(*args: str, cwd: Path = WORKTREE, timeout: int = 120) -> subprocess.CompletedProcess:
    return sh(["git", *args], cwd=cwd, timeout=timeout)


def notify(title: str, msg: str, sound: bool = False) -> None:
    try:
        script = f'display notification {json.dumps(msg)} with title {json.dumps(title)}'
        if sound:
            script += ' sound name "Glass"'
        sh(["osascript", "-e", script], timeout=10)
    except Exception:
        pass


def alarm(title: str, msg: str) -> None:
    """놓치지 않게 알린다: 알림 + 소리 3회 + 닫을 때까지 남는 경고창."""
    notify(title, msg, sound=True)
    try:
        for _ in range(3):
            sh(["afplay", "/System/Library/Sounds/Glass.aiff"], timeout=10)
        subprocess.Popen(["osascript", "-e", f'display alert {json.dumps(title)} message {json.dumps(msg)}'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


# ── frontmatter (YAML 부분집합: `key: value`, 인라인 리스트) ─────────────────
def _parse_value(raw: str):
    raw = raw.strip()
    if raw == "":
        return ""
    if raw[0] in '"[':
        try:
            return json.loads(raw)
        except ValueError:
            if raw[0] == "[" and raw.endswith("]"):
                return [x.strip().strip("\"'") for x in raw[1:-1].split(",") if x.strip()]
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw.strip("'")


def _dump_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    s = str(v)
    if s == "" or re.search(r"[:#\"'\[\]{}|>&*!%@`]", s) or s != s.strip():
        return json.dumps(s, ensure_ascii=False)
    return s


def read_task(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    meta: dict = {}
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
                if m:
                    meta[m.group(1)] = _parse_value(m.group(2))
    meta["_path"] = path
    return meta


def update_task(path: Path, **updates) -> None:
    text = path.read_text(encoding="utf-8")
    updates.setdefault("updated", f"{now():%Y-%m-%d %H:%M}")
    if not text.startswith("---\n") or text.find("\n---", 4) == -1:
        fm = "\n".join(f"{k}: {_dump_value(v)}" for k, v in updates.items())
        path.write_text(f"---\n{fm}\n---\n\n{text}", encoding="utf-8")
        return
    end = text.find("\n---", 4)
    lines = text[4:end].splitlines()
    for k, v in updates.items():
        new = f"{k}: {_dump_value(v)}"
        for i, line in enumerate(lines):
            if re.match(rf"^{re.escape(k)}:", line):
                lines[i] = new
                break
        else:
            lines.append(new)
    path.write_text("---\n" + "\n".join(lines) + text[end:], encoding="utf-8")


def append_section(path: Path, section: str, content: str) -> None:
    """`## section` 아래 끝에 내용을 덧붙인다 (섹션이 없으면 문서 끝에 만든다)."""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(section)}[ \t]*\n", text, flags=re.M)
    block = content.rstrip() + "\n"
    if not m:
        text = text.rstrip() + f"\n\n## {section}\n\n{block}"
    else:
        nxt = re.search(r"^## ", text[m.end():], flags=re.M)
        pos = m.end() + nxt.start() if nxt else len(text)
        head = text[:pos].rstrip() + "\n\n"
        text = head + block + ("\n" + text[pos:] if nxt else "")
    path.write_text(text, encoding="utf-8")


# ── 프로젝트·태스크 큐 ───────────────────────────────────────────────────────
class Project:
    def __init__(self, name: str):
        conf_path = TOOL_DIR / "projects" / f"{name}.json"
        if not conf_path.exists():
            known = ", ".join(sorted(p.stem for p in (TOOL_DIR / "projects").glob("*.json")))
            sys.exit(f"프로젝트 설정이 없습니다: {conf_path}\n등록된 프로젝트: {known}")
        c = json.loads(conf_path.read_text(encoding="utf-8"))
        self.name: str = c.get("name", name)
        self.dir: str = c["dir"]                      # 레포 루트 기준 코드 디렉터리
        self.vault_rel: str = c["vault"]              # 볼트 루트 기준 문서 디렉터리
        self.ssot: list[str] = c.get("ssot", [])      # 프로젝트 볼트 기준 핵심 문서
        self.verify: str = c.get("verify", "")        # 기본 검증 명령
        self.setup: dict = c.get("setup", {})         # {"unless_exists": ".venv", "cmd": "..."}
        self.link: list[str] = c.get("link", [])      # 본 체크아웃에서 심링크할 비추적 파일
        self.notes: str = c.get("notes", "")


    @property
    def vault(self) -> Path:
        return VAULT / self.vault_rel

    @property
    def task_dir(self) -> Path:
        return self.vault / "task" / "autodev"

    @property
    def branch(self) -> str:
        """현재 체크아웃된 브랜치 (드라이버는 브랜치를 바꾸지 않는다)."""
        return git("branch", "--show-current").stdout.strip() or "HEAD"

    @property
    def code(self) -> Path:
        return WORKTREE / self.dir

    def tasks(self) -> list[dict]:
        if not self.task_dir.exists():
            return []
        out = [read_task(p) for p in sorted(self.task_dir.glob("T-*.md"))]
        return [t for t in out if t.get("id")]


def all_projects() -> list[str]:
    return sorted(p.stem for p in (TOOL_DIR / "projects").glob("*.json") if not p.stem.startswith("_"))


def next_task(tasks: list[dict]) -> dict | None:
    done = {t["id"] for t in tasks if t.get("status") == "done"}
    ready = [t for t in tasks
             if t.get("status") in ("todo", "planned", "in-progress")
             and all(d in done for d in (t.get("depends_on") or []))]
    ready.sort(key=lambda t: (int(t.get("priority", 3) or 3), t["id"]))
    return ready[0] if ready else None


def slug(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", title)
    return re.sub(r"\s+", " ", s).strip()[:60]


def new_task_id(project: Project) -> str:
    day = f"{now():%Y%m%d}"
    nums = [int(m.group(1)) for t in project.tasks()
            if (m := re.fullmatch(rf"T-{day}-(\d+)", str(t["id"])))]
    return f"T-{day}-{max(nums, default=0) + 1:02d}"


def write_task_file(project: Project, spec: dict, depends: list[str]) -> Path:
    project.task_dir.mkdir(parents=True, exist_ok=True)
    tid = new_task_id(project)
    status = "manual" if spec.get("needs_human") else "todo"
    fm = {
        "project": project.name, "type": "autodev-task", "id": tid, "title": spec["title"],
        "kind": spec.get("kind", "code"), "status": status,
        "priority": int(spec.get("priority", 3)), "depends_on": depends,
        "verify": spec.get("verify", ""), "attempts": 0,
        "created": f"{now():%Y-%m-%d}", "updated": f"{now():%Y-%m-%d %H:%M}",
        "tags": ["autodev"],
    }
    head = "\n".join(f"{k}: {_dump_value(v)}" for k, v in fm.items())
    accept = "\n".join(f"- [ ] {a}" for a in spec.get("acceptance", [])) or "- [ ] (미정)"
    human = spec.get("human_action", "").strip() if spec.get("needs_human") else ""
    body = (
        f"# {tid} · {spec['title']}\n\n"
        f"## 목표\n\n{spec.get('goal', '').strip()}\n\n"
        f"## 완료 기준\n\n{accept}\n\n"
        "## 계획\n\n## 결정 기록\n\n## 검토\n\n## 구현 로그\n\n## 완료 보고\n\n"
        f"## 사람이 할 일\n\n{human}\n"
    )
    path = project.task_dir / f"{tid} {slug(spec['title'])}.md"
    path.write_text(f"---\n{head}\n---\n\n{body}", encoding="utf-8")
    return path


# ── 사용량(쿼터) ─────────────────────────────────────────────────────────────
def save_snapshot(info: dict, source: str) -> None:
    """stream-json 의 rate_limit_event 를 스냅샷 파일로 저장한다."""
    snap = {"ts": int(time.time()), "source": source, "status": info.get("status"),
            "limit_type": info.get("rateLimitType")}
    windows = info.get("unifiedWindows") or {}
    for key in ("five_hour", "seven_day"):
        w = windows.get(key)
        if w and w.get("utilization") is not None:
            snap[key] = {"pct": round(float(w["utilization"]) * 100, 1), "resets_at": w.get("resetsAt")}
    if "five_hour" not in snap and "seven_day" not in snap and info.get("rateLimitType"):
        util = info.get("utilization")
        snap[info["rateLimitType"]] = {
            "pct": round(float(util) * 100, 1) if util is not None else None,
            "resets_at": info.get("resetsAt")}
    try:
        tmp = SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap), encoding="utf-8")
        tmp.replace(SNAPSHOT)
    except OSError:
        pass


def load_snapshot() -> dict | None:
    try:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def probe_quota() -> dict | None:
    """아주 작은 haiku 호출 1회로 최신 한도 정보를 받아 스냅샷을 갱신한다."""
    cmd = [CLAUDE, "-p", "--model", MODEL["probe"], "--system-prompt", "Reply with OK only.",
           "--tools", "", "--disable-slash-commands", "--no-session-persistence",
           "--output-format", "stream-json", "--verbose", "--max-turns", "1", *NO_MCP]
    env = {**os.environ, "AUTODEV": "1"}
    try:
        p = subprocess.run(cmd, input="OK", text=True, capture_output=True, timeout=120,
                           env=env, cwd=str(Path.home()))
    except (subprocess.TimeoutExpired, OSError):
        return load_snapshot()
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "rate_limit_event":
            save_snapshot(ev.get("rate_limit_info") or {}, "probe")
    return load_snapshot()


def fresh_snapshot(force: bool = False) -> dict | None:
    snap = load_snapshot()
    if force or not snap or time.time() - snap.get("ts", 0) > SNAPSHOT_FRESH_S:
        snap = probe_quota()
    return snap


def fmt_reset(ts) -> str:
    if not ts:
        return "?"
    d = dt.datetime.fromtimestamp(int(ts))
    left = int(ts) - int(time.time())
    if left <= 0:
        return f"{d:%m-%d %H:%M} (지남)"
    if left >= 48 * 3600:
        return f"{d:%m-%d %H:%M} ({left // 86400}일 {left % 86400 // 3600}시간 뒤)"
    return f"{d:%m-%d %H:%M} ({left // 3600}시간 {left % 3600 // 60}분 뒤)"


def fmt_quota(snap: dict | None) -> str:
    if not snap:
        return "사용량 정보를 가져오지 못했습니다"
    parts = []
    for key, label in (("five_hour", "5시간 창"), ("seven_day", "주간 창")):
        w = snap.get(key)
        if w:
            pct = "?" if w.get("pct") is None else f"{w['pct']:.0f}%"
            parts.append(f"{label} {pct} 사용 · 리셋 {fmt_reset(w.get('resets_at'))}")
    age = int(time.time() - snap.get("ts", 0))
    return " | ".join(parts) + f" | 상태 {snap.get('status')} · {age}초 전 측정({snap.get('source')})"


# ── Claude 헤드리스 실행 ─────────────────────────────────────────────────────
def render(name: str, **vars) -> str:
    text = (TOOL_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    for k, v in vars.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def run_claude(phase: str, prompt: str, *, cwd: Path, schema: dict | None, log_path: Path) -> dict:
    """claude -p 를 1회 실행한다. 반환: ok / structured / text / rate_limited / limit_type / resets_at."""
    cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
           "--permission-mode", PERMISSION_MODE, "--permission-prompts", "none",
           "--max-turns", str(MAX_TURNS[phase]),
           "--append-system-prompt", render("policy"),
           "--add-dir", str(VAULT),
           "--disallowedTools", *DENY_TOOLS,
           *NO_MCP]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if MODEL.get(phase):
        cmd += ["--model", MODEL[phase]]
    env = {**os.environ, "AUTODEV": "1", "AUTODEV_PHASE": phase}
    log_path.parent.mkdir(parents=True, exist_ok=True)

    out = {"ok": False, "structured": None, "text": "", "rate_limited": False,
           "limit_type": None, "resets_at": None, "turns": 0, "timed_out": False}
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, text=True, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    timer = threading.Timer(TIMEOUT_S[phase], lambda: (out.update(timed_out=True), proc.kill()))
    timer.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        with log_path.open("w", encoding="utf-8") as lf:
            for line in proc.stdout:
                lf.write(line)
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "rate_limit_event":
                    info = ev.get("rate_limit_info") or {}
                    save_snapshot(info, "headless")
                    if info.get("status") == "rejected":
                        out.update(rate_limited=True, limit_type=info.get("rateLimitType"),
                                   resets_at=info.get("resetsAt"))
                elif kind == "assistant":
                    for block in (ev.get("message") or {}).get("content") or []:
                        if block.get("type") == "tool_use":
                            inp = block.get("input") or {}
                            hint = inp.get("file_path") or inp.get("command") or inp.get("description") or ""
                            print(f"    · {block.get('name')} {' '.join(str(hint).split())[:90]}", flush=True)
                elif kind == "result":
                    out["text"] = ev.get("result") or ""
                    out["structured"] = ev.get("structured_output")
                    out["turns"] = ev.get("num_turns", 0)
                    out["ok"] = not ev.get("is_error") and ev.get("subtype") == "success"
                    if ev.get("api_error_status") == 429 or (
                            ev.get("is_error") and re.search(
                                r"usage limit|rate limit|hit your limit|limit reached", out["text"], re.I)):
                        out["rate_limited"] = True
        proc.wait()
    finally:
        timer.cancel()
    if out["structured"] is None and out["text"]:
        m = re.search(r"\{.*\}", out["text"], flags=re.S)   # 구조화 출력 누락 시 본문 JSON 으로 대체
        if m:
            try:
                out["structured"] = json.loads(m.group(0))
            except ValueError:
                pass
    if out["rate_limited"]:
        out["ok"] = False
    return out


# ── 실행 상태·쿼터 게이트 ────────────────────────────────────────────────────
class Run:
    def __init__(self, project: Project, args):
        self.project = project
        self.wait = not args.no_wait
        self.push = not args.no_push
        self.forever = bool(getattr(args, "forever", False))
        self.deadline = float("inf") if self.forever else time.time() + args.max_hours * 3600
        self.weekly_budget = float("inf") if self.forever else args.weekly_budget
        self.week_start: float | None = None
        self.started = now()
        self.log_dir = TOOL_DIR / "logs" / f"{self.started:%Y-%m-%d}" / f"{self.started:%H%M%S}-{project.name}"
        self.results: list[dict] = []   # {id, title, status, note, commit}
        self.stop_reason = ""
        self.quota_start = ""

    def check_stop(self) -> None:
        if STOP_FILE.exists():
            raise StopRun("STOP 파일 감지 (autodev stop)")
        if time.time() > self.deadline:
            raise StopRun("최대 실행 시간 도달")

    def sleep_until(self, ts: float, why: str) -> None:
        if not self.wait:
            raise StopRun(f"{why} — 대기 안 함(--no-wait)")
        if ts + 90 > self.deadline:
            raise StopRun(f"{why} — 리셋이 최대 실행 시간 이후라 종료")
        log(f"⏸  {why} — {fmt_reset(ts)} 까지 대기")
        while time.time() < ts + 90:
            self.check_stop()
            time.sleep(60)

    def gate(self) -> None:
        """단계 실행 전 호출: 중단 조건과 사용량을 확인하고, 필요하면 리셋까지 기다린다."""
        self.check_stop()
        for _ in range(6):
            snap = fresh_snapshot()
            if not snap:
                return                      # 정보 없음 → 진행 (실제 한도 도달은 사후에 감지)
            week = (snap.get("seven_day") or {}).get("pct")
            five = (snap.get("five_hour") or {}).get("pct")
            if week is not None:
                if self.week_start is None:
                    self.week_start = week
                if week >= LIMIT_7D:
                    if not self.forever:
                        raise StopRun(f"주간 사용량 {week:.0f}% ≥ 상한 {LIMIT_7D:.0f}%")
                    reset = (snap.get("seven_day") or {}).get("resets_at")
                    self.sleep_until(reset or time.time() + 3600, f"주간 창 {week:.0f}% 사용")
                    self.week_start = None
                    fresh_snapshot(force=True)
                    continue
                if week - self.week_start >= self.weekly_budget:
                    raise StopRun(f"이번 실행의 주간 예산 {self.weekly_budget:.0f}%p 소진 "
                                  f"({self.week_start:.0f}% → {week:.0f}%)")
            if five is None or five < LIMIT_5H:
                return
            reset = (snap.get("five_hour") or {}).get("resets_at")
            if reset and reset <= time.time():
                fresh_snapshot(force=True)
                continue
            self.sleep_until(reset or time.time() + 1800, f"5시간 창 {five:.0f}% 사용")
            fresh_snapshot(force=True)

    def call(self, phase: str, prompt: str, schema: dict, tag: str) -> dict:
        """쿼터 게이트 + 실행 + 한도 도달 시 리셋 대기 후 재시도."""
        for attempt in range(1, 200 if self.forever else 5):
            self.gate()
            log(f"▶ {phase.upper()} {tag}")
            res = run_claude(phase, prompt, cwd=self.cwd_for(phase), schema=schema,
                             log_path=self.log_dir / f"{tag}-{phase}-{attempt}.jsonl")
            if not res["rate_limited"]:
                if res["timed_out"]:
                    log(f"  ⚠ {phase} 시간 초과")
                return res
            if res["limit_type"] and res["limit_type"] != "five_hour" and not self.forever:
                raise StopRun(f"한도 도달 ({res['limit_type']})")
            self.sleep_until(res["resets_at"] or time.time() + 1800, f"한도 도달 ({res['limit_type'] or '5시간 창'})")
            fresh_snapshot(force=True)
        raise StopRun("한도 대기 재시도 초과")

    def cwd_for(self, phase: str) -> Path:
        return self.project.code if self.project.code.exists() else WORKTREE


# ── 작업 공간 준비 (본 체크아웃 그대로 사용) ─────────────────────────────────
def project_dirty(project: Project) -> str:
    """이 프로젝트 폴더 안의 미커밋 변경만 본다 (다른 프로젝트·다른 세션의 변경은 무시)."""
    return git("status", "--porcelain", "--", project.dir).stdout.strip()


def ensure_worktree(project: Project) -> None:
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        sys.exit(f"git 저장소가 아닙니다: {REPO}")
    if git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        sys.exit("병합이 진행 중입니다. 병합을 끝낸 뒤 다시 실행하세요.")
    project.code.mkdir(parents=True, exist_ok=True)
    marker = project.setup.get("unless_exists")
    if project.setup.get("cmd") and not (marker and (project.code / marker).exists()):
        log(f"환경 준비: {project.setup['cmd']}")
        r = sh(project.setup["cmd"], cwd=project.code, timeout=1800)
        if r.returncode != 0:
            log(f"⚠ 환경 준비 실패 (계속 진행):\n{(r.stdout + r.stderr)[-1500:]}")
    if project_dirty(project):
        log(f"⚠ {project.dir}/ 에 커밋 안 된 변경이 있습니다. 다른 세션의 작업일 수 있어 그대로 두고, 태스크 커밋에도 섞지 않습니다.")


def sync_base() -> None:
    """(이전 worktree 방식의 잔재) 본 체크아웃에서 직접 작업하므로 할 일이 없다."""
    return


def preflight(project: Project) -> None:
    if not shutil.which(CLAUDE) and not Path(CLAUDE).exists():
        sys.exit("claude CLI 를 찾을 수 없습니다")
    try:
        project.task_dir.mkdir(parents=True, exist_ok=True)
        probe = project.task_dir / ".autodev-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        sys.exit(f"옵시디언 볼트에 쓸 수 없습니다: {e}\n"
                 "→ 이 터미널 앱에 '전체 디스크 접근 권한'이 있는지 확인하세요 (시스템 설정 > 개인정보 보호 및 보안).")


# ── 단계: SPLIT / PLAN / REVIEW / BUILD ──────────────────────────────────────
SPLIT_SCHEMA = {"type": "object", "required": ["tasks", "notes"], "properties": {
    "notes": {"type": "string"},
    "tasks": {"type": "array", "items": {"type": "object", "required": [
        "title", "kind", "goal", "acceptance", "verify", "priority", "depends_on", "needs_human"],
        "properties": {
            "title": {"type": "string"}, "kind": {"type": "string", "enum": ["code", "research", "docs"]},
            "goal": {"type": "string"}, "acceptance": {"type": "array", "items": {"type": "string"}},
            "verify": {"type": "string"}, "priority": {"type": "integer"},
            "depends_on": {"type": "array", "items": {"type": "integer"}},
            "needs_human": {"type": "boolean"}, "human_action": {"type": "string"}}}}}}
PLAN_SCHEMA = {"type": "object", "required": ["status", "summary", "verify"], "properties": {
    "status": {"type": "string", "enum": ["planned", "blocked"]}, "summary": {"type": "string"},
    "verify": {"type": "string"}, "human_action": {"type": "string"}}}
REVIEW_SCHEMA = {"type": "object", "required": ["verdict", "summary", "issues"], "properties": {
    "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
    "summary": {"type": "string"}, "issues": {"type": "array", "items": {"type": "string"}}}}
BUILD_SCHEMA = {"type": "object", "required": ["status", "summary", "commit_message"], "properties": {
    "status": {"type": "string", "enum": ["done", "blocked", "failed"]}, "summary": {"type": "string"},
    "commit_message": {"type": "string"}, "human_action": {"type": "string"}}}


def common_vars(project: Project) -> dict:
    ssot = "\n".join(f"- {project.vault / s}" for s in project.ssot) or "- (등록된 문서 없음 — 프로젝트 볼트 폴더를 직접 살펴볼 것)"
    return {"project": project.name, "code_dir": project.code, "vault_dir": project.vault,
            "task_dir": project.task_dir, "ssot": ssot, "default_verify": project.verify or "(없음)",
            "project_notes": project.notes or "(없음)", "today": f"{now():%Y-%m-%d}",
            "auto_vault": AUTO_VAULT}


def do_split(run: Run, topic: str | None) -> int:
    project = run.project
    existing = "\n".join(f"- {t['id']} [{t.get('status')}] {t.get('title')}" for t in project.tasks()) or "(없음)"
    source = (f"사용자가 준 주제:\n{topic}" if topic else
              "사용자가 따로 주제를 주지 않았다. 프로젝트의 TODO·로드맵 문서에서 "
              "'지금 착수 가능한' 미완료 항목을 골라 태스크로 만든다.")
    prompt = render("split", **common_vars(project), source=source, existing=existing)
    res = run.call("split", prompt, SPLIT_SCHEMA, f"split-{now():%H%M%S}")
    data = res["structured"] or {}
    specs = data.get("tasks") or []
    ids: list[str] = []
    for spec in specs:
        deps = [ids[i] for i in spec.get("depends_on", []) if isinstance(i, int) and 0 <= i < len(ids)]
        path = write_task_file(project, spec, deps)
        tid = read_task(path)["id"]
        ids.append(tid)
        log(f"  + {tid} [{'manual' if spec.get('needs_human') else 'todo'}] {spec['title']}")
    if data.get("notes"):
        log(f"  분해 메모: {data['notes'][:300]}")
    actionable = sum(1 for s in specs if not s.get("needs_human"))
    log(f"태스크 {len(specs)}개 생성 (자동 실행 가능 {actionable}개)")
    return actionable


def run_verify(project: Project, task: dict) -> tuple[bool, str]:
    cmd = (task.get("verify") or "").strip() or project.verify
    if not cmd:
        return True, "(검증 명령 없음)"
    env = {**os.environ, "VAULT": str(VAULT), "PROJECT_VAULT": str(project.vault),
           "TASK_FILE": str(task["_path"]), "CODE_DIR": str(project.code)}
    try:
        r = sh(cmd, cwd=project.code, timeout=VERIFY_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return False, f"검증 시간 초과: {cmd}"
    tail = (r.stdout + r.stderr)[-3000:]
    return r.returncode == 0, f"$ {cmd}\n(exit {r.returncode})\n{tail}"


def dirty_files(project: Project) -> dict[str, str]:
    """프로젝트 폴더 안의 미커밋 파일 → 내용 해시."""
    out = {}
    for line in git("status", "--porcelain", "-uall", "--", project.dir).stdout.splitlines():
        rel = line[3:].split(" -> ")[-1].strip().strip('"')
        f = REPO / rel
        out[rel] = git("hash-object", str(f)).stdout.strip() if f.is_file() else "-"
    return out


def task_changes(task: dict) -> list[str]:
    """이 태스크가 만든 변경만 고른다: 시작 전부터 있던 미커밋 파일이 그대로면 제외."""
    project = Project(task["project"]) if task.get("project") else None
    if project is None:
        return []
    before = task.get("_preexisting") or {}
    return [rel for rel, h in dirty_files(project).items() if before.get(rel) != h]


def block(run: Run, task: dict, reason: str, human: str = "") -> dict:
    path = task["_path"]
    note = f"- {now():%Y-%m-%d %H:%M} **blocked** — {reason}"
    if human:
        note += f"\n  - 필요한 조치: {human}"
    changed = task_changes(task)
    if changed:
        name = f"autodev blocked {task['id']}"
        git("stash", "push", "--include-untracked", "-m", name, "--", *changed)
        note += f"\n  - 미완성 코드는 git stash `{name}` 에 보관 (`git stash list`)"
    append_section(path, "사람이 할 일", note)
    update_task(path, status="blocked")
    log(f"  ✗ blocked: {reason[:200]}")
    return {"id": task["id"], "title": task.get("title"), "status": "blocked", "note": human or reason, "commit": ""}


def run_task(run: Run, task: dict) -> dict:
    project, path, tid = run.project, task["_path"], task["id"]
    log(f"━━ {tid} · {task.get('title')}")
    v = {**common_vars(project), "task_file": path, "task_id": tid}
    update_task(path, status="in-progress", attempts=int(task.get("attempts", 0) or 0) + 1)
    task["_preexisting"] = dirty_files(project)

    # 1) PLAN ↔ REVIEW (최대 MAX_PLAN_ROUNDS 회, 그 뒤에는 검토 의견을 안고 진행)
    feedback = "(없음 — 첫 계획)"
    for rnd in range(1, MAX_PLAN_ROUNDS + 1):
        res = run.call("plan", render("plan", **v, feedback=feedback), PLAN_SCHEMA, tid)
        plan = res["structured"] or {}
        if plan.get("status") == "blocked":
            return block(run, task, plan.get("summary", "계획 단계에서 중단"), plan.get("human_action", ""))
        if plan.get("status") != "planned":
            return block(run, task, f"계획 단계가 결과를 내지 못함: {res['text'][-300:]}")
        if plan.get("verify"):
            update_task(path, verify=plan["verify"])
        res = run.call("review", render("review", **v), REVIEW_SCHEMA, tid)
        review = res["structured"] or {}
        verdict = review.get("verdict", "approve")
        log(f"  검토: {verdict} — {review.get('summary', '')[:160]}")
        if verdict == "reject":
            return block(run, task, f"검토에서 반려: {review.get('summary', '')}",
                         "; ".join(review.get("issues", [])))
        if verdict == "approve":
            break
        feedback = "\n".join(f"- {i}" for i in review.get("issues", [])) or review.get("summary", "")
        if rnd == MAX_PLAN_ROUNDS:
            append_section(path, "결정 기록",
                           f"- {now():%Y-%m-%d %H:%M} 검토 의견이 남았지만 자동 진행 (재계획 {MAX_PLAN_ROUNDS}회 소진). "
                           "남은 의견은 구현 단계에서 반영한다.")
    update_task(path, status="planned")
    task = {**read_task(path), "_preexisting": task.get("_preexisting", {})}

    # 2) BUILD → 기계 검증 (최대 MAX_BUILD_ATTEMPTS 회)
    failure = "(없음 — 첫 구현)"
    build: dict = {}
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        res = run.call("build", render("build", **v, failure=failure,
                                       verify=task.get("verify") or project.verify or "(없음)"),
                       BUILD_SCHEMA, tid)
        build = res["structured"] or {}
        if build.get("status") == "blocked":
            return block(run, task, build.get("summary", "구현 단계에서 중단"), build.get("human_action", ""))
        ok, report = run_verify(project, read_task(path))
        log(f"  검증: {'통과' if ok else '실패'}")
        if ok and build.get("status") == "done":
            break
        failure = report if not ok else f"구현 단계가 완료를 보고하지 않음: {build.get('summary') or res['text'][-500:]}"
        if attempt == MAX_BUILD_ATTEMPTS:
            append_section(path, "구현 로그", f"### 검증 실패 ({now():%Y-%m-%d %H:%M})\n\n```\n{failure[-1500:]}\n```")
            return block(run, task, f"검증을 {MAX_BUILD_ATTEMPTS}회 연속 통과하지 못함")

    # 3) commit / push (드라이버가 수행)
    commit = ""
    changed = task_changes(task)
    if changed:
        git("add", "-A", "--", *changed)
    if changed and git("diff", "--cached", "--quiet", "--", *changed).returncode != 0:
        subject = (build.get("commit_message") or f"feat: {task.get('title')}").strip().splitlines()[0][:100]
        msg = f"{subject}\n\nAutodev-Task: {tid}\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
        r = git("commit", "-q", "-m", msg, "--", *changed)
        if r.returncode != 0:
            return block(run, task, f"커밋 실패: {r.stderr[-300:]}")
        commit = git("rev-parse", "--short", "HEAD").stdout.strip()
        if run.push:
            r = git("push", "-u", "origin", "HEAD", timeout=300)
            if r.returncode != 0:
                log(f"  ⚠ push 실패 (다음 태스크에서 다시 시도): {r.stderr.strip()[-200:]}")
    update_task(path, status="done", commit=commit or "(코드 변경 없음)", completed=f"{now():%Y-%m-%d %H:%M}")
    log(f"  ✓ done {commit}")
    return {"id": tid, "title": task.get("title"), "status": "done",
            "note": build.get("summary", ""), "commit": commit}


# ── 실행 요약 ────────────────────────────────────────────────────────────────
def write_summary(run: Run) -> Path:
    project = run.project
    out = AUTO_VAULT / "runs" / f"{run.started:%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    tasks = project.tasks()
    done = [r for r in run.results if r["status"] == "done"]
    blocked = [r for r in run.results if r["status"] == "blocked"]
    manual = [t for t in tasks if t.get("status") in ("manual", "blocked")]
    left = [t for t in tasks if t.get("status") in ("todo", "planned", "in-progress")]
    lines = [f"## {run.started:%H:%M}–{now():%H:%M} · {project.name}", "",
             f"- 종료 사유: {run.stop_reason or '큐 소진'}",
             f"- 브랜치: `{project.branch}` (`{REPO}`)",
             f"- 사용량 시작: {run.quota_start}",
             f"- 사용량 종료: {fmt_quota(load_snapshot())}",
             f"- 완료 {len(done)} · 중단 {len(blocked)} · 남은 큐 {len(left)}", ""]
    if done:
        lines += ["### 완료", ""] + [f"- [[{_stem(tasks, r['id'])}|{r['id']}]] {r['title']} `{r['commit']}` — {r['note'][:200]}" for r in done] + [""]
    if manual:
        lines += ["### 사람이 할 일", ""] + [f"- [[{t['_path'].stem}|{t['id']}]] ({t.get('status')}) {t.get('title')}" for t in manual] + [""]
    if left:
        lines += ["### 남은 큐", ""] + [f"- {t['id']} {t.get('title')}" for t in left[:15]] + [""]
    header = "" if out.exists() else (
        f"---\ntags: [업무자동화, autodev, run-log]\ncreated: {run.started:%Y-%m-%d}\n---\n\n"
        f"# autodev 실행 기록 {run.started:%Y-%m-%d}\n\n")
    with out.open("a", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    return out


def append_progress(run: Run, result: dict) -> None:
    """태스크가 끝날 때마다 그날의 실행 기록에 한 줄 남긴다 (긴 실행 중에도 진행을 볼 수 있게)."""
    try:
        out = AUTO_VAULT / "runs" / f"{now():%Y-%m-%d}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        header = "" if out.exists() else (
            f"---\ntags: [업무자동화, autodev, run-log]\ncreated: {now():%Y-%m-%d}\n---\n\n"
            f"# autodev 실행 기록 {now():%Y-%m-%d}\n\n")
        mark = "✓" if result["status"] == "done" else "✗"
        line = (f"- {now():%H:%M} {mark} {run.project.name} {result['id']} {result['title']}"
                f"{' `' + result['commit'] + '`' if result.get('commit') else ''}"
                f"{'' if result['status'] == 'done' else ' — ' + str(result.get('note', ''))[:150]}\n")
        with out.open("a", encoding="utf-8") as f:
            f.write(header + line)
    except OSError:
        pass


def _stem(tasks: list[dict], tid: str) -> str:
    for t in tasks:
        if t["id"] == tid:
            return t["_path"].stem
    return tid


# ── 명령: run / task / split / status / quota / stop ─────────────────────────
def start_run(args) -> Run:
    global _log_file
    project = Project(args.project)
    run = Run(project, args)
    run.log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = run.log_dir / "run.log"
    preflight(project)
    STOP_FILE.unlink(missing_ok=True)
    ensure_worktree(project)
    run.quota_start = fmt_quota(fresh_snapshot(force=True))
    log(f"프로젝트 {project.name} · 브랜치 {project.branch} · {run.quota_start}")
    return run


def finish_run(run: Run, pr: bool = False) -> None:
    summary = write_summary(run)
    done = sum(1 for r in run.results if r["status"] == "done")
    blocked = sum(1 for r in run.results if r["status"] == "blocked")
    if pr and done and run.push:
        open_pr(run)
    log(f"종료: {run.stop_reason or '큐 소진'} · 완료 {done} · 중단 {blocked}")
    log(f"요약: {summary}")
    need = sum(1 for t in run.project.tasks() if t.get("status") in ("blocked", "manual"))
    msg = f"{run.project.name}: 완료 {done} · 중단 {blocked} · 사람 조치 필요 {need} — {run.stop_reason or '큐 소진'}"
    (alarm if run.forever else notify)("autodev 종료", msg)


def open_pr(run: Run) -> None:
    b = run.project.branch
    if sh(["gh", "pr", "view", b, "--json", "url"], cwd=WORKTREE).returncode == 0:
        return
    body = "autodev 자동 실행 결과입니다. 태스크별 계획·검토·완료 보고는 Obsidian 볼트의 task/autodev 문서를 참고하세요.\n\n" + \
           "\n".join(f"- {r['id']} {r['title']} ({r['commit']})" for r in run.results if r["status"] == "done") + \
           "\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
    r = sh(["gh", "pr", "create", "--base", "main", "--head", b,
            "--title", f"autodev: {run.project.name} 자동 작업", "--body", body], cwd=WORKTREE)
    log(f"PR: {(r.stdout or r.stderr).strip()[-200:]}")


def cmd_run(args) -> None:
    run = start_run(args)
    project = run.project
    max_tasks = float("inf") if run.forever else args.max_tasks
    max_splits = float("inf") if run.forever else args.max_splits
    max_blocked = max(args.max_blocked, 5) if run.forever else args.max_blocked
    splits = 0
    blocked_streak = 0
    error_streak = 0
    topic = args.topic
    if run.forever:
        log("무한 모드: 시간·태스크 수·주간 예산 상한 없음. 한도에 닿으면 리셋까지 기다렸다가 이어갑니다.")
    try:
        while len(run.results) < max_tasks:
            try:
                run.check_stop()
                if topic:
                    do_split(run, topic)
                    splits += 1
                    topic = None
                sync_base()                     # 다른 세션이 그사이 커밋한 내용을 따라간다
                task = next_task(project.tasks())
                if not task:
                    if splits >= max_splits:
                        raise StopRun("실행 가능한 태스크 없음 (분해 횟수 상한)")
                    splits += 1
                    log("큐가 비었습니다 — 프로젝트 TODO 에서 다음 작업을 분해합니다")
                    if do_split(run, None) == 0:
                        raise StopRun("자동으로 진행할 수 있는 작업이 더 없음 (완료 또는 사람 조치 대기)")
                    continue
                result = run_task(run, task)
                run.results.append(result)
                append_progress(run, result)
                error_streak = 0
                blocked_streak = blocked_streak + 1 if result["status"] == "blocked" else 0
                if blocked_streak >= max_blocked:
                    raise StopRun(f"태스크 {blocked_streak}개 연속 중단 — 공통 원인 점검 필요")
            except (StopRun, KeyboardInterrupt):
                raise
            except Exception as e:               # 무한 모드에서는 일시적 오류로 죽지 않는다
                if not run.forever:
                    raise
                error_streak += 1
                import traceback
                log(f"⚠ 예기치 않은 오류 ({error_streak}/5): {e}\n{traceback.format_exc()[-1500:]}")
                if error_streak >= 5:
                    raise StopRun(f"오류 5회 연속: {e}")
                time.sleep(300)
        else:
            run.stop_reason = f"최대 태스크 수({args.max_tasks}) 도달"
    except StopRun as e:
        run.stop_reason = str(e)
    except KeyboardInterrupt:
        run.stop_reason = "사용자 중단 (Ctrl-C)"
    finish_run(run, pr=args.pr)


def cmd_task(args) -> None:
    run = start_run(args)
    matches = [t for t in run.project.tasks() if t["id"] == args.id]
    if not matches:
        sys.exit(f"태스크를 찾을 수 없습니다: {args.id}")
    try:
        run.results.append(run_task(run, matches[0]))
    except StopRun as e:
        run.stop_reason = str(e)
    except KeyboardInterrupt:
        run.stop_reason = "사용자 중단 (Ctrl-C)"
    finish_run(run)


def cmd_split(args) -> None:
    run = start_run(args)
    try:
        do_split(run, args.topic)
    except StopRun as e:
        log(f"중단: {e}")


def cmd_status(args) -> None:
    names = [args.project] if args.project else all_projects()
    icon = {"todo": "□", "planned": "◧", "in-progress": "▶", "done": "✓", "blocked": "✗", "manual": "☝"}
    for name in names:
        project = Project(name)
        tasks = project.tasks()
        counts: dict[str, int] = {}
        for t in tasks:
            counts[str(t.get("status"))] = counts.get(str(t.get("status")), 0) + 1
        summary = " · ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "태스크 없음"
        print(f"{name}: {summary}")
        if args.brief:
            nxt = next_task(tasks)
            if nxt:
                print(f"  다음: {nxt['id']} {nxt.get('title')}")
            continue
        for t in tasks:
            if t.get("status") == "done" and not args.all:
                continue
            deps = f" ← {', '.join(t['depends_on'])}" if t.get("depends_on") else ""
            print(f"  {icon.get(str(t.get('status')), '?')} {t['id']} p{t.get('priority', '?')} {t.get('title')}{deps}")


def cmd_quota(args) -> None:
    snap = fresh_snapshot(force=not args.cached)
    print(fmt_quota(snap))
    if not snap:
        sys.exit(2)
    five = (snap.get("five_hour") or {}).get("pct") or 0
    week = (snap.get("seven_day") or {}).get("pct") or 0
    sys.exit(1 if five >= LIMIT_5H or week >= LIMIT_7D else 0)


def cmd_stop(args) -> None:
    STOP_FILE.write_text(f"{now():%Y-%m-%d %H:%M}\n", encoding="utf-8")
    print("STOP 요청됨 — 현재 단계가 끝나면 루프가 멈춥니다.")


# ── 대시보드 (로컬 웹 페이지, 실행 중인 루프와 별개 프로세스) ────────────────
def dash_html() -> str:
    import html
    esc = html.escape
    alive = sh(["pgrep", "-f", "autodev.py run"]).returncode == 0
    logs = sorted((TOOL_DIR / "logs").glob("*.out"), key=lambda f: f.stat().st_mtime)
    lines = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines() if logs else []
    main = [l for l in lines if not l.startswith("    · ")]
    phase, tid, since, action = "", "", "", ""
    for l in reversed(lines):
        if not action and l.startswith("    · "):
            action = l[6:]
        m = re.match(r"\[(\d\d:\d\d:\d\d)\] ▶ (\w+) (\S+)", l)
        if m:
            since, phase, tid = m.groups()
            break
    wait = next((l for l in reversed(main[-3:]) if "⏸" in l), "")
    snap = load_snapshot() or {}

    def bar(pct, label, extra=""):
        if pct is None:
            return ""
        color = "var(--bad)" if pct >= 85 else "var(--warn)" if pct >= 60 else "var(--ok)"
        return (f'<div class="q"><div class="ql"><span>{label}</span><span>{pct:.0f}% {esc(extra)}</span></div>'
                f'<div class="track"><div class="fill" style="width:{min(pct, 100):.0f}%;background:{color}"></div></div></div>')

    quota = "".join(bar((snap.get(k) or {}).get("pct"), lab, "· 리셋 " + fmt_reset((snap.get(k) or {}).get("resets_at")))
                    for k, lab in (("five_hour", "5시간 창"), ("seven_day", "주간 창")))
    label = {"in-progress": "진행 중", "planned": "진행 중", "todo": "대기", "done": "완료",
             "blocked": "중단", "manual": "사람 할 일"}
    order = ["in-progress", "planned", "todo", "blocked", "manual", "done"]
    cards = ""
    for name in all_projects():
        tasks = Project(name).tasks()
        if not tasks:
            continue
        done = sum(1 for t in tasks if t.get("status") == "done")
        pct = done * 100 / len(tasks)
        rows = ""
        for t in sorted(tasks, key=lambda t: (order.index(t.get("status")) if t.get("status") in order else 9, t["id"])):
            st = str(t.get("status"))
            cur = f' <b class="now">{esc(phase)}</b>' if t["id"] == tid and alive and st != "done" else ""
            rows += (f'<tr class="s-{esc(st)}"><td><span class="chip">{esc(label.get(st, st))}</span></td>'
                     f'<td class="id">{esc(str(t["id"]))}</td><td>{esc(str(t.get("title", "")))}{cur}</td></tr>')
        cards += (f'<section><h2>{esc(name)} <small>{done}/{len(tasks)} 완료</small></h2>'
                  f'<div class="track big"><div class="fill" style="width:{pct:.0f}%;background:var(--ok)"></div></div>'
                  f'<table>{rows}</table></section>')
    state = ('<span class="badge wait">한도 대기 중</span>' if alive and wait else
             '<span class="badge run">실행 중</span>' if alive else '<span class="badge stop">멈춤</span>')
    nowline = (f"{esc(tid)} · <b>{esc(phase)}</b> 단계 ({esc(since)} 시작)" if alive and phase else
               esc(main[-1][:200]) if main else "기록 없음")
    tail = "\n".join(esc(l[:220]) for l in main[-14:])
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>autodev 진행 상황</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1c1f24;--mut:#6b7280;--line:#e5e7eb;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--acc:#2563eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111318;--card:#1b1e25;--fg:#e6e8ec;--mut:#9aa3b2;--line:#2a2f3a}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,sans-serif}}
main{{max-width:880px;margin:0 auto;display:grid;gap:12px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
h1{{font-size:18px;margin:0 0 6px}} h2{{font-size:15px;margin:0 0 8px}} small{{color:var(--mut);font-weight:400}}
.badge{{font-size:12px;padding:2px 9px;border-radius:99px;color:#fff;vertical-align:middle;margin-left:6px}}
.run{{background:var(--ok)}} .wait{{background:var(--warn)}} .stop{{background:var(--mut)}}
.act{{color:var(--mut);font-size:12px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.track{{height:8px;background:var(--line);border-radius:99px;overflow:hidden}} .big{{height:12px;margin-bottom:10px}}
.fill{{height:100%;border-radius:99px}} .q{{margin-top:8px}} .ql{{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}}
table{{width:100%;border-collapse:collapse}} td{{padding:5px 6px;border-top:1px solid var(--line);vertical-align:top}}
td.id{{color:var(--mut);white-space:nowrap;font-size:12px}} .chip{{font-size:11px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);white-space:nowrap}}
.s-done td{{color:var(--mut)}} .s-done .chip{{color:var(--ok);border-color:var(--ok)}}
.s-in-progress .chip,.s-planned .chip{{background:var(--acc);color:#fff;border-color:var(--acc)}}
.s-blocked .chip,.s-manual .chip{{color:var(--bad);border-color:var(--bad)}}
.now{{color:var(--acc);font-size:12px;margin-left:6px}}
pre{{margin:0;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all;color:var(--mut)}}
</style></head><body><main>
<section><h1>autodev 진행 상황 {state}</h1><div>{nowline}</div>
<div class="act">{esc(action) if alive else ""}</div>{quota}
<div class="act">갱신 {now():%H:%M:%S} · 5초마다 자동 새로고침</div></section>
{cards}
<section><h2>최근 로그</h2><pre>{tail}</pre></section>
</main></body></html>"""


def cmd_dash(args) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                body = dash_html().encode("utf-8")
            except Exception as e:      # 페이지 생성 오류로 서버가 죽지 않게
                body = f"<pre>dashboard error: {e}</pre>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    url = f"http://127.0.0.1:{args.port}"
    print(f"대시보드: {url}  (Ctrl-C 로 종료)", flush=True)
    if not args.no_open:
        sh(["open", url])
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


# ── 세션 인수인계 (훅에서 호출) ──────────────────────────────────────────────
def repo_root(cwd: str) -> Path:
    r = sh(["git", "rev-parse", "--show-toplevel"], cwd=Path(cwd) if Path(cwd).exists() else None)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else Path(cwd)


def transcript_text(path: Path, limit: int = 160_000) -> str:
    """트랜스크립트(jsonl)에서 사람이 읽을 수 있는 대화 흐름만 뽑는다."""
    chunks: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") not in ("user", "assistant") or ev.get("isSidechain"):
                continue
            content = (ev.get("message") or {}).get("content")
            who = "사용자" if ev["type"] == "user" else "Claude"
            if isinstance(content, str):
                chunks.append(f"[{who}] {content[:4000]}")
                continue
            for b in content or []:
                if b.get("type") == "text":
                    chunks.append(f"[{who}] {b.get('text', '')[:4000]}")
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    hint = inp.get("file_path") or inp.get("command") or inp.get("description") or ""
                    chunks.append(f"[도구] {b.get('name')} {str(hint)[:200]}")
                elif b.get("type") == "tool_result":
                    res = b.get("content")
                    if isinstance(res, list):
                        res = " ".join(x.get("text", "") for x in res if isinstance(x, dict))
                    tag = "도구 오류" if b.get("is_error") else "도구 결과"
                    chunks.append(f"[{tag}] {str(res or '').strip()[:400]}")
    return "\n".join(chunks)[-limit:]


def cmd_handoff(args) -> None:
    tp = Path(args.transcript)
    if not tp.exists():
        return
    root = repo_root(args.cwd)
    convo = transcript_text(tp)
    if len(convo) < 1500:
        return
    prompt = render("handoff", today=f"{now():%Y-%m-%d %H:%M}", cwd=args.cwd) + "\n\n<transcript>\n" + convo + "\n</transcript>\n"
    cmd = [CLAUDE, "-p", "--model", MODEL["handoff"], "--tools", "", "--disable-slash-commands",
           "--no-session-persistence", "--output-format", "json", "--max-turns", "1", *NO_MCP]
    p = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=600,
                       env={**os.environ, "AUTODEV": "1"}, cwd=str(Path.home()))
    try:
        res = json.loads(p.stdout)
    except ValueError:
        return
    body = (res.get("result") or "").strip()
    if res.get("is_error") or len(body) < 100:
        return
    stamp = now()
    doc = (f"---\ntags: [업무자동화, handoff]\ncreated: {stamp:%Y-%m-%d %H:%M}\n"
           f"repo: {root.name}\nsession: {args.session}\ntrigger: {args.reason}\n---\n\n{body}\n")
    dest = root / ".claude" / "handoff"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "current.md").write_text(doc, encoding="utf-8")
    try:
        copy_dir = AUTO_VAULT / "handoff" / root.name
        copy_dir.mkdir(parents=True, exist_ok=True)
        (copy_dir / f"handoff-{stamp:%Y-%m-%d-%H%M}.md").write_text(doc, encoding="utf-8")
    except OSError:
        pass


def cmd_hook(args) -> None:
    if os.environ.get("AUTODEV"):      # 헤드리스 드라이버·요약 호출에서는 훅을 건너뛴다 (재귀 방지)
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    cwd = data.get("cwd") or os.getcwd()
    if args.event == "session-start":
        root = repo_root(cwd)
        cur = root / ".claude" / "handoff" / "current.md"
        if cur.exists() and time.time() - cur.stat().st_mtime < 14 * 86400:
            print("=== 이전 세션 핸드오프 (자동 주입 · 사용자가 다른 일을 시키면 그 지시가 우선) ===")
            print(cur.read_text(encoding="utf-8")[:6000])
            print(f"(과거 핸드오프: {AUTO_VAULT / 'handoff' / root.name})")
        try:
            lines = []
            for name in all_projects():
                project = Project(name)
                tasks = project.tasks()
                if not tasks:
                    continue
                nxt = next_task(tasks)
                need = sum(1 for t in tasks if t.get("status") in ("blocked", "manual"))
                lines.append(f"- {name}: 전체 {len(tasks)} · 사람 조치 필요 {need} · 다음 {nxt['id'] + ' ' + str(nxt.get('title')) if nxt else '없음'}")
            if lines:
                print("=== autodev 태스크 큐 ===")
                print("\n".join(lines))
                print(f"(실행 기록: {AUTO_VAULT / 'runs'} · 사용법: {TOOL_DIR / 'README.md'})")
        except (OSError, SystemExit):
            pass
        return
    # pre-compact / session-end → 백그라운드로 핸드오프 문서 생성 (세션을 붙잡지 않는다)
    tp = data.get("transcript_path")
    if not tp or not Path(tp).exists() or Path(tp).stat().st_size < 40_000:
        return
    reason = data.get("trigger") or data.get("reason") or args.event
    log_dir = TOOL_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    with (log_dir / "handoff.log").open("a") as lf:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "handoff",
                          "--transcript", tp, "--cwd", cwd, "--session", data.get("session_id", ""),
                          "--reason", f"{args.event}:{reason}"],
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True,
                         env={**os.environ, "AUTODEV": "1"})


# ── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(prog="autodev", description="Claude Code 자율 개발 루프")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def run_opts(p):
        p.add_argument("--project", "-P", required=True, help="autodev/projects/<이름>.json")
        p.add_argument("--max-hours", type=float, default=10, help="최대 실행 시간 (기본 10)")
        p.add_argument("--weekly-budget", type=float, default=25,
                       help="이번 실행이 쓸 수 있는 주간 사용량 %%p (기본 25)")
        p.add_argument("--no-wait", action="store_true", help="5시간 창이 차면 기다리지 않고 종료")
        p.add_argument("--no-push", action="store_true", help="커밋만 하고 push 하지 않음")

    p = sub.add_parser("run", help="큐가 빌 때까지(또는 사용량·시간 상한까지) 태스크를 연속 처리")
    run_opts(p)
    p.add_argument("--topic", "-t", help="새 주제 — 먼저 태스크로 분해한 뒤 실행")
    p.add_argument("--max-tasks", type=int, default=30)
    p.add_argument("--max-splits", type=int, default=3, help="한 실행에서 자동 분해 최대 횟수")
    p.add_argument("--max-blocked", type=int, default=3, help="연속 중단 허용 개수")
    p.add_argument("--pr", action="store_true", help="끝나면 main 대상 PR 생성")
    p.add_argument("--forever", action="store_true",
                   help="상한 없이 계속: 할 일이 없어질 때까지 돌고, 한도에 닿으면 리셋까지 대기, 끝나면 알람")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("task", help="태스크 1개만 실행")
    run_opts(p)
    p.add_argument("--id", required=True)
    p.set_defaults(fn=cmd_task)

    p = sub.add_parser("split", help="주제(또는 프로젝트 TODO)를 태스크 파일로 분해만 함")
    run_opts(p)
    p.add_argument("--topic", "-t")
    p.set_defaults(fn=cmd_split)

    p = sub.add_parser("status", help="태스크 큐 현황")
    p.add_argument("--project", "-P")
    p.add_argument("--all", action="store_true", help="완료된 태스크도 표시")
    p.add_argument("--brief", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("quota", help="남은 사용량과 리셋 시각 (종료코드 0=여유, 1=상한 도달, 2=알 수 없음)")
    p.add_argument("--cached", action="store_true", help="새로 측정하지 않고 최근 스냅샷 사용")
    p.set_defaults(fn=cmd_quota)

    p = sub.add_parser("dash", help="진행 상황 대시보드를 브라우저로 띄움 (루프와 별개로 실행)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(fn=cmd_dash)

    p = sub.add_parser("stop", help="실행 중인 루프를 현재 단계 뒤에 멈춤")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("hook", help="(내부) Claude Code 훅 진입점")
    p.add_argument("event", choices=["session-start", "pre-compact", "session-end"])
    p.set_defaults(fn=cmd_hook)

    p = sub.add_parser("handoff", help="(내부) 트랜스크립트 → 핸드오프 문서")
    p.add_argument("--transcript", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--session", default="")
    p.add_argument("--reason", default="manual")
    p.set_defaults(fn=cmd_handoff)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
