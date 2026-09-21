"""Backfill and the live graph refresh (PLAN §6.5, §9.5, §13).

The guard these tests exist for is narrow and easy to break without noticing: the graph
refresh may read the simulator's `events.parquet` **only** for `event_time < test_start`.
Everything from the boundary onwards has to come from the scorer's own output. Reading
simulator rows inside the replay window would hand the graph events the live system has
not processed yet — the features would be better than anything reproducible, the live
metrics would beat the offline ones, and nothing would raise.

The backfill tests protect a quieter failure. A half-finished backfill does not error; it
produces a system that starts and scores against partial history, where every account
looks newer and thinner than it is.
"""

from __future__ import annotations

import gzip
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
from redis import Redis

from fraud.config import load_yaml
from fraud.features.spec import FEATURE_SPEC_VERSION
from fraud.features.store_redis import STATE_VERSION_KEY, RedisStore
from fraud.graph import refresh_live
from fraud.graph.refresh_live import (
    PUBLISHED_KEY,
    WATERMARK_KEY,
    from_epoch_seconds,
    next_due,
    published_snapshots,
    resolve_snapshot,
    snapshot_key,
    to_epoch_seconds,
)
from fraud.stream import backfill

pytestmark = pytest.mark.redis

BOUNDARY = pd.Timestamp("2026-03-14")
ACCOUNTS = ("A0000001", "A0000002", "A0000003")


# --- helpers -------------------------------------------------------------------------


def _checkpoint(tmp_path: Path, *, spec: str = FEATURE_SPEC_VERSION) -> Path:
    blob = {
        "checkpoint_version": 1,
        "feature_spec_version": spec,
        "accounts": {
            "A0000001": {
                "recent": [[1_772_600_000_000, 100.0, "M000001", 0]],
                "last_ts": 1_772_600_000_000,
                "last_lat": 19.0,
                "last_lon": 72.0,
                "n_all": 5,
                "n_ok": 5,
                "sum_ok": 500.0,
                "sumsq_ok": 60000.0,
                "hour_buckets": [1, 0, 2, 0, 1, 1],
                "devices": {"D0000001": 1_772_600_000_000},
                "merchants": ["M000001"],
                "cities": ["Mumbai"],
            },
            "A0000002": {
                "recent": [],
                "last_ts": None,
                "last_lat": None,
                "last_lon": None,
                "n_all": 0,
                "n_ok": 0,
                "sum_ok": 0.0,
                "sumsq_ok": 0.0,
                "hour_buckets": [0] * 6,
                "devices": {},
                "merchants": [],
                "cities": ["Delhi"],
            },
        },
        "entities": {
            "dev": {"D0000001": {"A0000001": 1_772_600_000_000}},
            "ip": {"49.1.1.1": {"A0000001": 1_772_600_000_000}},
            "mer": {"M000001": {"A0000001": 1_772_600_000_000}},
        },
    }
    path = tmp_path / f"checkpoint_{BOUNDARY.date()}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(blob, handle)
    return path


def _snapshot_frame(
    stamp: pd.Timestamp = BOUNDARY, accounts: tuple[str, ...] = ACCOUNTS
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "snapshot_ts": stamp,
                "account_id": account,
                "graph_degree": float(index + 1),
                "graph_clustering": 0.5,
                "community_size": index + 2,
                "community_shared_devices": 1,
                "community_young_share": 0.25,
                "ppr_risk": 0.001 * (index + 1),
            }
            for index, account in enumerate(accounts)
        ]
    )


def _offline_snapshot(tmp_path: Path, stamp: pd.Timestamp = BOUNDARY) -> Path:
    target = tmp_path / f"snapshot_ts={stamp.date()}"
    target.mkdir(parents=True, exist_ok=True)
    _snapshot_frame(stamp).to_parquet(target / "part.parquet", index=False)
    return tmp_path


# --- backfill: the skip check (PLAN §9.5 step 1) --------------------------------------


def test_backfill_loads_state_and_publishes(redis_client: Redis, tmp_path: Path) -> None:
    result = backfill.run(
        redis_client,
        checkpoint_path=_checkpoint(tmp_path),
        snapshot_dir=_offline_snapshot(tmp_path / "graph"),
    )
    assert result.skipped is False
    assert result.accounts == 2
    assert result.entity_members == 3
    assert result.graph_accounts == 3
    assert redis_client.exists("state:acct:A0000001")
    assert redis_client.zscore("ent:dev:D0000001", "A0000001") == 1_772_600_000_000


def test_a_second_run_is_skipped(redis_client: Redis, tmp_path: Path) -> None:
    """The marker is what stops `make up` redoing this on every restart."""
    args = {
        "checkpoint_path": _checkpoint(tmp_path),
        "snapshot_dir": _offline_snapshot(tmp_path / "graph"),
    }
    backfill.run(redis_client, **args)
    assert backfill.run(redis_client, **args).skipped is True


def test_force_overrides_the_marker(redis_client: Redis, tmp_path: Path) -> None:
    args = {
        "checkpoint_path": _checkpoint(tmp_path),
        "snapshot_dir": _offline_snapshot(tmp_path / "graph"),
    }
    backfill.run(redis_client, **args)
    assert backfill.run(redis_client, force=True, **args).skipped is False


def test_the_marker_is_written_last(redis_client: Redis, tmp_path: Path) -> None:
    """Written earlier, a crash midway would leave a marker claiming a finished run."""
    with pytest.raises(FileNotFoundError):
        backfill.run(redis_client, checkpoint_path=tmp_path / "absent.json.gz")
    assert not redis_client.exists(backfill.done_key())


def test_backfill_is_idempotent(redis_client: Redis, tmp_path: Path) -> None:
    """A partial earlier run must simply be overwritten (§9.5)."""
    args = {
        "checkpoint_path": _checkpoint(tmp_path),
        "snapshot_dir": _offline_snapshot(tmp_path / "graph"),
    }
    backfill.run(redis_client, **args)
    before = redis_client.get("state:acct:A0000001")
    published_before = published_snapshots(redis_client)

    backfill.run(redis_client, force=True, **args)
    assert redis_client.get("state:acct:A0000001") == before
    assert published_snapshots(redis_client) == published_before


def test_entity_scores_never_move_backwards(redis_client: Redis, tmp_path: Path) -> None:
    """ZADD GT: re-running must not undo progress the live run already made."""
    args = {
        "checkpoint_path": _checkpoint(tmp_path),
        "snapshot_dir": _offline_snapshot(tmp_path / "graph"),
    }
    backfill.run(redis_client, **args)
    redis_client.zadd("ent:dev:D0000001", {"A0000001": 1_772_900_000_000})

    backfill.run(redis_client, force=True, **args)
    assert redis_client.zscore("ent:dev:D0000001", "A0000001") == 1_772_900_000_000


def test_a_checkpoint_from_another_spec_is_refused(redis_client: Redis, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="feature spec"):
        backfill.run(redis_client, checkpoint_path=_checkpoint(tmp_path, spec="fs0"))


def test_backfill_writes_the_state_version(redis_client: Redis, tmp_path: Path) -> None:
    backfill.run(
        redis_client,
        checkpoint_path=_checkpoint(tmp_path),
        snapshot_dir=_offline_snapshot(tmp_path / "graph"),
    )
    assert redis_client.get(STATE_VERSION_KEY) == FEATURE_SPEC_VERSION
    RedisStore(redis_client).check_state_version()  # must not raise


def test_backfill_creates_the_group_at_zero(redis_client: Redis, tmp_path: Path) -> None:
    """Created after the replayer starts, or at `$`, early events are never scored."""
    stream, group = load_yaml("stream")["stream"]["name"], load_yaml("stream")["stream"]["group"]
    backfill.run(
        redis_client,
        checkpoint_path=_checkpoint(tmp_path),
        snapshot_dir=_offline_snapshot(tmp_path / "graph"),
    )
    redis_client.xadd(stream, {"txn_id": "T1"})

    batch = redis_client.xreadgroup(group, "c1", {stream: ">"}, count=10)
    assert len(batch[0][1]) == 1


def test_the_first_snapshot_is_published_and_resolvable(
    redis_client: Redis, tmp_path: Path
) -> None:
    backfill.run(
        redis_client,
        checkpoint_path=_checkpoint(tmp_path),
        snapshot_dir=_offline_snapshot(tmp_path / "graph"),
    )
    stamp = to_epoch_seconds(BOUNDARY.to_pydatetime())
    assert published_snapshots(redis_client) == [stamp]
    assert resolve_snapshot(BOUNDARY.to_pydatetime() + timedelta(hours=3), [stamp]) == stamp

    stored = redis_client.hgetall(snapshot_key(stamp, "A0000001"))
    assert float(stored["graph_degree"]) == 1.0


def test_graph_keys_expire(redis_client: Redis, tmp_path: Path) -> None:
    """§3.6 gives these a 3-day TTL so abandoned snapshots do not accumulate."""
    backfill.run(
        redis_client,
        checkpoint_path=_checkpoint(tmp_path),
        snapshot_dir=_offline_snapshot(tmp_path / "graph"),
    )
    stamp = to_epoch_seconds(BOUNDARY.to_pydatetime())
    ttl = redis_client.ttl(snapshot_key(stamp, "A0000001"))
    assert 0 < ttl <= load_yaml("graph")["live"]["key_ttl_seconds"]


def test_a_missing_offline_snapshot_is_survivable(redis_client: Redis, tmp_path: Path) -> None:
    """Better to start with defaults than to refuse to start at all."""
    result = backfill.run(
        redis_client, checkpoint_path=_checkpoint(tmp_path), snapshot_dir=tmp_path / "absent"
    )
    assert result.snapshot_ts is None
    assert result.accounts == 2


# --- the history guard (PLAN §6.5) ----------------------------------------------------


def test_history_stops_at_the_replay_boundary(tmp_path: Path) -> None:
    """The guard this whole module exists for."""
    path = tmp_path / "events.parquet"
    pd.DataFrame(
        {
            "txn_id": ["T1", "T2", "T3"],
            "event_time": [
                BOUNDARY - timedelta(days=1),
                BOUNDARY - timedelta(seconds=1),
                BOUNDARY,  # at the boundary: must be excluded
            ],
            "account_id": ["A1", "A2", "A3"],
        }
    ).to_parquet(path)

    history = refresh_live.load_history(path, BOUNDARY)
    assert list(history["txn_id"]) == ["T1", "T2"]
    assert history["event_time"].max() < BOUNDARY


def test_history_never_reaches_into_the_replay_window(tmp_path: Path) -> None:
    """Asserted on the real simulator file, not a fixture."""
    history = refresh_live.load_history()
    assert history["event_time"].max() < refresh_live.test_start()


def test_the_real_boundary_comes_from_the_split_config() -> None:
    from fraud.modeling.splits import windows

    assert refresh_live.test_start() == windows()["test"].start


def test_graph_inputs_combine_history_and_live_output(tmp_path: Path) -> None:
    """Events at or after the boundary may only come from the scorer's own output."""
    events = tmp_path / "events.parquet"
    pd.DataFrame(
        {
            "txn_id": ["OLD", "FUTURE"],
            "event_time": [
                refresh_live.test_start() - timedelta(days=1),
                refresh_live.test_start() + timedelta(hours=1),  # must be ignored
            ],
            "account_id": ["A1", "A2"],
        }
    ).to_parquet(events)

    combined = refresh_live.graph_inputs(
        refresh_live.test_start() + timedelta(days=1),
        events_path=events,
        scored_root=tmp_path / "no-scored-output",
    )
    assert list(combined["txn_id"]) == ["OLD"]


# --- the refresh loop (PLAN §6.5) -----------------------------------------------------


def test_nothing_is_due_without_a_watermark(redis_client: Redis) -> None:
    redis_client.zadd(PUBLISHED_KEY, {"1": 1})
    assert next_due(redis_client) is None


def test_nothing_is_due_before_the_watermark_passes_the_boundary(
    redis_client: Redis,
) -> None:
    stamp = to_epoch_seconds(BOUNDARY.to_pydatetime())
    redis_client.zadd(PUBLISHED_KEY, {str(stamp): stamp})
    redis_client.set(WATERMARK_KEY, (BOUNDARY + timedelta(hours=23)).isoformat())

    assert next_due(redis_client) is None


def test_a_snapshot_is_due_once_the_watermark_crosses(redis_client: Redis) -> None:
    """`wm >= T` is sufficient because one scorer flushes in event order (§6.5)."""
    stamp = to_epoch_seconds(BOUNDARY.to_pydatetime())
    redis_client.zadd(PUBLISHED_KEY, {str(stamp): stamp})
    redis_client.set(WATERMARK_KEY, (BOUNDARY + timedelta(days=1, minutes=1)).isoformat())

    assert next_due(redis_client) == BOUNDARY + timedelta(days=1)


def test_publish_writes_hashes_before_announcing(redis_client: Redis) -> None:
    """A scorer that sees T published starts resolving to it immediately."""
    stamp = BOUNDARY + timedelta(days=1)
    refresh_live.publish(redis_client, stamp, _snapshot_frame(stamp))

    epoch = to_epoch_seconds(stamp.to_pydatetime())
    assert published_snapshots(redis_client) == [epoch]
    for account in ACCOUNTS:
        assert redis_client.hgetall(snapshot_key(epoch, account))


def test_published_snapshots_round_trip(redis_client: Redis) -> None:
    for day in range(3):
        stamp = to_epoch_seconds((BOUNDARY + timedelta(days=day)).to_pydatetime())
        redis_client.zadd(PUBLISHED_KEY, {str(stamp): stamp})

    published = published_snapshots(redis_client)
    assert published == sorted(published)
    assert from_epoch_seconds(published[0]) == BOUNDARY.to_pydatetime()


def test_the_loop_stops_when_nothing_is_due(redis_client: Redis) -> None:
    stamp = to_epoch_seconds(BOUNDARY.to_pydatetime())
    redis_client.zadd(PUBLISHED_KEY, {str(stamp): stamp})
    redis_client.set(WATERMARK_KEY, BOUNDARY.isoformat())

    assert refresh_live.run(redis_client, stop_when_idle=True) == []


def test_the_loop_publishes_a_due_snapshot(redis_client: Redis, tmp_path: Path) -> None:
    """End to end on a tiny graph: due -> computed -> written -> published."""
    events = tmp_path / "events.parquet"
    start = refresh_live.test_start()
    # Two accounts sharing one device, which is an edge the projection will keep.
    pd.DataFrame(
        {
            "txn_id": ["T1", "T2", "T3", "T4"],
            "event_time": [start - timedelta(days=d) for d in (5, 4, 3, 2)],
            "account_id": ["A0000001", "A0000002", "A0000001", "A0000002"],
            "merchant_id": ["M000001"] * 4,
            "device_id": ["D0000001"] * 4,
            "ip": ["49.1.1.1"] * 4,
            "amount": [100.0] * 4,
            "status": ["APPROVED"] * 4,
        }
    ).to_parquet(events)

    stamp = to_epoch_seconds(start.to_pydatetime())
    redis_client.zadd(PUBLISHED_KEY, {str(stamp): stamp})
    redis_client.set(WATERMARK_KEY, (start + timedelta(days=1, hours=1)).isoformat())

    produced = refresh_live.run(
        redis_client,
        max_snapshots=1,
        events_path=events,
        scored_root=tmp_path / "no-scored",
        out_dir=tmp_path / "live",
    )

    assert len(produced) == 1
    assert produced[0].path is not None and produced[0].path.is_file()
    assert len(published_snapshots(redis_client)) == 2


def test_the_live_builder_is_the_offline_builder(tmp_path: Path) -> None:
    """§6.5's parity requirement, at the level that makes it true by construction.

    Both paths call the same four functions from `projection.py` and `algorithms.py`.
    Two implementations of "a snapshot" would make the parity check compare them rather
    than check anything, so this pins the shared import.
    """
    import inspect

    from fraud.graph import snapshots

    source = inspect.getsource(refresh_live.build_and_publish)
    for call in ("project(", "device_membership(", "seed_accounts(", "snapshot_features("):
        assert call in source, call
        assert call in inspect.getsource(snapshots.build_snapshots), call


def test_snapshot_columns_have_one_definition() -> None:
    from fraud.graph.algorithms import SNAPSHOT_COLUMNS

    assert refresh_live.SNAPSHOT_COLUMNS is SNAPSHOT_COLUMNS
