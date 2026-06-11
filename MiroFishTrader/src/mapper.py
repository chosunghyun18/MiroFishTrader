"""테마/엔티티 → 티커 정적 매핑.

`config/ticker_map.yaml`을 로드해 추출 신호의 themes/entities를 티커로 변환한다.
매칭 실패한 키워드는 `misses`로 돌려보내 사전 보강에 활용한다 (대소문자 무시).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import yaml

from .models import ExtractedSignal

logger = logging.getLogger(__name__)


@dataclass
class TickerMap:
    """정규화된(소문자 키) 매핑 사전."""

    themes: Dict[str, List[str]] = field(default_factory=dict)
    entities: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TickerMap":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        themes = {
            str(k).strip().lower(): [str(t).strip() for t in (v or [])]
            for k, v in (raw.get("themes") or {}).items()
        }
        entities = {
            str(k).strip().lower(): str(v).strip()
            for k, v in (raw.get("entities") or {}).items()
        }
        return cls(themes=themes, entities=entities)


@dataclass
class MappingResult:
    tickers: List[str] = field(default_factory=list)
    misses: List[str] = field(default_factory=list)


def map_signal(signal: ExtractedSignal, ticker_map: TickerMap) -> MappingResult:
    """신호의 themes/entities → 티커 목록. 미매칭은 misses로."""
    tickers: List[str] = []
    misses: List[str] = []

    def _add(values: List[str]) -> None:
        for tk in values:
            if tk and tk not in tickers:
                tickers.append(tk)

    for theme in signal.themes:
        hit = ticker_map.themes.get(theme.strip().lower())
        if hit:
            _add(hit)
        else:
            misses.append(theme)

    for entity in signal.entities:
        hit = ticker_map.entities.get(entity.name.strip().lower())
        if hit:
            _add([hit])
        else:
            misses.append(entity.name)

    if misses:
        logger.info("티커 매핑 실패 키워드(사전 보강 필요): %s", misses)
    return MappingResult(tickers=tickers, misses=misses)
