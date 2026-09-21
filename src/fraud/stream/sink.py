"""Parquet sink with a watermark, one writer per directory (PLAN §9.4, §3.7).

The sink buffers scored rows and turns them into Parquet. Its real job is the ordering
guarantee around the acknowledgement: the scorer may only ``XACK`` a message once that
message's row is durable, so ``flush`` hands back exactly the ids it wrote. Acking first
and writing afterwards would turn every crash into silently missing output — the message
is gone from the pending list, so nothing will ever redeliver it.

The reverse — writing, then crashing before the ack — is deliberately allowed. §9.3 calls
it out: the redelivered event returns its stored feature record, so the row written the
second time is identical to the first, and the DuckDB view in `storage/duck.py`
deduplicates on ``txn_id``. Cheap idempotency in exchange for a duplicate that readers
already resolve.

**Partitioning follows event date, not wall-clock date.** A flush spanning midnight in
event time writes one file per date (§9.4), because the partition has to mean the same
thing whether the data arrived live or through a replay at 3600x.

**Invariant 7: one writer per directory.** Files are named after the consumer, so two
scorers never contend. The sequence counter resumes from what is already on disk — a
restarted consumer beginning again at 000000 would overwrite its predecessor's output,
which loses scored rows silently.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import pandas as pd

from fraud.config import Settings, load_yaml
from fraud.storage.parquet_io import write_atomic

logger = logging.getLogger(__name__)

PARTITION: Final[str] = "date={}"
PART_NAME: Final[str] = "part-{consumer}-{seq:06d}.parquet"
_SEQ_PATTERN: Final[str] = r"part-{consumer}-(\d{{6}})\.parquet$"


@dataclass(frozen=True, slots=True)
class FlushResult:
    """What one flush produced, and what the scorer may now acknowledge."""

    paths: list[Path] = field(default_factory=list)
    message_ids: list[str] = field(default_factory=list)
    rows: int = 0
    watermark: datetime | None = None

    def __bool__(self) -> bool:
        return self.rows > 0


class ParquetSink:
    """Buffers scored rows, writes them atomically, and reports a watermark."""

    __slots__ = (
        "_buffer",
        "_clock",
        "_consumer",
        "_ids",
        "_last_flush",
        "_max_rows",
        "_max_seconds",
        "_root",
        "_seq",
    )

    def __init__(
        self,
        consumer: str,
        root: Path | None = None,
        *,
        max_rows: int | None = None,
        max_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        policy = load_yaml("stream")["sink"]
        self._consumer = consumer
        self._root = root or Settings.from_env().scored_dir
        self._max_rows = int(policy["max_rows"]) if max_rows is None else max_rows
        self._max_seconds = float(policy["max_seconds"]) if max_seconds is None else max_seconds
        self._clock = clock
        self._buffer: list[dict[str, Any]] = []
        self._ids: list[str] = []
        self._last_flush = clock()
        self._seq = self._next_sequence()

    def __len__(self) -> int:
        return len(self._buffer)

    @property
    def consumer(self) -> str:
        return self._consumer

    @property
    def root(self) -> Path:
        return self._root

    # --- buffering ---

    def append(self, rows: Iterable[dict[str, Any]], message_ids: Sequence[str]) -> None:
        """Buffer scored rows together with the stream ids that produced them.

        The two are kept side by side rather than zipped into the rows because the ids
        are transport state, not data: they must reach the ``XACK`` but must never end
        up in a Parquet column.
        """
        buffered = list(rows)
        if len(buffered) != len(message_ids):
            raise ValueError(
                f"{len(buffered)} rows but {len(message_ids)} message ids; the sink acks "
                "exactly what it wrote, so these cannot drift apart (PLAN §9.2)"
            )
        self._buffer.extend(buffered)
        self._ids.extend(message_ids)

    def should_flush(self) -> bool:
        """>= max_rows buffered, or max_seconds since the last flush (PLAN §9.2)."""
        if not self._buffer:
            return False
        if len(self._buffer) >= self._max_rows:
            return True
        return (self._clock() - self._last_flush) >= self._max_seconds

    # --- writing ---

    def flush(self) -> FlushResult:
        """Write the buffer, one file per event date, and return what was written."""
        if not self._buffer:
            self._last_flush = self._clock()
            return FlushResult()

        frame = pd.DataFrame(self._buffer)
        frame["event_time"] = pd.to_datetime(frame["event_time"])

        paths: list[Path] = []
        # Sorting by date keeps file sequence numbers in date order, which makes a
        # directory listing readable; groupby alone does not promise an order.
        for day, chunk in sorted(frame.groupby(frame["event_time"].dt.date), key=lambda kv: kv[0]):
            paths.append(write_atomic(chunk.reset_index(drop=True), self._path_for(day)))

        # Valid because processing is ordered (§9.4): nothing older can still arrive.
        watermark = frame["event_time"].max().to_pydatetime()
        result = FlushResult(
            paths=paths, message_ids=list(self._ids), rows=len(frame), watermark=watermark
        )

        self._buffer.clear()
        self._ids.clear()
        self._last_flush = self._clock()
        logger.info(
            "flushed %d rows to %d file(s), watermark %s", result.rows, len(paths), watermark
        )
        return result

    def _path_for(self, day: date) -> Path:
        path = (
            self._root
            / PARTITION.format(day.isoformat())
            / PART_NAME.format(consumer=self._consumer, seq=self._seq)
        )
        self._seq += 1
        return path

    def _next_sequence(self) -> int:
        """Resume numbering after a restart instead of overwriting earlier output."""
        if not self._root.is_dir():
            return 0
        pattern = re.compile(_SEQ_PATTERN.format(consumer=re.escape(self._consumer)))
        highest = -1
        for existing in self._root.glob(f"*/part-{self._consumer}-*.parquet"):
            found = pattern.search(existing.name)
            if found:
                highest = max(highest, int(found.group(1)))
        if highest >= 0:
            logger.info("resuming %s file numbering at %06d", self._consumer, highest + 1)
        return highest + 1
