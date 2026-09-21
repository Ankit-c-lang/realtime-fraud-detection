"""The scorer under failure: every row of the §9.3 matrix (PLAN §9.2-§9.3, §13).

The scorer has durable side effects on three systems — Redis state, stream
acknowledgements, and Parquet — and a crash can land between any two of them. §9.3 sets
out what each gap must produce, and every row of that table gets a test here, driven by
the `crash_after` hook.

The property that matters most is asymmetric. A crash *before* the flush must leave the
message pending, because an acknowledged message is gone forever and nothing will ever
redeliver it: acking early turns a crash into silently missing output that no downstream
check can find. A crash *after* the flush is allowed to write a duplicate row, because
the redelivered event returns its stored feature record and the DuckDB view deduplicates
on `txn_id`. A visible duplicate beats an invisible hole, and these tests pin that
direction.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from feature_helpers import ACCOUNT, CREATED_AT, HOME_CITY, MUMBAI, directory, make_event
from redis import Redis

from fraud.config import load_yaml
from fraud.features.store_redis import RedisStore, StateVersionMismatch
from fraud.graph.algorithms import defaults as graph_defaults
from fraud.graph.refresh_live import PUBLISHED_KEY, snapshot_key, to_epoch_seconds
from fraud.modeling.decisions import ALLOW, HOLD, REVIEW, Thresholds
from fraud.schemas import EVENT_FIELDS
from fraud.storage import duck
from fraud.stream.recovery import (
    DeadLetterQueue,
    PendingEntry,
    drain_own_pending,
    ensure_group,
    pending_entries,
    reclaim,
)
from fraud.stream.replayer import MaxPacer, Replayer
from fraud.stream.scorer import (
    ALERTS_KEY,
    CRASH_POINTS,
    HOLD_KEY,
    METRICS_KEY,
    WATERMARK_KEY,
    InjectedCrash,
    Scorer,
)
from fraud.stream.sink import ParquetSink

pytestmark = pytest.mark.redis

START = datetime(2026, 3, 14, 10, 0, 0)  # noqa: DTZ001 - naive IST, per PLAN §3.5
SECOND_ACCOUNT = "A0000002"


# --- a tiny fake model, so these tests exercise the scorer rather than XGBoost --------


class FakeScored:
    def __init__(self, rows: int, decisions: list[str]) -> None:
        import numpy as np

        self.p_xgb = np.full(rows, 0.6)
        self.anomaly_pct = np.full(rows, 0.4)
        self.risk = np.full(rows, 0.5)
        self.decision = np.array(decisions, dtype=object)
        self.reasons = [[_Reason()] for _ in range(rows)]
        self.model_version = "v1"
        self.feature_spec_version = "fs1"


class _Reason:
    text = "8 payments in the previous 5 minutes"

    def to_dict(self) -> dict[str, Any]:
        return {"feature": "acct_cnt_5m", "value": 8.0, "contribution": 1.5, "text": self.text}


class FakeModel:
    """Decides by a rule the test controls, so decisions are predictable."""

    def __init__(self, decide: Any = None) -> None:
        self._decide = decide or (lambda frame: [ALLOW] * len(frame))
        self.model_version = "v1"
        self.seen: list[pd.DataFrame] = []

    def score_batch(self, frame: pd.DataFrame, *, explain: bool = True) -> FakeScored:
        self.seen.append(frame.copy())
        return FakeScored(len(frame), list(self._decide(frame)))

    @property
    def bundle(self) -> Any:  # pragma: no cover - only startup logging touches this
        class _Bundle:
            thresholds = Thresholds(0.5, None, 0.02, 0.9, 0.9, 1, 0.01)
            blend_weight = 0.5
            model_version = "v1"

        return _Bundle()


def _directory() -> Any:
    return directory(
        {ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT), SECOND_ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT)}
    )


def _scorer(client: Redis, tmp_path: Path, **kwargs: Any) -> Scorer:
    model = kwargs.pop("model", None) or FakeModel()
    store = RedisStore(client)
    store.set_state_version()
    consumer = kwargs.pop("consumer", "scorer-1")
    return Scorer(
        client,
        consumer=consumer,
        model=model,
        store=store,
        accounts=_directory(),
        # Flush on every batch. The default policy waits for 2,000 rows or 2 seconds,
        # which would leave the "flush" and "ack" crash points unreachable in a test
        # that sends three events — the hooks would silently never fire.
        sink=ParquetSink(consumer, tmp_path, max_rows=1),
        **kwargs,
    )


def _publish(client: Redis, events: list[Any]) -> list[str]:
    frame = pd.DataFrame(
        [{field: getattr(event, field) for field in EVENT_FIELDS} for event in events]
    )
    Replayer(client, MaxPacer()).send(frame)
    stream = load_yaml("stream")["stream"]["name"]
    return [mid for mid, _ in client.xrange(stream)]


def _events(count: int, *, account_id: str = ACCOUNT) -> list[Any]:
    return [
        make_event(f"T{i:07d}", START + timedelta(minutes=i), account_id=account_id)
        for i in range(count)
    ]


def _stream_group(client: Redis) -> tuple[str, str]:
    raw = load_yaml("stream")["stream"]
    return raw["name"], raw["group"]


def _pending_count(client: Redis) -> int:
    stream, group = _stream_group(client)
    return int(client.xpending(stream, group)["pending"])


def _scored_rows(root: Path) -> pd.DataFrame:
    files = sorted(root.glob("*/*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)


# --- happy path ---------------------------------------------------------------------


def test_a_batch_is_scored_written_and_acked(redis_client: Redis, tmp_path: Path) -> None:
    _publish(redis_client, _events(3))
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    rows = _scored_rows(tmp_path)
    assert len(rows) == 3
    assert _pending_count(redis_client) == 0
    assert scorer.metrics.events == 3


def test_scored_row_has_the_planned_schema(redis_client: Redis, tmp_path: Path) -> None:
    """PLAN §9.2's scored-row schema, including the transport columns."""
    _publish(redis_client, _events(1))
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    row = _scored_rows(tmp_path).iloc[0]
    for column in (
        "txn_id",
        "event_time",
        "account_id",
        "amount",
        "status",  # §3.5 event
        "acct_cnt_5m",
        "geo_speed_kmh",
        "dev_accts_30d",  # hot features
        "graph_degree",
        "community_young_share",
        "ppr_risk",  # graph features
        "graph_snapshot_ts",
        "p_xgb",
        "anomaly_pct",
        "risk",
        "decision",
        "reasons",
        "account_on_hold",
        "model_version",
        "feature_spec_version",
        "ingest_ts",
        "scored_ts",
        "consumer",
        "stream_id",
    ):
        assert column in row.index, column
    assert json.loads(row["reasons"])[0]["feature"] == "acct_cnt_5m"
    assert row["consumer"] == "scorer-1"


def test_labels_never_appear_in_a_scored_row(redis_client: Redis, tmp_path: Path) -> None:
    _publish(redis_client, _events(2))
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    columns = set(_scored_rows(tmp_path).columns)
    assert columns & {"is_fraud", "fraud_type", "attack_id", "ring_id"} == set()


# --- §9.3 row 1: crash BEFORE the feature commit -------------------------------------


def test_crash_before_commit_leaves_state_and_output_untouched(
    redis_client: Redis, tmp_path: Path
) -> None:
    """Redis unchanged, no Parquet, message still pending — reprocessed normally."""
    _publish(redis_client, _events(2))
    scorer = _scorer(redis_client, tmp_path, crash_after="commit")
    scorer.startup()

    with pytest.raises(InjectedCrash):
        scorer.run_once()

    assert redis_client.keys("state:acct:*") == []
    assert redis_client.keys("feat:*") == []
    assert list(tmp_path.glob("*/*.parquet")) == []
    assert _pending_count(redis_client) == 2


def test_restart_after_a_pre_commit_crash_scores_everything(
    redis_client: Redis, tmp_path: Path
) -> None:
    _publish(redis_client, _events(2))
    crashed = _scorer(redis_client, tmp_path, crash_after="commit")
    crashed.startup()
    with pytest.raises(InjectedCrash):
        crashed.run_once()

    restarted = _scorer(redis_client, tmp_path)
    restarted.run(
        stop_when_idle=True
    )  # startup drains its own pending, then the loop flushes on exit

    assert len(_scored_rows(tmp_path)) == 2
    assert _pending_count(redis_client) == 0


# --- §9.3 row 2: crash AFTER the commit, BEFORE the flush ----------------------------


def test_crash_after_commit_before_flush_writes_no_row(redis_client: Redis, tmp_path: Path) -> None:
    """State updated once, nothing on disk, message still pending."""
    _publish(redis_client, _events(2))
    scorer = _scorer(redis_client, tmp_path, crash_after="flush")
    scorer.startup()

    with pytest.raises(InjectedCrash):
        scorer.run_once()
        scorer._flush_and_ack(force=True)

    assert redis_client.keys("feat:*")  # the commit happened
    assert list(tmp_path.glob("*/*.parquet")) == []
    assert _pending_count(redis_client) == 2


def test_the_row_is_written_once_after_that_restart(redis_client: Redis, tmp_path: Path) -> None:
    """The redelivered event returns its stored record, so the row appears exactly once."""
    _publish(redis_client, _events(2))
    crashed = _scorer(redis_client, tmp_path, crash_after="flush")
    crashed.startup()
    with pytest.raises(InjectedCrash):
        crashed.run_once()

    restarted = _scorer(redis_client, tmp_path)
    restarted.run(stop_when_idle=True)

    rows = _scored_rows(tmp_path)
    assert len(rows) == 2
    assert rows["txn_id"].nunique() == 2


# --- §9.3 row 3: crash AFTER the flush, BEFORE the ack -------------------------------


def test_crash_after_flush_before_ack_keeps_the_message_pending(
    redis_client: Redis, tmp_path: Path
) -> None:
    """The row is durable but unacknowledged — deliberately, so nothing is lost."""
    _publish(redis_client, _events(2))
    scorer = _scorer(redis_client, tmp_path, crash_after="ack")
    scorer.startup()

    with pytest.raises(InjectedCrash):
        scorer.run_once()
        scorer._flush_and_ack(force=True)

    assert len(_scored_rows(tmp_path)) == 2
    assert _pending_count(redis_client) == 2  # not acked


def test_the_duplicate_row_is_identical_and_readers_deduplicate(
    redis_client: Redis, tmp_path: Path
) -> None:
    """§9.3 accepts the duplicate; the DuckDB view is what resolves it."""
    _publish(redis_client, _events(2))
    crashed = _scorer(redis_client, tmp_path, crash_after="ack")
    crashed.startup()
    with pytest.raises(InjectedCrash):
        crashed.run_once()

    restarted = _scorer(redis_client, tmp_path)
    restarted.run(stop_when_idle=True)

    rows = _scored_rows(tmp_path)
    assert len(rows) == 4  # written twice, on purpose
    assert _pending_count(redis_client) == 0

    connection = duck.connect(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM scored").fetchone()[0] == 2
    finally:
        connection.close()

    # The duplicate must be identical, not merely present: the features come from the
    # stored record, so a retry cannot recompute them against newer state.
    first = rows[rows["txn_id"] == rows["txn_id"].iloc[0]]
    assert first["risk"].nunique() == 1
    assert first["acct_cnt_5m"].nunique() == 1
    # dropna=False: no snapshot is published here, so both copies are None — which is
    # still "identical", and the default nunique() would report 0 and hide that.
    assert first["graph_snapshot_ts"].nunique(dropna=False) == 1


# --- §9.3 row 4: poison message ------------------------------------------------------


def test_an_unparseable_message_is_dead_lettered_and_acked(
    redis_client: Redis, tmp_path: Path
) -> None:
    """A bad message must not stall the stream behind it."""
    stream, _ = _stream_group(redis_client)
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    redis_client.xadd(stream, {"txn_id": "BAD", "event_time": "not-a-date"})

    scorer.run_once()

    dlq = load_yaml("stream")["stream"]["dlq"]
    entries = redis_client.xrange(dlq)
    assert len(entries) == 1
    assert "unparseable" in entries[0][1]["dlq_reason"]
    assert _pending_count(redis_client) == 0
    assert scorer.metrics.dead_lettered == 1


def test_a_poison_message_does_not_block_good_ones(redis_client: Redis, tmp_path: Path) -> None:
    stream, _ = _stream_group(redis_client)
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    redis_client.xadd(stream, {"txn_id": "BAD", "amount": "oops"})
    _publish(redis_client, _events(2))

    scorer.run_once()
    scorer._flush_and_ack(force=True)

    assert len(_scored_rows(tmp_path)) == 2
    assert len(redis_client.xrange(load_yaml("stream")["stream"]["dlq"])) == 1


# --- pending drain and reclaim (PLAN §9.2) -------------------------------------------


def test_startup_drains_this_consumers_own_pending(redis_client: Redis, tmp_path: Path) -> None:
    """Reading `>` returns only new messages; own pending is reachable only from id 0."""
    _publish(redis_client, _events(3))
    stream, group = _stream_group(redis_client)
    ensure_group(redis_client, stream, group)
    redis_client.xreadgroup(group, "scorer-1", {stream: ">"}, count=10)  # claimed, unacked

    assert _pending_count(redis_client) == 3
    drained = drain_own_pending(redis_client, stream, group, "scorer-1", 200)
    assert len(drained) == 3


def test_draining_pages_past_the_batch_size(redis_client: Redis, tmp_path: Path) -> None:
    """More pending messages than one read returns, which is where the cursor matters.

    Reading with id "0" returns the consumer's pending entries from the beginning every
    time. Without advancing the cursor this loop re-reads the same page forever: it
    reported draining 19,900 messages when 201 were pending, and handed the scorer the
    same events over and over. Only a fixture larger than `count` can catch that, which
    is why this test exists alongside the three-message one.
    """
    _publish(redis_client, _events(25))
    stream, group = _stream_group(redis_client)
    ensure_group(redis_client, stream, group)
    redis_client.xreadgroup(group, "scorer-1", {stream: ">"}, count=100)

    drained = drain_own_pending(redis_client, stream, group, "scorer-1", 10)
    assert len(drained) == 25
    assert len({message_id for message_id, _ in drained}) == 25  # no repeats


def test_draining_an_empty_pending_list_returns_nothing(redis_client: Redis) -> None:
    _publish(redis_client, _events(3))
    stream, group = _stream_group(redis_client)
    ensure_group(redis_client, stream, group)

    assert drain_own_pending(redis_client, stream, group, "scorer-1", 10) == []


def test_a_restarted_scorer_replays_its_pending_work(redis_client: Redis, tmp_path: Path) -> None:
    _publish(redis_client, _events(3))
    stream, group = _stream_group(redis_client)
    ensure_group(redis_client, stream, group)
    redis_client.xreadgroup(group, "scorer-1", {stream: ">"}, count=10)

    restarted = _scorer(redis_client, tmp_path, consumer="scorer-1")
    restarted.run(stop_when_idle=True)

    assert len(_scored_rows(tmp_path)) == 3
    assert _pending_count(redis_client) == 0


def test_reclaim_takes_over_an_abandoned_message(redis_client: Redis, tmp_path: Path) -> None:
    _publish(redis_client, _events(2))
    stream, group = _stream_group(redis_client)
    ensure_group(redis_client, stream, group)
    redis_client.xreadgroup(group, "dead-consumer", {stream: ">"}, count=10)

    dlq = DeadLetterQueue(redis_client, "txn:dlq", 100)
    claimed = reclaim(
        redis_client,
        stream,
        group,
        "scorer-2",
        idle_ms=0,
        count=100,
        max_deliveries=3,
        dlq=dlq,
    )
    assert len(claimed) == 2
    assert redis_client.xrange("txn:dlq") == []


def test_reclaim_ignores_messages_that_are_not_idle(redis_client: Redis, tmp_path: Path) -> None:
    """A live consumer's in-flight work must not be stolen out from under it."""
    _publish(redis_client, _events(2))
    stream, group = _stream_group(redis_client)
    ensure_group(redis_client, stream, group)
    redis_client.xreadgroup(group, "busy-consumer", {stream: ">"}, count=10)

    dlq = DeadLetterQueue(redis_client, "txn:dlq", 100)
    claimed = reclaim(
        redis_client,
        stream,
        group,
        "scorer-2",
        idle_ms=60_000,
        count=100,
        max_deliveries=3,
        dlq=dlq,
    )
    assert claimed == []


def test_repeatedly_redelivered_messages_go_to_the_dlq(redis_client: Redis, tmp_path: Path) -> None:
    """Past the delivery limit a message is poison, and retrying stalls the stream."""
    _publish(redis_client, _events(1))
    stream, group = _stream_group(redis_client)
    ensure_group(redis_client, stream, group)

    dlq = DeadLetterQueue(redis_client, "txn:dlq", 100)
    # The first read makes it pending; XCLAIM is what increments the delivery counter,
    # so four claims take it past the limit of 3. Reading from "0" would not — it
    # returns a consumer's own pending entries without counting a new delivery.
    redis_client.xreadgroup(group, "flaky", {stream: ">"}, count=10)
    message_ids = [mid for mid, _ in redis_client.xrange(stream)]
    for _ in range(4):
        redis_client.xclaim(stream, group, "flaky", min_idle_time=0, message_ids=message_ids)

    reclaim(
        redis_client,
        stream,
        group,
        "scorer-2",
        idle_ms=0,
        count=100,
        max_deliveries=3,
        dlq=dlq,
    )

    entries = redis_client.xrange("txn:dlq")
    assert len(entries) == 1
    assert "poison message" in entries[0][1]["dlq_reason"]
    assert _pending_count(redis_client) == 0  # acked, so it stops coming back


def test_pending_entries_reports_the_delivery_count(redis_client: Redis) -> None:
    _publish(redis_client, _events(1))
    stream, group = _stream_group(redis_client)
    ensure_group(redis_client, stream, group)
    redis_client.xreadgroup(group, "c1", {stream: ">"}, count=10)

    entries = pending_entries(redis_client, stream, group, idle_ms=0, count=10)
    assert len(entries) == 1
    assert isinstance(entries[0], PendingEntry)
    assert entries[0].delivery_count == 1
    assert entries[0].consumer == "c1"


def test_group_creation_is_idempotent(redis_client: Redis) -> None:
    stream, group = _stream_group(redis_client)
    assert ensure_group(redis_client, stream, group) is True
    assert ensure_group(redis_client, stream, group) is False


def test_group_is_created_at_zero_so_no_early_event_is_missed(redis_client: Redis) -> None:
    """Created at `$` instead, everything already in the stream would be skipped."""
    _publish(redis_client, _events(3))
    stream, group = _stream_group(redis_client)
    ensure_group(redis_client, stream, group)

    batch = redis_client.xreadgroup(group, "c1", {stream: ">"}, count=10)
    assert len(batch[0][1]) == 3


# --- graph snapshot resolution (PLAN §6.5) -------------------------------------------


def test_events_before_any_snapshot_get_graph_defaults(redis_client: Redis, tmp_path: Path) -> None:
    _publish(redis_client, _events(1))
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    row = _scored_rows(tmp_path).iloc[0]
    assert pd.isna(row["graph_snapshot_ts"]) or row["graph_snapshot_ts"] is None
    for name, value in graph_defaults().items():
        assert row[name] == value


def test_the_snapshot_at_or_before_the_event_is_used(redis_client: Redis, tmp_path: Path) -> None:
    """Taking the newest snapshot instead would hand the event its own future (L3)."""
    older = to_epoch_seconds(datetime(2026, 3, 13))  # noqa: DTZ001
    newer = to_epoch_seconds(datetime(2026, 3, 20))  # noqa: DTZ001 - after the event
    redis_client.zadd(PUBLISHED_KEY, {str(older): older, str(newer): newer})
    redis_client.hset(snapshot_key(older, ACCOUNT), mapping={"graph_degree": "7"})
    redis_client.hset(snapshot_key(newer, ACCOUNT), mapping={"graph_degree": "99"})

    _publish(redis_client, _events(1))
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    row = _scored_rows(tmp_path).iloc[0]
    assert row["graph_snapshot_ts"] == older
    assert row["graph_degree"] == 7.0


def test_a_redelivery_reuses_the_original_snapshot(redis_client: Redis, tmp_path: Path) -> None:
    """The snapshot is committed with the record, so a retry cannot score differently."""
    older = to_epoch_seconds(datetime(2026, 3, 13))  # noqa: DTZ001
    redis_client.zadd(PUBLISHED_KEY, {str(older): older})

    _publish(redis_client, _events(1))
    crashed = _scorer(redis_client, tmp_path, crash_after="ack")
    crashed.startup()
    with pytest.raises(InjectedCrash):
        crashed.run_once()

    # A newer snapshot is published in the gap before the retry.
    newer = to_epoch_seconds(datetime(2026, 3, 14, 9))  # noqa: DTZ001
    redis_client.zadd(PUBLISHED_KEY, {str(newer): newer})

    restarted = _scorer(redis_client, tmp_path)
    restarted.run(stop_when_idle=True)

    rows = _scored_rows(tmp_path)
    assert len(rows) == 2
    assert rows["graph_snapshot_ts"].nunique() == 1
    assert rows["graph_snapshot_ts"].iloc[0] == older


def test_a_missing_account_in_a_snapshot_falls_back_to_defaults(
    redis_client: Redis, tmp_path: Path
) -> None:
    older = to_epoch_seconds(datetime(2026, 3, 13))  # noqa: DTZ001
    redis_client.zadd(PUBLISHED_KEY, {str(older): older})  # published, but no account key

    _publish(redis_client, _events(1))
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    row = _scored_rows(tmp_path).iloc[0]
    assert row["graph_degree"] == graph_defaults()["graph_degree"]


# --- HOLD annotation (PLAN §7.7) ------------------------------------------------------


def test_a_hold_decision_freezes_the_account(redis_client: Redis, tmp_path: Path) -> None:
    model = FakeModel(lambda frame: [HOLD] * len(frame))
    _publish(redis_client, _events(1))
    scorer = _scorer(redis_client, tmp_path, model=model)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    key = HOLD_KEY.format(ACCOUNT)
    assert redis_client.exists(key)
    assert 0 < redis_client.ttl(key) <= load_yaml("stream")["scorer"]["hold_ttl_seconds"]


def test_later_events_on_a_held_account_are_annotated(redis_client: Redis, tmp_path: Path) -> None:
    redis_client.set(HOLD_KEY.format(ACCOUNT), 1, ex=600)
    _publish(redis_client, _events(1))

    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    assert bool(_scored_rows(tmp_path).iloc[0]["account_on_hold"]) is True


def test_the_hold_annotation_is_never_a_model_input(redis_client: Redis, tmp_path: Path) -> None:
    """§7.7: output only. Feeding it back would make past decisions their own evidence."""
    redis_client.set(HOLD_KEY.format(ACCOUNT), 1, ex=600)
    _publish(redis_client, _events(1))

    model = FakeModel()
    scorer = _scorer(redis_client, tmp_path, model=model)
    scorer.startup()
    scorer.run_once()

    assert "account_on_hold" not in model.seen[0].columns


def test_an_allow_decision_sets_no_hold(redis_client: Redis, tmp_path: Path) -> None:
    _publish(redis_client, _events(1))
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()

    assert redis_client.keys("hold:acct:*") == []


# --- alerts, metrics and the watermark (PLAN §3.6, §9.2) ------------------------------


def test_alerts_are_published_and_bounded(redis_client: Redis, tmp_path: Path) -> None:
    model = FakeModel(lambda frame: [REVIEW] * len(frame))
    _publish(redis_client, _events(3))
    scorer = _scorer(redis_client, tmp_path, model=model)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    alerts = redis_client.lrange(ALERTS_KEY, 0, -1)
    assert len(alerts) == 3
    assert json.loads(alerts[0])["decision"] == REVIEW
    assert scorer.metrics.alerts == 3


def test_the_watermark_is_the_largest_flushed_event_time(
    redis_client: Redis, tmp_path: Path
) -> None:
    """§6.5 drives the graph refresh off this; processing is ordered, so max is valid."""
    events = _events(4)
    _publish(redis_client, events)
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    assert redis_client.get(WATERMARK_KEY) == events[-1].event_time.isoformat()


def test_the_watermark_only_moves_after_a_flush(redis_client: Redis, tmp_path: Path) -> None:
    _publish(redis_client, _events(2))
    scorer = _scorer(redis_client, tmp_path, crash_after="flush")
    scorer.startup()
    with pytest.raises(InjectedCrash):
        scorer.run_once()

    assert redis_client.get(WATERMARK_KEY) is None


def test_metrics_reach_redis(redis_client: Redis, tmp_path: Path) -> None:
    _publish(redis_client, _events(2))
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)

    metrics = redis_client.hgetall(METRICS_KEY)
    assert int(metrics["events"]) == 2
    assert int(metrics["flushes"]) == 1
    assert metrics["consumer"] == "scorer-1"
    assert metrics["heartbeat"]


def test_metrics_accumulate_across_a_restart(redis_client: Redis, tmp_path: Path) -> None:
    """HINCRBY on deltas, not HSET on absolutes (§9.2).

    With HSET the counters reset every time the scorer restarts, so a crash makes the
    dashboard's totals go backwards and "events processed" stops meaning anything.
    """
    _publish(redis_client, _events(2))
    first = _scorer(redis_client, tmp_path)
    first.startup()
    first.run_once()
    first._flush_and_ack(force=True)
    assert int(redis_client.hget(METRICS_KEY, "events")) == 2

    _publish(redis_client, _events(3))
    second = _scorer(redis_client, tmp_path, consumer="scorer-1")
    second.startup()
    second.run_once()
    second._flush_and_ack(force=True)

    # 2 from the first process plus 3 from the second, not 3.
    assert int(redis_client.hget(METRICS_KEY, "events")) == 5


def test_repeated_publishes_do_not_double_count(redis_client: Redis, tmp_path: Path) -> None:
    """Each publish sends only what has happened since the last one."""
    _publish(redis_client, _events(2))
    scorer = _scorer(redis_client, tmp_path)
    scorer.startup()
    scorer.run_once()
    scorer._flush_and_ack(force=True)
    scorer._publish(None, 0)
    scorer._publish(None, 0)

    assert int(redis_client.hget(METRICS_KEY, "events")) == 2


def test_latency_samples_are_bounded(redis_client: Redis, tmp_path: Path) -> None:
    from fraud.stream.scorer import LATENCY_KEY

    scorer = _scorer(redis_client, tmp_path)
    scorer.record_latency(list(range(1200)))
    assert redis_client.llen(LATENCY_KEY) == load_yaml("stream")["scorer"]["latency_keep"]


# --- startup refusals (PLAN §8) -------------------------------------------------------


def test_a_stale_state_version_refuses_to_start(redis_client: Redis, tmp_path: Path) -> None:
    """Blobs built for another spec mean something else now; a fresh backfill is the fix."""
    RedisStore(redis_client).set_state_version("fs0")
    store = RedisStore(redis_client)
    scorer = Scorer(
        redis_client,
        consumer="scorer-1",
        model=FakeModel(),
        store=store,
        accounts=_directory(),
        scored_root=tmp_path,
    )
    with pytest.raises(StateVersionMismatch):
        scorer.startup()


def test_an_unknown_crash_point_is_refused(redis_client: Redis, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="crash_after must be one of"):
        _scorer(redis_client, tmp_path, crash_after="whenever")


def test_every_documented_crash_point_is_reachable(redis_client: Redis, tmp_path: Path) -> None:
    """Each §9.3 row has a hook; a silently dead hook would make those tests vacuous."""
    for point in CRASH_POINTS:
        client_rows = tmp_path / point
        _publish(redis_client, _events(1))
        scorer = _scorer(redis_client, client_rows, crash_after=point)
        scorer.startup()
        with pytest.raises(InjectedCrash, match=point):
            scorer.run_once()
            scorer._flush_and_ack(force=True)
        redis_client.flushdb()
        RedisStore(redis_client).set_state_version()


# --- SIGTERM (PLAN §9.2) --------------------------------------------------------------


def test_the_exit_path_flushes_and_acks(redis_client: Redis, tmp_path: Path) -> None:
    """docker stop sends SIGTERM; anything unflushed would be re-scored on restart."""
    _publish(redis_client, _events(3))
    scorer = _scorer(redis_client, tmp_path)
    scorer._sink = ParquetSink("scorer-1", tmp_path, max_rows=9999, max_seconds=999.0)

    scorer.run(max_batches=1)  # the policy never fires; only the exit path can flush

    assert len(_scored_rows(tmp_path)) == 3
    assert _pending_count(redis_client) == 0


def test_a_stop_request_ends_the_loop(redis_client: Redis, tmp_path: Path) -> None:
    _publish(redis_client, _events(3))
    scorer = _scorer(redis_client, tmp_path)
    scorer._request_stop()
    scorer.run()

    assert scorer.metrics.batches == 0  # returned without consuming
    # Nothing was delivered, so nothing is pending; the work is still in the stream
    # for whoever starts next.
    assert redis_client.xlen(_stream_group(redis_client)[0]) == 3
