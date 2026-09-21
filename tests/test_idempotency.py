"""Duplicate delivery must change nothing (PLAN §5.4, §9.3, §13).

Redis Streams deliver at-least-once. The scorer therefore sees the same event twice
whenever an ack is lost, a consumer dies mid-batch, or `XAUTOCLAIM` reassigns a pending
message. Without a guard each duplicate would fold itself into the account state a second
time: velocity counters climb, the running mean drifts, and the corruption is permanent
because nothing downstream can tell a real second payment from a replayed first one.

The contract §5.4 asks for is narrow and testable — a redelivered event returns the
record stored the first time and touches no state — so these tests check both halves:
the answer is identical, and nothing in Redis moved.

The other half is atomicity. A commit that wrote the record but not the state would make
the next duplicate return a record whose effects were never applied; the reverse would
double-count. MULTI/EXEC is what rules both out.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from feature_helpers import ACCOUNT, CREATED_AT, HOME_CITY, MUMBAI, at, directory, make_event
from redis import Redis

from fraud.features.engine import FeatureEngine
from fraud.features.spec import FeatureConfig
from fraud.features.store_memory import InMemoryStore
from fraud.features.store_redis import RedisStore

pytestmark = pytest.mark.redis

SECOND_ACCOUNT = "A0000002"


def _directory() -> Any:
    return directory(
        {
            ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT),
            SECOND_ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT),
        }
    )


def _engine(client: Redis) -> tuple[FeatureEngine, RedisStore]:
    store = RedisStore(client)
    return FeatureEngine(store, _directory()), store


def _snapshot(client: Redis) -> dict[str, Any]:
    """Everything in the test database, so "nothing moved" is checkable literally."""
    state: dict[str, Any] = {}
    for key in sorted(client.keys("*")):
        kind = client.type(key)
        if kind == "zset":
            state[key] = client.zrange(key, 0, -1, withscores=True)
        else:
            state[key] = client.get(key)
    return state


# --- the redelivery contract -------------------------------------------------------


def test_redelivery_returns_the_same_record(redis_client: Redis) -> None:
    engine, _ = _engine(redis_client)
    event = make_event("T0000001", at(9))

    first = engine.process(event)
    second = engine.process(event)
    assert first == second


def test_redelivery_changes_no_state_at_all(redis_client: Redis) -> None:
    """Not "roughly the same": byte-identical keys, values and sorted-set scores."""
    engine, _ = _engine(redis_client)
    event = make_event("T0000001", at(9))

    engine.process(event)
    before = _snapshot(redis_client)
    engine.process(event)
    assert _snapshot(redis_client) == before


def test_redelivery_does_not_double_count_velocity(redis_client: Redis) -> None:
    """The failure this whole mechanism exists to prevent (PLAN §9.3)."""
    engine, _ = _engine(redis_client)
    first = make_event("T0000001", at(9))

    engine.process(first)
    engine.process(first)  # duplicate
    engine.process(first)  # and again

    later = engine.process(make_event("T0000002", at(9, 1)))
    assert later["acct_cnt_5m"] == 1
    assert later["acct_history_cnt"] == 1


def test_redelivery_does_not_double_count_fan_out(redis_client: Redis) -> None:
    engine, _ = _engine(redis_client)
    engine.process(make_event("T0000001", at(9), account_id=SECOND_ACCOUNT))
    engine.process(make_event("T0000001", at(9), account_id=SECOND_ACCOUNT))

    row = engine.process(make_event("T0000002", at(9, 30), account_id=ACCOUNT))
    assert row["dev_accts_1h"] == 1


def test_redelivery_does_not_disturb_the_running_mean(redis_client: Redis) -> None:
    """A duplicated amount would bias amount_zscore for every later event."""
    engine, _ = _engine(redis_client)
    for index, amount in enumerate((100.0, 200.0, 300.0), start=1):
        event = make_event(f"T000000{index}", at(9, index), amount=amount)
        engine.process(event)
        engine.process(event)  # every one delivered twice

    row = engine.process(make_event("T0000009", at(10), amount=200.0))
    assert row["acct_history_cnt"] == 3
    assert row["amount_to_mean"] == pytest.approx(1.0)


def test_both_stores_agree_under_duplicate_delivery(redis_client: Redis) -> None:
    """Parity has to hold on the redelivery path too, not just the happy path."""
    events = [make_event(f"T000000{i}", at(9, i * 2), amount=100.0 * i) for i in range(1, 5)]
    delivered = [event for event in events for _ in range(2)]  # each one twice

    memory_engine = FeatureEngine(InMemoryStore(), _directory())
    memory_rows = [memory_engine.process(event) for event in delivered]

    redis_engine, _ = _engine(redis_client)
    redis_rows = [redis_engine.process(event) for event in delivered]
    assert memory_rows == redis_rows


def test_extra_fields_survive_a_redelivery(redis_client: Redis) -> None:
    """`extra` carries graph_snapshot_ts, so a duplicate must reuse the same snapshot."""
    engine, _ = _engine(redis_client)
    event = make_event("T0000001", at(9))

    first = engine.process(event, extra={"graph_snapshot_ts": "2026-03-01T00:00:00"})
    second = engine.process(event, extra={"graph_snapshot_ts": "2026-03-08T00:00:00"})
    assert second["graph_snapshot_ts"] == "2026-03-01T00:00:00"
    assert second == first


def test_a_non_json_extra_is_rejected_loudly(redis_client: Redis) -> None:
    """Silently stringifying a datetime would break the §9.6 re-score comparison."""
    from datetime import datetime

    engine, _ = _engine(redis_client)
    with pytest.raises(TypeError):
        engine.process(
            make_event("T0000001", at(9)),
            extra={"graph_snapshot_ts": datetime(2026, 3, 1)},  # noqa: DTZ001
        )
    assert not redis_client.exists("feat:T0000001")


# --- atomicity (PLAN §5.4, §9.3) ----------------------------------------------------


def test_commit_uses_a_transactional_pipeline(redis_client: Redis) -> None:
    """MULTI/EXEC, not a plain pipeline: the writes must land together or not at all."""
    engine, store = _engine(redis_client)
    seen: list[bool] = []
    original = store.client.pipeline

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(bool(kwargs.get("transaction", True)))
        return original(*args, **kwargs)

    store.client.pipeline = spy  # type: ignore[method-assign]
    try:
        engine.process(make_event("T0000001", at(9)))
    finally:
        store.client.pipeline = original  # type: ignore[method-assign]

    # The read pipeline is non-transactional; the commit must be transactional.
    assert seen == [False, True]


def test_a_failure_while_building_the_commit_writes_nothing(redis_client: Redis) -> None:
    engine, _ = _engine(redis_client)
    engine.process(make_event("T0000001", at(9)))
    before = _snapshot(redis_client)

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        engine.process(make_event("T0000002", at(9, 5)), extra={"bad": Unserialisable()})

    assert _snapshot(redis_client) == before
    assert not redis_client.exists("feat:T0000002")


def test_commit_writes_every_key_together(redis_client: Redis) -> None:
    """One event, one transaction: record, account blob and three entity touches."""
    engine, _ = _engine(redis_client)
    event = make_event("T0000001", at(9))
    engine.process(event)

    assert sorted(redis_client.keys("*")) == sorted(
        [
            f"feat:{event.txn_id}",
            f"state:acct:{event.account_id}",
            f"ent:dev:{event.device_id}",
            f"ent:ip:{event.ip}",
            f"ent:mer:{event.merchant_id}",
        ]
    )


def test_uncommitted_processing_writes_nothing(redis_client: Redis) -> None:
    """`commit=False` is how the API scores without touching state (PLAN §10)."""
    engine, _ = _engine(redis_client)
    features = engine.process(make_event("T0000001", at(9)), commit=False)

    assert features["acct_cnt_5m"] == 0
    assert redis_client.keys("*") == []


def test_uncommitted_processing_never_short_circuits(redis_client: Redis) -> None:
    """Without a commit there is no stored record, so the engine must recompute."""
    engine, _ = _engine(redis_client)
    event = make_event("T0000001", at(9))
    engine.process(event)

    # Same txn_id, different amount: a committed call returns the stored record, an
    # uncommitted one recomputes from the event it was handed.
    changed = make_event("T0000001", at(9), amount=9999.0)
    assert engine.process(changed)["log_amount"] == engine.process(event)["log_amount"]
    assert (
        engine.process(changed, commit=False)["log_amount"] != engine.process(event)["log_amount"]
    )


# --- trimming (PLAN §5.3) -----------------------------------------------------------


def test_trimming_never_changes_a_count(redis_client: Redis) -> None:
    """Trimming removes only what no window can reach, so counts must be untouched."""
    cfg = replace(FeatureConfig.load(), redis_trim_every=1)  # trim on every commit
    events = [make_event(f"T000000{i}", at(9, i * 6)) for i in range(1, 6)]

    engine = FeatureEngine(RedisStore(redis_client, cfg), _directory())
    rows = [engine.process(event) for event in events]

    memory_engine = FeatureEngine(InMemoryStore(), _directory())
    assert rows == [memory_engine.process(event) for event in events]


def test_trimming_keeps_the_window_boundary(redis_client: Redis) -> None:
    """The cutoff is exclusive, so an entry exactly on the boundary survives."""
    cfg = replace(FeatureConfig.load(), redis_trim_every=1)
    engine = FeatureEngine(RedisStore(redis_client, cfg), _directory())
    month = timedelta(seconds=cfg.month_seconds)
    start = at(10)

    engine.process(make_event("T0000001", start - month, account_id=SECOND_ACCOUNT))
    row = engine.process(make_event("T0000002", start, account_id=ACCOUNT))

    assert row["dev_accts_30d"] == 1
    assert redis_client.zscore("ent:dev:D0000001", SECOND_ACCOUNT) is not None
