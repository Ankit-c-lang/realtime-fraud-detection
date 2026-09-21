"""Scorer service: the consumer that turns stream messages into scored rows (PLAN §9.2).

Everything else in this project is a function of its inputs. This module is the one place
with durable side effects on three systems at once — Redis state, Redis stream
acknowledgements, and Parquet on disk — so the order those happen in *is* the design.

**The ack comes last, and that is the whole point.** For each micro-batch:

    resolve snapshot -> engine.process (atomic commit) -> score -> sink.append
    -> flush (atomic rename) -> XACK the flushed ids -> watermark, metrics

An acknowledged message leaves the pending list forever; nothing will ever redeliver it.
So acking before the row is durable converts any crash into silently missing output,
which no downstream check can detect. Acking after means a crash in the gap redelivers
the message, the feature engine returns its stored record unchanged (§5.4), an identical
row is written twice, and the DuckDB view deduplicates on ``txn_id``. A visible duplicate
beats an invisible hole.

**The snapshot is resolved before the commit.** The chosen ``graph_snapshot_ts`` goes into
the committed feature record, so a redelivered event reuses the snapshot it originally
used rather than whatever has been published since. Without that, a retry would score
differently from the row already written and the §9.6 re-score check would fail for a
reason that has nothing to do with the model.

**Nothing here decides anything.** Thresholds, the blend weight and the decision tiers all
come from the artifact through ``RiskModel``, the same object the API uses. A second
implementation of "is this an alert" is how an online system drifts from its own report.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from redis import Redis

from fraud.config import Settings, load_yaml
from fraud.features.accounts import AccountDirectory
from fraud.features.engine import FeatureEngine
from fraud.features.spec import WARM_FEATURE_NAMES
from fraud.features.store_redis import RedisStore
from fraud.graph.refresh_live import fetch_graph_features, published_snapshots, resolve_snapshot
from fraud.modeling.decisions import ALLOW, HOLD
from fraud.schemas import EVENT_FIELDS, TransactionEvent
from fraud.scoring.risk_model import RiskModel
from fraud.stream.recovery import (
    NEW_MESSAGES,
    DeadLetterQueue,
    Message,
    drain_own_pending,
    ensure_group,
    reclaim,
)
from fraud.stream.sink import ParquetSink

logger = logging.getLogger(__name__)

MILLIS_PER_SECOND: Final[int] = 1000

HOLD_KEY: Final[str] = "hold:acct:{}"
ALERTS_KEY: Final[str] = "alerts:recent"
METRICS_KEY: Final[str] = "metrics:scorer"
LATENCY_KEY: Final[str] = "metrics:latency_ms"
WATERMARK_KEY: Final[str] = "sink:watermark"

# `crash_after` hook points, one per row of the §9.3 failure matrix.
CRASH_POINTS: Final[tuple[str, ...]] = ("commit", "score", "flush", "ack")


class InjectedCrash(RuntimeError):
    """Raised by the ``crash_after`` hook. Tests only; never raised in production."""


@dataclass(slots=True)
class ScorerMetrics:
    """Counters mirrored into ``metrics:scorer`` (PLAN §3.6, §9.2)."""

    events: int = 0
    alerts: int = 0
    holds: int = 0
    flushes: int = 0
    dead_lettered: int = 0
    reclaimed: int = 0
    batches: int = 0

    def totals(self) -> dict[str, int]:
        """Absolute counts for this process's lifetime."""
        return {
            "events": self.events,
            "alerts": self.alerts,
            "holds": self.holds,
            "flushes": self.flushes,
            "dead_lettered": self.dead_lettered,
            "reclaimed": self.reclaimed,
            "batches": self.batches,
        }


@dataclass(slots=True)
class _Prepared:
    """One message that parsed, committed and is ready to score."""

    message_id: str
    event: TransactionEvent
    record: dict[str, Any]
    snapshot_ts: int | None
    ingest_ts: int
    graph: dict[str, float] = field(default_factory=dict)


class Scorer:
    """The §9.2 consumer loop, with every side effect ordered deliberately."""

    def __init__(
        self,
        client: Redis,
        *,
        consumer: str | None = None,
        model: RiskModel | None = None,
        store: RedisStore | None = None,
        accounts: AccountDirectory | None = None,
        sink: ParquetSink | None = None,
        config: dict[str, Any] | None = None,
        scored_root: Path | None = None,
        crash_after: str | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        raw = config or load_yaml("stream")
        settings = Settings.from_env()

        self._redis = client
        self._stream = str(raw["stream"]["name"])
        self._group = str(raw["stream"]["group"])
        scorer = raw["scorer"]
        self._batch_size = int(scorer["batch_size"])
        self._block_ms = int(scorer["block_ms"])
        reclaim_cfg = scorer["reclaim"]
        self._reclaim_every = float(reclaim_cfg["every_seconds"])
        self._reclaim_idle_ms = int(reclaim_cfg["idle_ms"])
        self._reclaim_count = int(reclaim_cfg["count"])
        self._max_deliveries = int(reclaim_cfg["max_deliveries"])
        self._hold_ttl = int(scorer["hold_ttl_seconds"])
        self._alerts_keep = int(scorer["alerts_keep"])
        self._latency_keep = int(scorer["latency_keep"])

        self.consumer = consumer or settings.consumer_name
        self._dlq = DeadLetterQueue(
            client, str(raw["stream"]["dlq"]), int(raw["stream"]["dlq_maxlen"])
        )
        self._model = model
        self._store = store
        self._accounts = accounts
        # `sink or ParquetSink(...)` would be a bug: ParquetSink defines __len__, so an
        # empty sink is falsy and every injected sink would be silently replaced by one
        # writing to the real data/scored.
        self._sink = sink if sink is not None else ParquetSink(self.consumer, scored_root)
        self._engine: FeatureEngine | None = None

        if crash_after is not None and crash_after not in CRASH_POINTS:
            raise ValueError(f"crash_after must be one of {CRASH_POINTS}, got {crash_after!r}")
        self._crash_after = crash_after

        self._clock = clock
        self._last_reclaim = clock()
        self._stopping = False
        self.metrics = ScorerMetrics()
        # What has already been added to metrics:scorer, so each publish sends a delta.
        self._published: dict[str, int] = {}
        # End-to-end latencies for rows buffered but not yet flushed. Published with the
        # flush, so a sample only ever describes a row that actually became durable.
        self._latencies: list[int] = []

    # --- startup (PLAN §9.2) ---

    def startup(self) -> list[Message]:
        """Load the model, verify state, create the group, drain our own pending.

        The order matters: refusing to start on a stale artifact or a stale state version
        has to happen before a single message is claimed, or the scorer takes ownership of
        work it is not fit to do.
        """
        self._model = self._model or RiskModel.load()
        self._store = self._store or RedisStore(self._redis)
        self._store.check_state_version()

        if self._accounts is None:
            self._accounts = AccountDirectory.from_parquet(
                Settings.from_env().raw_dir / "accounts.parquet"
            )
        self._engine = FeatureEngine(self._store, self._accounts)

        ensure_group(self._redis, self._stream, self._group)
        pending = drain_own_pending(
            self._redis, self._stream, self._group, self.consumer, self._batch_size
        )
        logger.info(
            "scorer %s ready: model %s, %d pending message(s) to replay",
            self.consumer,
            self._model.model_version,
            len(pending),
        )
        return pending

    def install_signal_handlers(self) -> None:
        """SIGTERM stops the loop; the final flush happens on the way out (§9.2)."""
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, self._request_stop)

    def _request_stop(self, *_: Any) -> None:
        logger.info("stop requested; finishing the current batch and flushing")
        self._stopping = True

    # --- the loop ---

    def run(self, *, max_batches: int | None = None, stop_when_idle: bool = False) -> ScorerMetrics:
        """Consume until stopped, then flush and acknowledge what is left (§9.2).

        ``stop_when_idle`` exits once a read comes back empty. The service never uses it
        — it blocks and waits — but a bounded replay has to finish on its own, and so do
        the recovery tests, which would otherwise hang on an empty stream rather than
        fail.
        """
        pending = self.startup()
        if pending:
            self.handle(pending)

        batches = 0
        while not self._stopping and (max_batches is None or batches < max_batches):
            read = self.run_once()
            batches += 1
            if stop_when_idle and read == 0:
                break

        # The exit path is not an afterthought: docker stop sends SIGTERM, and anything
        # buffered but unflushed here would be re-delivered and re-scored on restart.
        self._flush_and_ack(force=True)
        logger.info("scorer %s stopped: %s", self.consumer, self.metrics.totals())
        return self.metrics

    def run_once(self) -> int:
        """One micro-batch: reclaim if due, read, process, flush if the policy says so."""
        if self._clock() - self._last_reclaim >= self._reclaim_every:
            self._reclaim()

        messages = self._read()
        if messages:
            self.handle(messages)

        self._flush_and_ack()
        return len(messages)

    def _read(self) -> list[Message]:
        batch = self._redis.xreadgroup(
            self._group,
            self.consumer,
            {self._stream: NEW_MESSAGES},
            count=self._batch_size,
            block=self._block_ms,
        )
        if not batch:
            return []
        return [(str(mid), fields) for _stream, entries in batch for mid, fields in entries]

    def _reclaim(self) -> None:
        self._last_reclaim = self._clock()
        before = self.metrics.dead_lettered
        claimed = reclaim(
            self._redis,
            self._stream,
            self._group,
            self.consumer,
            idle_ms=self._reclaim_idle_ms,
            count=self._reclaim_count,
            max_deliveries=self._max_deliveries,
            dlq=self._dlq,
        )
        self.metrics.dead_lettered = before  # recovery logs its own; counted in handle()
        if claimed:
            self.metrics.reclaimed += len(claimed)
            self.handle(claimed)

    # --- processing one batch (PLAN §9.2) ---

    def handle(self, messages: list[Message]) -> None:
        """Parse, commit, score and buffer a batch. Never acknowledges."""
        if not messages:
            return
        assert self._engine is not None and self._model is not None

        self.metrics.batches += 1
        published = published_snapshots(self._redis)
        prepared: list[_Prepared] = []

        for message_id, fields in messages:
            event = self._parse(message_id, fields)
            if event is None:
                continue

            snapshot_ts = resolve_snapshot(event.event_time, published)
            self._maybe_crash("commit")
            # Atomic and idempotent: a redelivery returns the stored record, including
            # the snapshot chosen the first time (§5.4, §9.3).
            record = self._engine.process(event, extra={"graph_snapshot_ts": snapshot_ts})
            prepared.append(
                _Prepared(
                    message_id=message_id,
                    event=event,
                    record=record,
                    snapshot_ts=record.get("graph_snapshot_ts", snapshot_ts),
                    ingest_ts=int(fields.get("ingest_ts", 0) or 0),
                )
            )

        if not prepared:
            return

        self._attach_graph(prepared)
        self._maybe_crash("score")
        self._score_and_buffer(prepared)

    def _parse(self, message_id: str, fields: dict[str, str]) -> TransactionEvent | None:
        """A message that cannot be read is poison, not a reason to stop (§9.3)."""
        try:
            return TransactionEvent.from_message(fields)
        except Exception as error:  # noqa: BLE001 - any malformed field lands here
            self._dlq.send(message_id, fields, f"unparseable message: {error}")
            self._redis.xack(self._stream, self._group, message_id)
            self.metrics.dead_lettered += 1
            return None

    def _attach_graph(self, prepared: list[_Prepared]) -> None:
        """One pipeline for the batch; a missing key means §6.3 defaults."""
        rows = fetch_graph_features(
            self._redis, [(item.snapshot_ts, item.event.account_id) for item in prepared]
        )
        for item, graph in zip(prepared, rows, strict=True):
            item.graph = graph

    def _score_and_buffer(self, prepared: list[_Prepared]) -> None:
        import pandas as pd

        assert self._model is not None
        frame = pd.DataFrame([{**item.record, **item.graph} for item in prepared])
        for name in WARM_FEATURE_NAMES:
            if name not in frame.columns:  # pragma: no cover - defaults always supply these
                frame[name] = 0.0

        scored = self._model.score_batch(frame)
        held = self._held_accounts([item.event.account_id for item in prepared])
        scored_ts = int(time.time() * MILLIS_PER_SECOND)

        rows: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        new_holds: list[str] = []

        for index, item in enumerate(prepared):
            decision = str(scored.decision[index])
            row = {
                **{name: getattr(item.event, name) for name in EVENT_FIELDS},
                **item.record,
                **item.graph,
                "graph_snapshot_ts": item.snapshot_ts,
                "p_xgb": float(scored.p_xgb[index]),
                "anomaly_pct": float(scored.anomaly_pct[index]),
                "risk": float(scored.risk[index]),
                "decision": decision,
                "reasons": json.dumps([r.to_dict() for r in scored.reasons[index]]),
                # Output only, never a model input (§7.7). It describes what the account
                # is already subject to, and feeding it back would let the system's own
                # past decisions become evidence for the next one.
                "account_on_hold": item.event.account_id in held,
                "model_version": scored.model_version,
                "feature_spec_version": scored.feature_spec_version,
                "ingest_ts": item.ingest_ts,
                "scored_ts": scored_ts,
                "consumer": self.consumer,
                "stream_id": item.message_id,
            }
            rows.append(row)
            if item.ingest_ts:
                # §9.6's end-to-end measure: stream write to score. Rows with no
                # ingest_ts came from something other than the replayer, and would
                # otherwise contribute a meaningless ~1.7e12 ms sample.
                self._latencies.append(scored_ts - item.ingest_ts)

            if decision != ALLOW:
                self.metrics.alerts += 1
                alerts.append(
                    {
                        "txn_id": item.event.txn_id,
                        "account_id": item.event.account_id,
                        "merchant_id": item.event.merchant_id,
                        "amount": item.event.amount,
                        "event_time": item.event.event_time.isoformat(),
                        "risk": row["risk"],
                        "decision": decision,
                        "reason": scored.reasons[index][0].text if scored.reasons[index] else "",
                    }
                )
            if decision == HOLD:
                new_holds.append(item.event.account_id)

        self._apply_holds(new_holds)
        self._push_alerts(alerts)
        self.metrics.events += len(rows)
        self._sink.append(rows, [item.message_id for item in prepared])

    def _held_accounts(self, account_ids: list[str]) -> set[str]:
        """Which accounts are already on HOLD, checked before this batch sets any."""
        unique = sorted(set(account_ids))
        if not unique:
            return set()
        pipe = self._redis.pipeline(transaction=False)
        for account_id in unique:
            pipe.exists(HOLD_KEY.format(account_id))
        return {
            account_id for account_id, exists in zip(unique, pipe.execute(), strict=True) if exists
        }

    def _apply_holds(self, account_ids: list[str]) -> None:
        if not account_ids:
            return
        pipe = self._redis.pipeline(transaction=False)
        for account_id in account_ids:
            pipe.set(HOLD_KEY.format(account_id), 1, ex=self._hold_ttl)
        pipe.execute()
        self.metrics.holds += len(account_ids)

    def _push_alerts(self, alerts: list[dict[str, Any]]) -> None:
        if not alerts:
            return
        pipe = self._redis.pipeline(transaction=False)
        for alert in alerts:
            pipe.lpush(ALERTS_KEY, json.dumps(alert, sort_keys=True))
        pipe.ltrim(ALERTS_KEY, 0, self._alerts_keep - 1)
        pipe.execute()

    # --- flush, then acknowledge (PLAN §9.2, §9.3) ---

    def _flush_and_ack(self, *, force: bool = False) -> None:
        if not force and not self._sink.should_flush():
            return

        self._maybe_crash("flush")
        result = self._sink.flush()
        if not result:
            return
        self.metrics.flushes += 1

        # Only now. Everything in `result.message_ids` is on disk under a name readers
        # can see; nothing else is.
        self._maybe_crash("ack")
        self._redis.xack(self._stream, self._group, *result.message_ids)
        self._publish(result.watermark, len(result.message_ids))
        self.record_latency(self._latencies)
        self._latencies.clear()

    def _publish(self, watermark: datetime | None, acked: int) -> None:
        pipe = self._redis.pipeline(transaction=False)
        if watermark is not None:
            pipe.set(WATERMARK_KEY, watermark.isoformat())
        # HINCRBY on the delta since the last publish, not HSET on the absolute count
        # (PLAN §9.2). HSET resets the dashboard's counters every time the scorer
        # restarts, so a crash mid-run makes the totals go backwards and the numbers
        # stop meaning "events processed".
        totals = self.metrics.totals()
        for name, value in totals.items():
            delta = value - self._published.get(name, 0)
            if delta:
                pipe.hincrby(METRICS_KEY, name, delta)
        self._published = totals
        pipe.hset(METRICS_KEY, "heartbeat", datetime.now().isoformat(timespec="seconds"))  # noqa: DTZ005
        pipe.hset(METRICS_KEY, "consumer", self.consumer)
        pipe.execute()
        logger.debug("acked %d message(s), watermark %s", acked, watermark)

    def record_latency(self, milliseconds: list[int]) -> None:
        """Bounded latency samples for the benchmark (PLAN §3.6, §9.6)."""
        if not milliseconds:
            return
        pipe = self._redis.pipeline(transaction=False)
        for value in milliseconds:
            pipe.lpush(LATENCY_KEY, value)
        pipe.ltrim(LATENCY_KEY, 0, self._latency_keep - 1)
        pipe.execute()

    # --- test hook (PLAN §9.3) ---

    def _maybe_crash(self, point: str) -> None:
        if self._crash_after == point:
            self._crash_after = None  # one shot, so a restart makes progress
            raise InjectedCrash(f"crash_after={point}")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - service entrypoint
    """``python -m fraud.stream.scorer`` (PLAN §9.2)."""
    import argparse

    parser = argparse.ArgumentParser(description="Score the event stream (PLAN §9.2).")
    parser.add_argument("--consumer", default=None, help="consumer name; default CONSUMER_NAME")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    client = Redis.from_url(settings.redis_url, decode_responses=True)

    scorer = Scorer(client, consumer=args.consumer)
    scorer.install_signal_handlers()
    scorer.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
