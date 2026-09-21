"""The Parquet sink, atomic writes and the deduplicating reader (PLAN §9.3, §9.4, §13).

Three properties here are the ones a crash exposes, and none of them fail loudly.

*Atomic writes.* A reader that catches a file mid-write does not get bad data, it gets an
exception — and the dashboard is down for as long as the write lasts. The temporary-file
dance is what keeps `*.parquet` meaning "complete".

*Ack ordering.* `flush` returns exactly the ids it made durable, so the scorer can only
acknowledge what survives a crash. Getting this backwards loses output permanently: an
acked message is gone from the pending list and nothing will redeliver it.

*Deduplication.* §9.3 deliberately allows a duplicate row when a crash lands between the
flush and the ack. The reader resolves it; these tests check the view actually does.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from fraud.storage import duck
from fraud.storage.parquet_io import TEMP_SUFFIX, cleanup_temp_files, write_atomic
from fraud.stream.sink import FlushResult, ParquetSink

START = datetime(2026, 3, 14, 10, 0, 0)  # noqa: DTZ001 - naive IST, per PLAN §3.5


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _row(index: int, moment: datetime | None = None, **overrides: Any) -> dict[str, Any]:
    row = {
        "txn_id": f"T{index:07d}",
        "event_time": moment or START + timedelta(seconds=index),
        "account_id": f"A{index % 3:07d}",
        "merchant_id": f"M{index % 2:06d}",
        "amount": 100.0 + index,
        "risk": 0.1 * (index % 10),
        "decision": "REVIEW" if index % 5 == 0 else "ALLOW",
        "p_xgb": 0.2,
        "anomaly_pct": 0.3,
        "ingest_ts": 1_700_000_000_000 + index,
        "scored_ts": 1_700_000_000_500 + index,
        "model_version": "v1",
        "feature_spec_version": "fs1",
        "consumer": "scorer-1",
    }
    row.update(overrides)
    return row


def _rows(count: int, start: int = 0, **overrides: Any) -> list[dict[str, Any]]:
    return [_row(index, **overrides) for index in range(start, start + count)]


def _ids(count: int, start: int = 0) -> list[str]:
    return [f"{1700000000000 + index}-0" for index in range(start, start + count)]


def _sink(tmp_path: Path, clock: FakeClock | None = None, **kwargs: Any) -> ParquetSink:
    return ParquetSink("scorer-1", tmp_path, clock=clock or FakeClock(), **kwargs)


# --- atomic writes (PLAN §9.4) ------------------------------------------------------


def test_write_atomic_leaves_no_temp_file(tmp_path: Path) -> None:
    path = write_atomic(pd.DataFrame({"a": [1, 2, 3]}), tmp_path / "part.parquet")
    assert path.is_file()
    assert list(tmp_path.glob(f"*{TEMP_SUFFIX}")) == []


def test_write_atomic_creates_parent_directories(tmp_path: Path) -> None:
    path = write_atomic(pd.DataFrame({"a": [1]}), tmp_path / "date=2026-03-14" / "part.parquet")
    assert path.is_file()


def test_a_failed_write_publishes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the temp file: a crash mid-write must not create a readable name."""
    import os

    def explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(pd.DataFrame({"a": [1]}), tmp_path / "part.parquet")

    assert not (tmp_path / "part.parquet").exists()
    assert list(tmp_path.glob(f"*{TEMP_SUFFIX}")) == []  # and no litter left behind


def test_temp_files_are_invisible_to_the_reader_glob(tmp_path: Path) -> None:
    """Readers glob *.parquet, so `.parquet.tmp` cannot match by construction."""
    (tmp_path / "date=2026-03-14").mkdir(parents=True)
    (tmp_path / "date=2026-03-14" / f"part-x-000000.parquet{TEMP_SUFFIX}").write_text("junk")
    assert list(tmp_path.glob("*/*.parquet")) == []


def test_cleanup_removes_orphans(tmp_path: Path) -> None:
    (tmp_path / "date=2026-03-14").mkdir(parents=True)
    (tmp_path / "date=2026-03-14" / f"part-x-000000.parquet{TEMP_SUFFIX}").write_text("junk")
    write_atomic(pd.DataFrame({"a": [1]}), tmp_path / "date=2026-03-14" / "part-x-000001.parquet")

    assert cleanup_temp_files(tmp_path) == 1
    assert len(list(tmp_path.glob("*/*.parquet"))) == 1


# --- flush policy (PLAN §9.2) -------------------------------------------------------


def test_nothing_buffered_means_nothing_to_flush(tmp_path: Path) -> None:
    assert _sink(tmp_path).should_flush() is False


def test_row_threshold_triggers_a_flush(tmp_path: Path) -> None:
    sink = _sink(tmp_path, max_rows=5, max_seconds=999.0)
    sink.append(_rows(4), _ids(4))
    assert sink.should_flush() is False

    sink.append(_rows(1, start=4), _ids(1, start=4))
    assert sink.should_flush() is True


def test_time_threshold_triggers_a_flush(tmp_path: Path) -> None:
    """A quiet stream still has to reach the dashboard."""
    clock = FakeClock()
    sink = _sink(tmp_path, clock, max_rows=9999, max_seconds=2.0)
    sink.append(_rows(1), _ids(1))
    assert sink.should_flush() is False

    clock.advance(2.0)
    assert sink.should_flush() is True


def test_the_timer_restarts_after_a_flush(tmp_path: Path) -> None:
    clock = FakeClock()
    sink = _sink(tmp_path, clock, max_rows=9999, max_seconds=2.0)
    sink.append(_rows(1), _ids(1))
    clock.advance(2.0)
    sink.flush()

    sink.append(_rows(1, start=1), _ids(1, start=1))
    assert sink.should_flush() is False


def test_defaults_come_from_config(tmp_path: Path) -> None:
    from fraud.config import load_yaml

    policy = load_yaml("stream")["sink"]
    assert (policy["max_rows"], policy["max_seconds"]) == (2000, 2.0)


# --- flush behaviour ----------------------------------------------------------------


def test_flush_writes_one_file_and_clears_the_buffer(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    sink.append(_rows(3), _ids(3))
    result = sink.flush()

    assert result.rows == 3
    assert len(result.paths) == 1
    assert result.paths[0].is_file()
    assert len(sink) == 0
    assert bool(result) is True


def test_flush_returns_exactly_the_ids_it_wrote(tmp_path: Path) -> None:
    """The scorer acks these; anything else loses or double-acks messages (§9.2)."""
    sink = _sink(tmp_path)
    ids = _ids(4)
    sink.append(_rows(4), ids)
    assert sink.flush().message_ids == ids


def test_ids_do_not_leak_into_the_parquet(tmp_path: Path) -> None:
    """Stream ids are transport state, not data."""
    sink = _sink(tmp_path)
    sink.append(_rows(2), _ids(2))
    frame = pd.read_parquet(sink.flush().paths[0])
    assert "message_id" not in frame.columns
    assert "stream_id" not in frame.columns


def test_mismatched_rows_and_ids_are_refused(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    with pytest.raises(ValueError, match="cannot drift apart"):
        sink.append(_rows(3), _ids(2))


def test_flushing_an_empty_buffer_writes_nothing(tmp_path: Path) -> None:
    result = _sink(tmp_path).flush()
    assert result == FlushResult()
    assert list(tmp_path.rglob("*.parquet")) == []


def test_watermark_is_the_largest_event_time(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    rows = _rows(5)
    sink.append(rows, _ids(5))
    assert sink.flush().watermark == max(row["event_time"] for row in rows)


# --- date partitioning (PLAN §9.4) --------------------------------------------------


def test_rows_land_in_an_event_date_partition(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    sink.append(_rows(2), _ids(2))
    assert sink.flush().paths[0].parent.name == "date=2026-03-14"


def test_a_flush_spanning_midnight_writes_one_file_per_date(tmp_path: Path) -> None:
    """§9.4 explicitly: a batch crossing a date boundary splits."""
    sink = _sink(tmp_path)
    rows = [
        _row(0, datetime(2026, 3, 14, 23, 59, 30)),  # noqa: DTZ001
        _row(1, datetime(2026, 3, 14, 23, 59, 59)),  # noqa: DTZ001
        _row(2, datetime(2026, 3, 15, 0, 0, 1)),  # noqa: DTZ001
    ]
    sink.append(rows, _ids(3))
    result = sink.flush()

    assert sorted(path.parent.name for path in result.paths) == [
        "date=2026-03-14",
        "date=2026-03-15",
    ]
    assert result.rows == 3
    assert result.watermark == datetime(2026, 3, 15, 0, 0, 1)  # noqa: DTZ001


def test_partition_follows_event_time_not_wall_clock(tmp_path: Path) -> None:
    """A 3600x replay must partition the same way a live run would."""
    sink = _sink(tmp_path)
    sink.append([_row(0, datetime(2026, 3, 20, 4, 0, 0))], _ids(1))  # noqa: DTZ001
    assert sink.flush().paths[0].parent.name == "date=2026-03-20"


# --- file naming and restarts (invariant 7) -----------------------------------------


def test_files_are_named_for_the_consumer(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    sink.append(_rows(1), _ids(1))
    assert sink.flush().paths[0].name == "part-scorer-1-000000.parquet"


def test_sequence_increments_within_a_run(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    names = []
    for index in range(3):
        sink.append(_rows(1, start=index), _ids(1, start=index))
        names.append(sink.flush().paths[0].name)

    assert names == [
        "part-scorer-1-000000.parquet",
        "part-scorer-1-000001.parquet",
        "part-scorer-1-000002.parquet",
    ]


def test_a_restarted_sink_does_not_overwrite_earlier_output(tmp_path: Path) -> None:
    """Restarting at 000000 would silently destroy the previous run's scored rows."""
    first = _sink(tmp_path)
    first.append(_rows(2), _ids(2))
    first.flush()

    restarted = _sink(tmp_path)
    restarted.append(_rows(2, start=2), _ids(2, start=2))
    restarted.flush()

    assert len(list(tmp_path.glob("*/*.parquet"))) == 2


def test_two_consumers_never_collide(tmp_path: Path) -> None:
    for name in ("scorer-1", "scorer-2"):
        sink = ParquetSink(name, tmp_path, clock=FakeClock())
        sink.append(_rows(1), _ids(1))
        assert sink.flush().paths[0].name == f"part-{name}-000000.parquet"

    assert len(list(tmp_path.glob("*/*.parquet"))) == 2


# --- the reader view (PLAN §9.4) ----------------------------------------------------


def _write(tmp_path: Path, rows: list[dict[str, Any]], consumer: str = "scorer-1") -> None:
    sink = ParquetSink(consumer, tmp_path, clock=FakeClock())
    sink.append(rows, _ids(len(rows)))
    sink.flush()


def test_view_reads_what_the_sink_wrote(tmp_path: Path) -> None:
    _write(tmp_path, _rows(6))
    connection = duck.connect(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM scored").fetchone()[0] == 6
    finally:
        connection.close()


def test_view_deduplicates_on_txn_id(tmp_path: Path) -> None:
    """The §9.3 crash-between-flush-and-ack case, resolved by the reader."""
    _write(tmp_path, _rows(3))
    _write(tmp_path, _rows(3))  # the same rows written a second time

    assert len(list(tmp_path.glob("*/*.parquet"))) == 2
    connection = duck.connect(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM scored").fetchone()[0] == 3
    finally:
        connection.close()


def test_view_keeps_the_earliest_scored_ts(tmp_path: Path) -> None:
    _write(tmp_path, [_row(0, scored_ts=500)])
    _write(tmp_path, [_row(0, scored_ts=900)])

    connection = duck.connect(tmp_path)
    try:
        value = connection.execute("SELECT scored_ts FROM scored WHERE txn_id='T0000000'")
        assert value.fetchone()[0] == 500
    finally:
        connection.close()


def test_view_exposes_the_hive_partition(tmp_path: Path) -> None:
    _write(tmp_path, _rows(2))
    connection = duck.connect(tmp_path)
    try:
        rows = connection.execute("SELECT DISTINCT date FROM scored").fetchall()
        assert [str(row[0]) for row in rows] == ["2026-03-14"]
    finally:
        connection.close()


def test_view_is_empty_but_queryable_before_any_data(tmp_path: Path) -> None:
    """The dashboard starts before the scorer writes; it must not crash."""
    assert duck.has_scored_data(tmp_path) is False
    connection = duck.connect(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM scored").fetchone()[0] == 0
    finally:
        connection.close()


def test_temp_files_are_not_read_by_the_view(tmp_path: Path) -> None:
    _write(tmp_path, _rows(2))
    partition = next(tmp_path.glob("date=*"))
    (partition / f"part-scorer-9-000000.parquet{TEMP_SUFFIX}").write_text("not parquet")

    connection = duck.connect(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM scored").fetchone()[0] == 2
    finally:
        connection.close()


def test_monitoring_queries_run(tmp_path: Path) -> None:
    """§9.4's queries go in the README and the dashboard; they must actually execute."""
    _write(tmp_path, _rows(20))
    connection = duck.connect(tmp_path)
    try:
        merchants = connection.execute(duck.RISKIEST_MERCHANTS).fetchall()
        hourly = connection.execute(duck.ALERT_RATE_BY_HOUR).fetchall()
        latency = connection.execute(duck.LATENCY_PERCENTILES).fetchone()[0]
    finally:
        connection.close()

    assert merchants and len(merchants) <= 10
    assert hourly
    assert len(latency) == 3


def test_connection_opens_no_database_file(tmp_path: Path) -> None:
    """Invariant 7: services never open a .duckdb file, because it would lock."""
    _write(tmp_path, _rows(2))
    connection = duck.connect(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM scored").fetchone()[0] == 2
    finally:
        connection.close()
    assert list(tmp_path.rglob("*.duckdb")) == []


def test_two_readers_can_open_at_once(tmp_path: Path) -> None:
    """No file, no lock: the dashboard and the API read concurrently."""
    _write(tmp_path, _rows(2))
    first, second = duck.connect(tmp_path), duck.connect(tmp_path)
    try:
        assert first.execute("SELECT count(*) FROM scored").fetchone()[0] == 2
        assert second.execute("SELECT count(*) FROM scored").fetchone()[0] == 2
    finally:
        first.close()
        second.close()
