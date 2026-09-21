"""The two state backends must produce identical features (PLAN §5.4, §13).

This is the test the whole two-store design rests on. Every metric in `reports/` was
measured offline through `InMemoryStore`; the scorer serves through `RedisStore`. If they
disagree anywhere, those metrics stop describing the running system and there is no way
to notice from the outside — the online numbers would simply be a little different and
nobody would know which set was true.

So parity is asserted on whole sequences rather than on individual calls, and the
sequences are built to hit the places the two implementations are most likely to drift:
window edges, ties on identical timestamps, entities shared across accounts, and
last-seen timestamps that must never move backwards.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from feature_helpers import (
    ACCOUNT,
    CREATED_AT,
    DELHI,
    HOME_CITY,
    MUMBAI,
    at,
    directory,
    make_event,
)
from redis import Redis

from fraud.features.engine import FeatureEngine
from fraud.features.spec import HOT_FEATURE_NAMES, FeatureConfig
from fraud.features.state import AccountState, to_millis
from fraud.features.store_memory import InMemoryStore
from fraud.features.store_redis import RedisStore
from fraud.schemas import TransactionEvent

pytestmark = pytest.mark.redis

SECOND_ACCOUNT = "A0000002"
THIRD_ACCOUNT = "A0000003"


def _directory() -> Any:
    return directory(
        {
            ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT),
            SECOND_ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT),
            THIRD_ACCOUNT: ("Delhi", DELHI, CREATED_AT),
        }
    )


def _run(store: Any, events: list[TransactionEvent]) -> list[dict[str, Any]]:
    engine = FeatureEngine(store, _directory())
    return [engine.process(event) for event in events]


def _both(client: Redis, events: list[TransactionEvent]) -> tuple[list[dict], list[dict]]:
    return _run(InMemoryStore(), events), _run(RedisStore(client), events)


def _assert_parity(client: Redis, events: list[TransactionEvent]) -> list[dict[str, Any]]:
    memory, redis_rows = _both(client, events)
    assert len(memory) == len(redis_rows)
    for index, (left, right) in enumerate(zip(memory, redis_rows, strict=True)):
        assert left.keys() == right.keys(), f"row {index}: different feature sets"
        for name in HOT_FEATURE_NAMES:
            assert left[name] == right[name], (
                f"row {index} ({events[index].txn_id}), feature {name!r}: "
                f"memory={left[name]!r} redis={right[name]!r}"
            )
    return redis_rows


# --- the client contract -----------------------------------------------------------


def test_bytes_client_is_refused(redis_url: str) -> None:
    """Blobs are JSON text; a bytes client would break every read downstream."""
    with pytest.raises(ValueError, match="decode_responses=True"):
        RedisStore(Redis.from_url(redis_url, decode_responses=False))


def test_from_url_builds_a_decoding_client(redis_url: str) -> None:
    store = RedisStore.from_url(redis_url)
    assert store.client.ping() is True
    store.client.close()


# --- feature parity ----------------------------------------------------------------


def test_parity_on_a_single_account_sequence(redis_client: Redis) -> None:
    events = [
        make_event("T0000001", at(9)),
        make_event("T0000002", at(9, 2), amount=50.0, status="DECLINED"),
        make_event("T0000003", at(9, 3), amount=80.0, merchant_id="M000002"),
        make_event("T0000004", at(10), amount=5000.0, merchant_id="M000003"),
        make_event("T0000005", at(23, 30), location=DELHI, city="Delhi"),
    ]
    rows = _assert_parity(redis_client, events)
    # Guard against the whole sequence being trivially zero on both sides.
    assert rows[2]["acct_cnt_5m"] == 2
    assert rows[-1]["new_city"] == 1


def test_parity_across_accounts_sharing_a_device_and_ip(redis_client: Redis) -> None:
    """The fan-out features are where the two index designs could disagree."""
    events = [
        make_event("T0000001", at(9), account_id=ACCOUNT),
        make_event("T0000002", at(9, 10), account_id=SECOND_ACCOUNT),
        make_event("T0000003", at(9, 20), account_id=THIRD_ACCOUNT, city="Delhi", location=DELHI),
        make_event("T0000004", at(9, 30), account_id=ACCOUNT),
    ]
    rows = _assert_parity(redis_client, events)
    assert rows[-1]["dev_accts_1h"] == 3
    assert rows[-1]["ip_accts_1h"] == 3


def test_parity_when_an_account_revisits_the_same_device(redis_client: Redis) -> None:
    """Re-touching must not inflate the count: it is distinct accounts, not visits."""
    events = [make_event(f"T000000{i}", at(9, i * 5)) for i in range(1, 6)]
    rows = _assert_parity(redis_client, events)
    assert rows[-1]["dev_accts_1h"] == 1


def test_parity_with_declines_and_small_amounts(redis_client: Redis) -> None:
    """The card-testing signature, which reads several state fields at once."""
    events = [
        make_event("T0000001", at(9), amount=20.0, status="DECLINED"),
        make_event("T0000002", at(9, 1), amount=30.0, status="DECLINED"),
        make_event("T0000003", at(9, 2), amount=40.0),
        make_event("T0000004", at(9, 3), amount=2000.0),
    ]
    rows = _assert_parity(redis_client, events)
    assert rows[-1]["acct_declines_1h"] == 2
    assert rows[-1]["acct_small_1h"] == 3


def test_parity_over_a_long_mixed_sequence(redis_client: Redis) -> None:
    """Breadth, not depth: many accounts, devices and IPs over several days."""
    accounts = [ACCOUNT, SECOND_ACCOUNT, THIRD_ACCOUNT]
    events: list[TransactionEvent] = []
    moment = at(0)
    for index in range(120):
        moment = moment + timedelta(minutes=17)
        account = accounts[index % 3]
        events.append(
            make_event(
                f"T{index:07d}",
                moment,
                account_id=account,
                device_id=f"D000000{index % 4}",
                ip=f"49.1.1.{index % 5}",
                merchant_id=f"M00000{index % 6}",
                amount=float(50 + (index * 37) % 4000),
                status="DECLINED" if index % 11 == 0 else "APPROVED",
                city="Delhi" if account == THIRD_ACCOUNT else HOME_CITY,
                location=DELHI if account == THIRD_ACCOUNT else MUMBAI,
            )
        )
    rows = _assert_parity(redis_client, events)
    assert max(row["dev_accts_30d"] for row in rows) >= 3


# --- window boundaries (PLAN §5.1 rule 2) ------------------------------------------


def test_window_is_half_open_at_the_lower_edge(redis_client: Redis) -> None:
    """An event exactly one hour earlier is INSIDE [t - 1h, t)."""
    events = [
        make_event("T0000001", at(9), account_id=SECOND_ACCOUNT),
        make_event("T0000002", at(10), account_id=ACCOUNT),
    ]
    rows = _assert_parity(redis_client, events)
    assert rows[-1]["dev_accts_1h"] == 1


def test_window_excludes_an_event_a_millisecond_too_old(redis_client: Redis) -> None:
    start = at(10)
    events = [
        make_event(
            "T0000001", start - timedelta(hours=1, milliseconds=1), account_id=SECOND_ACCOUNT
        ),
        make_event("T0000002", start, account_id=ACCOUNT),
    ]
    rows = _assert_parity(redis_client, events)
    assert rows[-1]["dev_accts_1h"] == 0


def test_an_event_never_counts_itself(redis_client: Redis) -> None:
    """The upper bound is exclusive, so a simultaneous earlier touch is excluded."""
    moment = at(12)
    events = [
        make_event("T0000001", moment, account_id=SECOND_ACCOUNT),
        make_event("T0000002", moment, account_id=ACCOUNT),
    ]
    rows = _assert_parity(redis_client, events)
    # Both sit at exactly `now`; neither is inside [low, now).
    assert rows[-1]["dev_accts_1h"] == 0
    assert rows[0]["dev_accts_1h"] == 0


def test_thirty_day_boundary(redis_client: Redis) -> None:
    start = at(10)
    cfg = FeatureConfig.load()
    month = timedelta(seconds=cfg.month_seconds)
    events = [
        make_event("T0000001", start - month, account_id=SECOND_ACCOUNT),
        make_event(
            "T0000002",
            start - month - timedelta(milliseconds=1),
            account_id=THIRD_ACCOUNT,
            city="Delhi",
            location=DELHI,
        ),
        make_event("T0000003", start, account_id=ACCOUNT),
    ]
    rows = _assert_parity(redis_client, events)
    # Exactly 30 days back is in; a millisecond older is out.
    assert rows[-1]["dev_accts_30d"] == 1


def test_zadd_gt_never_moves_a_timestamp_backwards(redis_client: Redis) -> None:
    """Mirrors _EntityIndex.touch. A late arrival must not rewrite history."""
    store = RedisStore(redis_client)
    engine = FeatureEngine(store, _directory())

    engine.process(make_event("T0000001", at(15), account_id=SECOND_ACCOUNT))
    key = "ent:dev:D0000001"
    after_first = redis_client.zscore(key, SECOND_ACCOUNT)

    engine.process(make_event("T0000002", at(9), account_id=SECOND_ACCOUNT))
    assert redis_client.zscore(key, SECOND_ACCOUNT) == after_first


def test_scores_are_milliseconds(redis_client: Redis) -> None:
    """PLAN §3.6 says 'epoch s' but §5.1/§5.3 say milliseconds; ms is what parity needs.

    Seconds would collapse two events in the same second onto one score, and the two
    stores would then disagree. A double holds these integers exactly.
    """
    store = RedisStore(redis_client)
    event = make_event("T0000001", at(15))
    FeatureEngine(store, _directory()).process(event)

    expected = to_millis(event.event_time)
    assert redis_client.zscore("ent:dev:D0000001", ACCOUNT) == float(expected)
    assert float(expected).is_integer()


# --- key layout (PLAN §3.6) ---------------------------------------------------------


def test_keys_match_the_planned_schema(redis_client: Redis) -> None:
    event = make_event("T0000001", at(9))
    FeatureEngine(RedisStore(redis_client), _directory()).process(event)

    assert redis_client.exists(f"feat:{event.txn_id}")
    assert redis_client.exists(f"state:acct:{event.account_id}")
    assert redis_client.type(f"ent:dev:{event.device_id}") == "zset"
    assert redis_client.type(f"ent:ip:{event.ip}") == "zset"
    assert redis_client.type(f"ent:mer:{event.merchant_id}") == "zset"


def test_feature_record_carries_the_planned_ttl(redis_client: Redis) -> None:
    """48 hours: long enough to outlive a redelivery, short enough not to accumulate."""
    FeatureEngine(RedisStore(redis_client), _directory()).process(make_event("T1", at(9)))
    ttl = redis_client.ttl("feat:T1")
    assert 0 < ttl <= FeatureConfig.load().redis_feature_ttl_seconds


def test_account_state_has_no_expiry(redis_client: Redis) -> None:
    """§3.6 marks the account blob persistent; an expiry would silently reset history."""
    FeatureEngine(RedisStore(redis_client), _directory()).process(make_event("T1", at(9)))
    assert redis_client.ttl(f"state:acct:{ACCOUNT}") == -1


def test_state_blob_round_trips_through_redis(redis_client: Redis) -> None:
    store = RedisStore(redis_client)
    engine = FeatureEngine(store, _directory())
    for index in range(4):
        engine.process(make_event(f"T000000{index}", at(9, index * 3)))

    blob = redis_client.get(f"state:acct:{ACCOUNT}")
    assert AccountState.from_json(blob).to_json() == blob


# --- state version (PLAN §8) --------------------------------------------------------


def test_state_version_is_claimed_on_an_empty_database(redis_client: Redis) -> None:
    from fraud.features.spec import FEATURE_SPEC_VERSION

    store = RedisStore(redis_client)
    assert store.state_version() is None
    store.check_state_version()
    assert store.state_version() == FEATURE_SPEC_VERSION


def test_state_version_mismatch_refuses(redis_client: Redis) -> None:
    from fraud.features.store_redis import StateVersionMismatch

    store = RedisStore(redis_client)
    store.set_state_version("fs0")
    with pytest.raises(StateVersionMismatch, match="fresh backfill"):
        store.check_state_version()
