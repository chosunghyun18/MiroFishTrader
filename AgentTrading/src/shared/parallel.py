"""정렬 순서 슬라이딩 윈도우 프로세스 풀 — 분석·백테스트 그리드 CLI `--jobs N` 공용.

spawn `ProcessPoolExecutor` 에서 `fn(item)` 을 계산하되 결과는 입력 순서 그대로 하나씩 내놓는다. 미완료 future 는
최대 `WINDOW_PER_JOB × jobs` 개라 메모리 상한이 입력 수와 무관하다. 쓰기(싱크)는 호출자(메인 프로세스)의 몫이다.
"""

from __future__ import annotations

import itertools
import multiprocessing
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor

WINDOW_PER_JOB = 2  # 병렬 실행 시 미완료 run 상한 = WINDOW_PER_JOB × jobs (메모리 상한이 run 수와 무관)


def ordered_pool_map(fn: Callable, items: Iterable, jobs: int, *, initializer: Callable | None = None,
                     initargs: tuple = ()) -> Iterator:
    """spawn 프로세스 풀에서 `fn(item)` 을 계산해 `items` 순서 그대로 결과를 하나씩 내놓는다(제너레이터).

    `fn`·`initializer` 는 spawn 워커가 import 할 수 있는 모듈 최상위 함수여야 한다(피클 가능). 공통 큰 입력(bars 등)은
    `initializer(*initargs)` 로 워커당 1회만 넘긴다. 미완료 future 를 최대 `WINDOW_PER_JOB × jobs` 개 제출하고 맨 앞
    future 의 결과를 기다려 내놓은 뒤 하나 더 제출한다(완료 순서 무시). 워커 예외는 `.result()` 로 그대로 전파되고,
    예외·`close()` 등 정상 종료가 아니면 남은 태스크를 취소한다.
    """
    ctx = multiprocessing.get_context("spawn")
    window = WINDOW_PER_JOB * jobs
    ex = ProcessPoolExecutor(max_workers=jobs, mp_context=ctx, initializer=initializer, initargs=initargs)
    pending: deque = deque()
    it = iter(items)
    ok = False
    try:
        for item in itertools.islice(it, window):
            pending.append(ex.submit(fn, item))
        while pending:
            result = pending.popleft().result()
            for nxt in itertools.islice(it, 1):
                pending.append(ex.submit(fn, nxt))
            yield result
        ok = True
    finally:
        ex.shutdown(wait=True, cancel_futures=not ok)
