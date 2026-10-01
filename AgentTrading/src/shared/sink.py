"""트리거별 라운드트립 parquet 스트리밍 쓰기 싱크 — 분석·백테스트 CLI 공용.

run 마다 `write(strategy_id, df)` 로 `<parquet>.tmp` 에 이어 쓰고, 전체 성공 후 `commit()` 에서 일괄 rename 한다.
메모리 상한 = 행 그룹 버퍼(`row_group_rows` 행) + 호출자가 들고 있는 run 1개. 전체 라운드트립을 모으지 않는다.
분석(`src.analysis.run`)·백테스트(`src.backtest.run`) 양쪽이 쓰므로 순환 import 를 피해 여기 둔다.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.shared.schema import TableSchema, empty_frame

ROW_GROUP_ROWS = 100_000  # 트리거별 버퍼가 이 행 수에 닿으면 행 그룹 하나로 flush


class RoundtripSink:
    """트리거별 라운드트립을 run 단위로 `<parquet>.tmp` 에 이어 쓰고, `commit()` 에서 일괄 rename.

    `write(sid, df)` 는 `(strategy_id, param_id, trade_id)` 정렬 순서로 불려야 한다(같은 sid 는 연속).
    동시에 열린 writer 는 1개, 버퍼는 `row_group_rows` 행. 0행 df 만 받은 트리거는 0행 parquet 가 된다
    (`write_empty_row_group` 이면 빈 행 그룹 1개 — `DataFrame.to_parquet` 의 0행 출력과 바이트 동일, 아니면 행 그룹 0개).
    `validate` 는 0행이 아닌 df 마다 run 단위로 부른다.
    with 블록에서 예외가 나거나 commit 전에 빠져나오면 `abort()` 가 이번 실행의 `.tmp` 를 모두 지운다.
    """

    def __init__(self, paths: Mapping[str, Path], schema: TableSchema,
                 validate: Callable[[pd.DataFrame], object], *, row_group_rows: int = ROW_GROUP_ROWS,
                 write_empty_row_group: bool = False):
        self.paths = dict(paths)
        self.schema = pa.Schema.from_pandas(empty_frame(schema), preserve_index=False)
        self._validate = validate
        self._row_group_rows = row_group_rows
        self._write_empty_row_group = write_empty_row_group
        self._tmps: dict[str, Path] = {}
        self._sid: str | None = None
        self._writer: pq.ParquetWriter | None = None
        self._sid_rows = 0
        self._buf: list[pa.Table] = []
        self._buf_rows = 0
        self._committed = False

    def write(self, sid: str, df: pd.DataFrame) -> None:
        if sid != self._sid:
            if sid in self._tmps:
                raise ValueError(f"strategy_id {sid!r} 가 연속되지 않는다(정렬 순서로 써야 한다)")
            self._close()
            tmp = self.paths[sid].with_name(self.paths[sid].name + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            self._tmps[sid] = tmp
            self._writer = pq.ParquetWriter(tmp, self.schema, compression="snappy")
            self._sid, self._sid_rows = sid, 0
        if len(df):
            self._validate(df)
            self._buf.append(pa.Table.from_pandas(df, schema=self.schema, preserve_index=False))
            self._buf_rows += len(df)
            self._sid_rows += len(df)
            if self._buf_rows >= self._row_group_rows:
                self._flush()

    def _flush(self) -> None:
        if self._buf:
            self._writer.write_table(pa.concat_tables(self._buf))
            self._buf, self._buf_rows = [], 0

    def _close(self) -> None:
        if self._writer is not None:
            try:
                self._flush()
                if self._sid_rows == 0 and self._write_empty_row_group:
                    self._writer.write_table(self.schema.empty_table())
            finally:
                self._writer.close()
                self._writer, self._sid = None, None

    def commit(self) -> None:
        self._close()
        for sid, tmp in self._tmps.items():
            os.replace(tmp, self.paths[sid])
        self._committed = True

    def abort(self) -> None:
        self._buf, self._buf_rows = [], 0
        try:
            if self._writer is not None:
                self._writer.close()
        finally:
            self._writer, self._sid = None, None
            for tmp in self._tmps.values():
                tmp.unlink(missing_ok=True)

    def __enter__(self) -> RoundtripSink:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None or not self._committed:
            self.abort()
