"""Producer: paces the test window into the stream with backpressure (PLAN §9.1).

This is the only writer of ``txn:events``. It reads the replay window straight from
``events.parquet``, keeps the §3.5 columns and nothing else, stamps each message with an
ingest time, and hands it to Redis at whatever speed was asked for.

**Why it reads the test window, and why that is not the guarded read.** `splits.yaml`
designates the test split as "also the window replayed live through Redis". The guard in
`modeling/splits.py` protects *labelled evaluation*: it stops anybody scoring or tuning
against `labels.parquet` before the one sanctioned run. Streaming raw, unlabelled events
is a different act entirely, so this module deliberately does not call
``splits.load("test")`` — it takes only the window boundaries from ``splits.windows()``,
which reads `configs/splits.yaml` and no data at all. Nothing here appends to
`reports/test_runs.log` or touches `metrics.test`.

**Labels are never in the stream (§3.5).** That is not a comment, it is checked twice:
once when the frame is loaded and again on the fields of every message built. A label
reaching the scorer would make the online metrics meaningless in a way that looks like
success, which is the worst kind of bug to ship.

**Ordering is total, not merely sorted by time.** Hundreds of events share a timestamp in
this data, and "sorted by event_time" leaves their order undefined. The simulator assigns
``txn_id`` after sorting by time, so ``(event_time, txn_id)`` is a total order that
reproduces the offline replay exactly — which is what makes the §9.6 re-score check a
comparison rather than a coin flip.

**Backpressure has two thresholds on purpose.** Pausing above 20,000 and resuming below
5,000 gives the consumer room to actually drain. One threshold would resume the instant
it dipped under, and the producer would spend its time flapping.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

import pandas as pd
from redis import Redis

from fraud.config import Settings, load_yaml
from fraud.schemas import EVENT_FIELDS, SCHEMA_VERSION

logger = logging.getLogger(__name__)

MILLIS_PER_SECOND: Final[int] = 1000

# Anything that identifies fraud. Checked against the loaded frame and every message.
LABEL_FIELDS: Final[frozenset[str]] = frozenset(
    {"is_fraud", "fraud_type", "attack_id", "ring_id", "label", "label_available_at"}
)

# The full stream message: the event plus the two transport fields §3.5 adds.
MESSAGE_FIELDS: Final[tuple[str, ...]] = (*EVENT_FIELDS, "ingest_ts", "schema_version")


class LabelLeak(RuntimeError):
    """A label reached the stream path. Never recoverable; always a bug (PLAN §3.5)."""


# --- pacing (PLAN §9.1) -------------------------------------------------------------


class Pacer(Protocol):
    """Decides when event ``index`` should be sent, in seconds since the run started."""

    def offset(self, index: int, event_time: datetime, first: datetime) -> float | None: ...


@dataclass(frozen=True, slots=True)
class SpeedupPacer:
    """``--speedup S``: event i goes out at ``wall0 + (t_i - t_0) / S`` (§9.1).

    This is the mode that makes a demo watchable: event time keeps its shape, including
    the overnight lull and the morning peak, just compressed.
    """

    speedup: float

    def __post_init__(self) -> None:
        if self.speedup <= 0:
            raise ValueError(f"--speedup must be positive, got {self.speedup}")

    def offset(self, index: int, event_time: datetime, first: datetime) -> float:
        return (event_time - first).total_seconds() / self.speedup


@dataclass(frozen=True, slots=True)
class RatePacer:
    """``--rate R``: a flat R events per second, ignoring event time (§9.1)."""

    rate: float

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError(f"--rate must be positive, got {self.rate}")

    def offset(self, index: int, event_time: datetime, first: datetime) -> float:
        return index / self.rate


@dataclass(frozen=True, slots=True)
class MaxPacer:
    """``--max``: never sleep. Used for the §9.6 capacity benchmark."""

    def offset(self, index: int, event_time: datetime, first: datetime) -> None:
        return None


def pacer_from_args(speedup: float | None, rate: float | None, maximum: bool) -> Pacer:
    chosen = [
        name for name, on in (("--speedup", speedup), ("--rate", rate), ("--max", maximum)) if on
    ]
    if len(chosen) > 1:
        raise ValueError(f"pick one pacing mode, got {chosen}")
    if maximum:
        return MaxPacer()
    if rate is not None:
        return RatePacer(rate)
    return SpeedupPacer(speedup if speedup is not None else _default_speedup())


def _default_speedup() -> float:
    return float(load_yaml("stream")["replayer"]["default_speedup"])


# --- loading (PLAN §9.1) ------------------------------------------------------------


def replay_window() -> tuple[pd.Timestamp, pd.Timestamp]:
    """The replay window, from the one place split dates live (invariant 6).

    ``windows()`` reads `configs/splits.yaml` only. It is not the guarded ``load()``, and
    no labelled data is opened by asking for a pair of dates.
    """
    from fraud.modeling.splits import TEST_SPLIT, windows

    window = windows()[TEST_SPLIT]
    return window.start, window.end


def load_events(
    path: Path | None = None,
    *,
    start_at: datetime | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """The replay window's events, §3.5 columns only, in a total order (§9.1)."""
    settings = Settings.from_env()
    frame = pd.read_parquet(path or settings.raw_dir / "events.parquet")

    leaked = LABEL_FIELDS & set(frame.columns)
    if leaked:
        # events.parquet should not carry labels at all; if it ever does, stop rather
        # than rely on the column selection below to save us.
        raise LabelLeak(f"{path} carries label columns {sorted(leaked)} (PLAN §3.5, §4.6)")

    start, end = replay_window()
    window = frame[(frame["event_time"] >= start) & (frame["event_time"] < end)]
    if start_at is not None:
        window = window[window["event_time"] >= pd.Timestamp(start_at)]

    # Total order: timestamps tie constantly, and txn_id is assigned after the simulator
    # sorts by time, so this reproduces the offline order exactly (§4.2).
    window = window.sort_values(["event_time", "txn_id"], kind="stable")
    ordered = window[list(EVENT_FIELDS)].reset_index(drop=True)
    return ordered.head(limit) if limit is not None else ordered


def assert_ordered(frame: pd.DataFrame) -> None:
    """§9.1 asserts the input is sorted; out-of-order events would break every window."""
    keys = frame[["event_time", "txn_id"]]
    if not keys.equals(keys.sort_values(["event_time", "txn_id"], kind="stable")):
        raise ValueError("replay input is not ordered by (event_time, txn_id) (PLAN §5.1)")


def to_message(row: Any, ingest_ts: int) -> dict[str, str]:
    """One stream message: the §3.5 event plus ingest_ts and schema_version.

    Everything is stringified because Redis stores strings regardless; doing it here
    keeps what the consumer parses identical to what was intended, and
    ``TransactionEvent.from_message`` is the matching half.
    """
    message = {field: str(getattr(row, field)) for field in EVENT_FIELDS}
    message["ingest_ts"] = str(ingest_ts)
    message["schema_version"] = str(SCHEMA_VERSION)

    leaked = LABEL_FIELDS & message.keys()
    if leaked:  # pragma: no cover - unreachable while EVENT_FIELDS is the §3.5 list
        raise LabelLeak(f"message for {message['txn_id']} carries labels {sorted(leaked)}")
    return message


# --- the replayer -------------------------------------------------------------------


@dataclass
class ReplayStats:
    sent: int = 0
    pauses: int = 0
    paused_seconds: float = 0.0
    last_event_time: datetime | None = None


class Replayer:
    """Writes the replay window into ``txn:events`` at a chosen pace (PLAN §9.1)."""

    def __init__(
        self,
        client: Redis,
        pacer: Pacer,
        *,
        config: dict[str, Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        raw = config or load_yaml("stream")
        self._redis = client
        self._pacer = pacer
        self._stream = str(raw["stream"]["name"])
        self._group = str(raw["stream"]["group"])
        self._maxlen = int(raw["stream"]["maxlen"])
        self._batch_size = int(raw["replayer"]["batch_size"])
        back = raw["replayer"]["backpressure"]
        self._check_every = int(back["check_every"])
        self._pause_above = int(back["pause_above"])
        self._resume_below = int(back["resume_below"])
        self._poll_seconds = float(back["poll_seconds"])
        # Injected so tests can drive pacing and pauses without real time passing.
        self._sleep = sleep
        self._monotonic = monotonic
        self.stats = ReplayStats()

    # --- backpressure (PLAN §9.1) ---

    def consumer_lag(self) -> int | None:
        """Entries the ``scorers`` group has not yet read.

        ``lag`` needs Redis >= 7.0 and is None when the group's position cannot be
        determined — after trimming past it, for instance. None means "do not know", and
        the honest response is to keep going rather than pause forever on a guess.
        """
        try:
            groups = self._redis.xinfo_groups(self._stream)
        except Exception:  # noqa: BLE001 - no stream yet is not an error for the producer
            return None
        for group in groups:
            if group.get("name") == self._group:
                lag = group.get("lag")
                return None if lag is None else int(lag)
        return None

    def await_capacity(self) -> None:
        """Pause while the consumer is more than ``pause_above`` behind (§9.1).

        Resumes only once the lag falls under ``resume_below``. The gap between the two
        is what stops this becoming a busy-wait that pauses and resumes forever without
        the consumer ever catching up.
        """
        lag = self.consumer_lag()
        if lag is None or lag <= self._pause_above:
            return

        self.stats.pauses += 1
        started = self._monotonic()
        logger.warning(
            "backpressure: %s lag %s > %s, pausing until it falls below %s",
            self._group,
            f"{lag:,}",
            f"{self._pause_above:,}",
            f"{self._resume_below:,}",
        )
        while lag is not None and lag >= self._resume_below:
            self._sleep(self._poll_seconds)
            lag = self.consumer_lag()

        waited = self._monotonic() - started
        self.stats.paused_seconds += waited
        logger.info("backpressure: resumed after %.1fs, lag %s", waited, lag)

    # --- sending ---

    def send(self, frame: pd.DataFrame) -> ReplayStats:
        """Replay a frame, pacing and pausing as configured."""
        assert_ordered(frame)
        if frame.empty:
            return self.stats

        first = frame["event_time"].iloc[0].to_pydatetime()
        started = self._monotonic()

        for batch in self._batches(frame):
            self._wait_for(batch[0][0], batch[0][1], first, started)
            self._publish(batch)

            if self._check_every and self.stats.sent % self._check_every < len(batch):
                self.await_capacity()

        logger.info(
            "sent %s events, %d pause(s), %.1fs paused, last event %s",
            f"{self.stats.sent:,}",
            self.stats.pauses,
            self.stats.paused_seconds,
            self.stats.last_event_time,
        )
        return self.stats

    def _batches(self, frame: pd.DataFrame) -> Iterator[list[tuple[int, datetime, Any]]]:
        batch: list[tuple[int, datetime, Any]] = []
        for index, row in enumerate(frame.itertuples(index=False)):
            batch.append((index, row.event_time.to_pydatetime(), row))
            if len(batch) >= self._batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _wait_for(self, index: int, event_time: datetime, first: datetime, started: float) -> None:
        offset = self._pacer.offset(index, event_time, first)
        if offset is None:
            return
        delay = offset - (self._monotonic() - started)
        if delay > 0:
            self._sleep(delay)

    def _publish(self, batch: list[tuple[int, datetime, Any]]) -> None:
        """One pipeline per batch (§9.1); ingest_ts is stamped at send time."""
        pipe = self._redis.pipeline(transaction=False)
        for _, _, row in batch:
            ingest_ts = int(time.time() * MILLIS_PER_SECOND)
            pipe.xadd(
                self._stream,
                to_message(row, ingest_ts),
                maxlen=self._maxlen,
                approximate=True,
            )
        pipe.execute()

        self.stats.sent += len(batch)
        self.stats.last_event_time = batch[-1][1]


def main(argv: list[str] | None = None) -> int:
    """``python -m fraud.stream.replayer`` (PLAN §9.1)."""
    parser = argparse.ArgumentParser(description="Replay the test window into Redis (§9.1).")
    pacing = parser.add_mutually_exclusive_group()
    pacing.add_argument("--speedup", type=float, help="compress event time by this factor")
    pacing.add_argument("--rate", type=float, help="fixed events per second")
    pacing.add_argument("--max", action="store_true", help="no sleeping (benchmark)")
    parser.add_argument("--limit", type=int, default=None, help="stop after N events")
    parser.add_argument(
        "--start-at", type=str, default=None, help="skip events before this ISO time"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    frame = load_events(
        start_at=datetime.fromisoformat(args.start_at) if args.start_at else None,
        limit=args.limit,
    )
    logger.info(
        "replaying %s events from %s to %s",
        f"{len(frame):,}",
        frame["event_time"].iloc[0] if len(frame) else "-",
        frame["event_time"].iloc[-1] if len(frame) else "-",
    )

    settings = Settings.from_env()
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    replayer = Replayer(client, pacer_from_args(args.speedup, args.rate, args.max))
    replayer.send(frame)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
