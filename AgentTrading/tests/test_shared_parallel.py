"""shared.parallel.ordered_pool_map — 완료 순서와 무관한 입력 순서 산출, 빈 입력."""

import time

from src.shared.parallel import ordered_pool_map

_OFFSET = {}


def _init(offset):
    _OFFSET["v"] = offset


def _slow_even(x):  # spawn 워커가 import 하므로 모듈 최상위 함수
    if x % 2 == 0:
        time.sleep(0.05)
    return x * 10 + _OFFSET.get("v", 0)


def test_yields_in_input_order():
    items = list(range(9))
    assert list(ordered_pool_map(_slow_even, items, 2, initializer=_init, initargs=(1,))) == \
        [x * 10 + 1 for x in items]


def test_empty_input_yields_nothing():
    assert list(ordered_pool_map(_slow_even, [], 2)) == []
