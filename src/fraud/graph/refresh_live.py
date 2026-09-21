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


# --- the refresh loop (PLAN §6.5) ---------------------------------------------------
#
# Everything below builds and publishes snapshots while the replay runs. The rule that
# makes it correct is narrow: history comes from the simulator file ONLY for
# `event_time < test_start`; from the boundary onwards the only permitted source is the
# scorer's own output. Reading simulator rows inside the replay window would give the
# graph events the live system has not seen yet — the features would be quietly better
# than anything reproducible, and no downstream check would notice.

import logging as _logging
import time as _time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from fraud.config import Settings, load_yaml
from fraud.graph.algorithms import SNAPSHOT_COLUMNS, seed_accounts, snapshot_features
from fraud.graph.projection import device_membership, project
from fraud.storage.parquet_io import write_atomic

WATERMARK_KEY: Final[str] = "sink:watermark"


class HistoryLeak(RuntimeError):
    """History was read at or after the replay boundary (PLAN §6.5)."""


@dataclass(frozen=True, slots=True)
class RefreshResult:
    snapshot_ts: int
    accounts: int
    seconds: float
    path: Path | None = None


def test_start() -> pd.Timestamp:
    """The replay boundary, from the one place split dates live (invariant 6)."""
    from fraud.modeling.splits import TEST_SPLIT, windows

    return windows()[TEST_SPLIT].start


def load_history(
    events_path: Path | None = None, boundary: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Simulator events strictly before the replay boundary (PLAN §6.5).

    The filter is asserted after the fact, not just applied. A slicing mistake here would
    not raise — it would simply produce a better graph than the live system can justify.
    """
    settings = Settings.from_env()
    edge = boundary if boundary is not None else test_start()
    frame = pd.read_parquet(events_path or settings.raw_dir / "events.parquet")
    history = frame[frame["event_time"] < edge]

    if len(history) and history["event_time"].max() >= edge:  # pragma: no cover - guard
        raise HistoryLeak(f"history reaches {history['event_time'].max()}, boundary is {edge}")
    return history


def load_live_events(scored_root: Path | None = None) -> pd.DataFrame:
    """Events the scorer has actually processed, from its own output (PLAN §6.5)."""
    from fraud.storage import duck

    root = scored_root or Settings.from_env().scored_dir
    if not duck.has_scored_data(root):
        return pd.DataFrame(columns=["event_time"])

    connection = duck.connect(root)
    try:
        return connection.execute(
            "SELECT txn_id, event_time, account_id, merchant_id, device_id, ip, amount, "
            "status FROM scored"
        ).fetch_df()
    finally:
        connection.close()


def graph_inputs(
    snapshot_ts: pd.Timestamp,
    *,
    events_path: Path | None = None,
    scored_root: Path | None = None,
) -> pd.DataFrame:
    """History plus live output, restricted to ``event_time < snapshot_ts``."""
    history = load_history(events_path)
    live = load_live_events(scored_root)

    if len(live):
        live = live[live["event_time"] >= test_start()]
    combined = pd.concat([history, live], ignore_index=True) if len(live) else history
    return combined[combined["event_time"] < snapshot_ts]


def build_and_publish(
    client: Redis,
    snapshot_ts: pd.Timestamp,
    *,
    events_path: Path | None = None,
    scored_root: Path | None = None,
    labels_path: Path | None = None,
    out_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> RefreshResult:
    """Compute one snapshot and publish it, exactly as the offline builder would."""
    settings = Settings.from_env()
    cfg = config or load_yaml("graph")
    started = _time.perf_counter()

    events = graph_inputs(snapshot_ts, events_path=events_path, scored_root=scored_root)
    labels = pd.read_parquet(labels_path or settings.raw_dir / "labels.parquet")
    accounts = pd.read_parquet(
        settings.raw_dir / "accounts.parquet", columns=["account_id", "created_at"]
    )
    created = accounts.set_index("account_id")["created_at"]

    # The same four calls snapshots.py makes. Two implementations of "a snapshot" would
    # be two different graphs, and §6.5's parity check would be comparing them rather
    # than checking anything.
    result = snapshot_features(
        project(events, snapshot_ts, cfg),
        snapshot_ts,
        device_members=device_membership(events, snapshot_ts, cfg),
        account_created=created,
        seeds=seed_accounts(labels, snapshot_ts, events),
        config=cfg,
    )

    frame = result.features
    if frame.empty:
        frame = pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))

    target = out_dir or settings.graph_live_dir
    path = write_atomic(
        frame,
        target / f"snapshot_ts={snapshot_ts.strftime('%Y-%m-%d')}" / "part.parquet",
        compression="zstd",
    )
    published = publish(client, snapshot_ts, frame, config=cfg)

    elapsed = _time.perf_counter() - started
    _logging.getLogger(__name__).info(
        "published snapshot %s: %d accounts in %.1fs", snapshot_ts.date(), published, elapsed
    )
    return RefreshResult(
        snapshot_ts=to_epoch_seconds(snapshot_ts.to_pydatetime()),
        accounts=published,
        seconds=elapsed,
        path=path,
    )


def publish(
    client: Redis,
    snapshot_ts: pd.Timestamp,
    frame: pd.DataFrame,
    *,
    config: dict[str, Any] | None = None,
) -> int:
    """Write the snapshot's hashes, then add it to ``graph:published``.

    The ZADD is last on purpose: a scorer that sees T in the published set immediately
    starts resolving events to it, and doing that before the hashes exist would silently
    serve defaults for the gap.
    """
    cfg = config or load_yaml("graph")
    ttl = int(cfg["live"]["key_ttl_seconds"])
    stamp = to_epoch_seconds(snapshot_ts.to_pydatetime())
    names = list(defaults(cfg))

    pipe = client.pipeline(transaction=False)
    for row in frame.to_dict("records"):
        key = snapshot_key(stamp, str(row["account_id"]))
        pipe.hset(key, mapping={name: str(row[name]) for name in names})
        pipe.expire(key, ttl)
    pipe.execute()

    client.zadd(PUBLISHED_KEY, {str(stamp): stamp})
    return len(frame)


def next_due(client: Redis, cadence_days: int = 1) -> pd.Timestamp | None:
    """The next snapshot boundary, if the scorer has flushed far enough (PLAN §6.5).

    ``wm >= T`` is sufficient because one scorer processes events in time order and
    flushes in that order: once anything at or after T is on disk, everything before T
    already is.
    """
    watermark = client.get(WATERMARK_KEY)
    if not watermark:
        return None

    published = published_snapshots(client)
    if not published:
        return None

    candidate = from_epoch_seconds(published[-1]) + timedelta(days=cadence_days)
    return pd.Timestamp(candidate) if pd.Timestamp(watermark) >= candidate else None


def run(
    client: Redis,
    *,
    max_snapshots: int | None = None,
    stop_when_idle: bool = False,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> list[RefreshResult]:
    """The §6.5 loop: whenever the watermark passes the next boundary, publish it."""
    cfg = config or load_yaml("graph")
    poll = float(cfg["live"]["poll_seconds"])
    cadence = int(cfg["cadence_days"])

    produced: list[RefreshResult] = []
    while max_snapshots is None or len(produced) < max_snapshots:
        due = next_due(client, cadence)
        if due is None:
            if stop_when_idle:
                break
            _time.sleep(poll)
            continue
        produced.append(build_and_publish(client, due, config=cfg, **kwargs))
    return produced


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - service entrypoint
    """``python -m fraud.graph.refresh_live`` (PLAN §6.5)."""
    import argparse

    parser = argparse.ArgumentParser(description="Publish live graph snapshots (§6.5).")
    parser.add_argument("--max-snapshots", type=int, default=None)
    parser.add_argument("--stop-when-idle", action="store_true")
    args = parser.parse_args(argv)

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    run(client, max_snapshots=args.max_snapshots, stop_when_idle=args.stop_when_idle)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
