"""Live graph keyspace: how snapshots are published and looked up (PLAN §6.5, §3.6).

This module owns the shape of the live graph keys. The refresh service writes them
(P6.4) and the scorer reads them, and the two cannot be allowed to disagree about a key
name or a timestamp unit — a mismatch does not raise, it just means every lookup misses
and every event silently scores on §6.3 defaults. So the layout lives in one place and
both sides import it.

**Timestamps here are epoch seconds**, as §3.6 specifies. That is deliberately different
from the entity sorted sets in ``features/store_redis.py``, which use milliseconds
because two events in the same second must not collapse onto one score. Snapshots are
midnight-aligned and a day apart, so seconds carry them exactly and there is no reason to
depart from the plan.

**Resolution is a bisect, not a scan.** Each event takes the latest published snapshot at
or before its own event time, which is the online equivalent of the point-in-time join in
§6.4. Taking the *newest* snapshot instead would hand an event graph features built from
its own future (leakage rule L3), and would do so invisibly.
"""

from __future__ import annotations

import bisect
import logging
from datetime import datetime, timedelta
from typing import Any, Final

from redis import Redis

from fraud.features.state import EPOCH
from fraud.graph.algorithms import defaults

logger = logging.getLogger(__name__)

PUBLISHED_KEY: Final[str] = "graph:published"
SNAPSHOT_KEY: Final[str] = "graph:{ts}:acct:{account_id}"


def to_epoch_seconds(moment: datetime) -> int:
    """Epoch seconds from a naive IST timestamp (PLAN §3.5, §3.6).

    Mirrors ``state.to_millis``: not ``datetime.timestamp()``, which would read a naive
    value in the machine's local zone and make the same snapshot resolve differently on
    a different host.
    """
    return int((moment - EPOCH).total_seconds())


def from_epoch_seconds(seconds: float | str) -> datetime:
    """The exact inverse of ``to_epoch_seconds``.

    Arithmetic on EPOCH, never ``datetime.fromtimestamp``: that reads the value in the
    machine's local zone, which is the same trap ``state.to_millis`` was written to
    avoid, and it would silently shift every snapshot by the host's UTC offset.
    """
    return EPOCH + timedelta(seconds=float(seconds))


def snapshot_key(snapshot_ts: int, account_id: str) -> str:
    return SNAPSHOT_KEY.format(ts=snapshot_ts, account_id=account_id)


def published_snapshots(client: Redis) -> list[int]:
    """Every published snapshot boundary, ascending (about 20 entries, §6.5)."""
    return sorted(int(float(member)) for member in client.zrange(PUBLISHED_KEY, 0, -1))


def resolve_snapshot(event_time: datetime, published: list[int]) -> int | None:
    """The latest snapshot at or before ``event_time``, or None if there is none.

    None is a real answer, not an error: events before the first snapshot legitimately
    have no graph position yet and take the §6.3 defaults.
    """
    if not published:
        return None
    index = bisect.bisect_right(published, to_epoch_seconds(event_time))
    return published[index - 1] if index else None


def fetch_graph_features(
    client: Redis, wanted: list[tuple[int | None, str]]
) -> list[dict[str, float]]:
    """Graph features for (snapshot, account) pairs, one pipeline (PLAN §6.5).

    A missing key means the account was not in that snapshot — new, or dropped by the
    fan-out caps — and §6.3 says that is defaults, not an error.
    """
    fallback = defaults()

    pipe = client.pipeline(transaction=False)
    for snapshot_ts, account_id in wanted:
        if snapshot_ts is None:
            continue
        pipe.hgetall(snapshot_key(snapshot_ts, account_id))
    fetched = iter(pipe.execute())

    rows: list[dict[str, float]] = []
    for snapshot_ts, _ in wanted:
        if snapshot_ts is None:
            rows.append(dict(fallback))
            continue
        stored: dict[str, Any] = next(fetched)
        if not stored:
            rows.append(dict(fallback))
            continue
        rows.append({name: float(stored.get(name, value)) for name, value in fallback.items()})
    return rows
