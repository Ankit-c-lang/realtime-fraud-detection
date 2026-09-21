"""Pending-message drain, reclaim and the dead-letter queue (PLAN §9.3).

Redis Streams hand a message to a consumer and keep it in a pending list until that
consumer acknowledges it. So every message a crashed scorer was holding is still there,
owned by a name nobody is using. Nothing redelivers it on its own — without the code in
this module those events are simply never scored, and the only symptom is a number in a
report being slightly too small.

Three mechanisms, each answering a different failure:

**Drain at startup.** A restarted consumer reading ``>`` gets only *new* messages; its
own pending entries are reachable only by reading from id ``0``. Skipping that step
strands exactly the messages that were in flight when it died.

**Reclaim on idle.** A consumer that never comes back — a container replaced, a host
lost — leaves its pending entries owned forever. ``XPENDING`` finds anything idle beyond
a threshold and ``XCLAIM`` moves it to a live consumer.

**Dead-letter on repeat.** A message that has now been delivered more than
``max_deliveries`` times is not unlucky, it is poison: it has crashed every consumer that
touched it, and a fourth attempt only stalls the stream behind it. It goes to ``txn:dlq``
with its error and is acknowledged, because an unacked poison message is an infinite loop
with extra steps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from redis import Redis

logger = logging.getLogger(__name__)

# XREADGROUP from this id returns the consumer's own pending entries rather than new
# messages. ">" means "never delivered to anyone"; "0" means "mine, still unacked".
OWN_PENDING: Final[str] = "0"
NEW_MESSAGES: Final[str] = ">"

Message = tuple[str, dict[str, str]]


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """One unacknowledged message, as ``XPENDING``'s extended form reports it."""

    message_id: str
    consumer: str
    idle_ms: int
    delivery_count: int

    @classmethod
    def from_redis(cls, raw: dict[str, Any]) -> PendingEntry:
        return cls(
            message_id=str(raw["message_id"]),
            consumer=str(raw.get("consumer", "")),
            idle_ms=int(raw.get("time_since_delivered", 0)),
            delivery_count=int(raw.get("times_delivered", 0)),
        )


class DeadLetterQueue:
    """``txn:dlq``: messages this scorer will not try again (PLAN §9.3)."""

    __slots__ = ("_key", "_maxlen", "_redis")

    def __init__(self, client: Redis, key: str, maxlen: int) -> None:
        self._redis = client
        self._key = key
        self._maxlen = maxlen

    @property
    def key(self) -> str:
        return self._key

    def send(self, message_id: str, fields: dict[str, str], error: str) -> None:
        """Record the message and why it was given up on.

        The original fields travel with it. A DLQ entry that says only "failed" cannot be
        diagnosed later, and the whole point of the queue is that someone can look.
        """
        payload = {
            **{key: str(value) for key, value in fields.items()},
            "dlq_reason": error[:500],
            "dlq_source_id": message_id,
        }
        self._redis.xadd(self._key, payload, maxlen=self._maxlen, approximate=True)
        logger.warning("dead-lettered %s: %s", message_id, error)


def ensure_group(client: Redis, stream: str, group: str) -> bool:
    """Create the consumer group at id 0, tolerating "already exists" (PLAN §9.5).

    ``MKSTREAM`` so the group can exist before the replayer has sent anything, and id
    ``0`` so a group created after the first events still sees them. Created at ``$``
    instead, it would silently skip everything already in the stream.
    """
    try:
        client.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception as error:
        if "BUSYGROUP" in str(error):
            return False
        raise
    logger.info("created consumer group %s on %s at id 0", group, stream)
    return True


def drain_own_pending(
    client: Redis, stream: str, group: str, consumer: str, count: int
) -> list[Message]:
    """Every message this consumer still owns, oldest first (PLAN §9.2 startup).

    These are the messages the previous incarnation of this consumer was holding when it
    died; a normal ``>`` read never returns them. The list is paged on the last id seen,
    for the reason spelled out below.
    """
    drained: list[Message] = []
    cursor = OWN_PENDING
    while True:
        batch = client.xreadgroup(group, consumer, {stream: cursor}, count=count)
        messages = _flatten(batch)
        if not messages:
            break
        drained.extend(messages)
        # The cursor MUST advance. Reading with id "0" returns the consumer's pending
        # entries from the beginning every time — it is not a queue that drains as it is
        # read. Looping on "0" re-reads the same page forever, reporting a wildly
        # inflated count and handing the scorer the same messages again and again.
        # Redis returns pending entries with an ID strictly greater than the one given,
        # so paging on the last id seen is what actually walks the list.
        cursor = messages[-1][0]
        if len(messages) < count:
            break

    if drained:
        logger.info("drained %d pending message(s) owned by %s", len(drained), consumer)
    return drained


def pending_entries(
    client: Redis, stream: str, group: str, *, idle_ms: int, count: int
) -> list[PendingEntry]:
    """Messages unacknowledged for longer than ``idle_ms`` (PLAN §9.2 reclaim)."""
    try:
        raw = client.xpending_range(stream, group, min="-", max="+", count=count, idle=idle_ms)
    except Exception:  # noqa: BLE001 - no stream or no group yet is not an error
        return []
    return [PendingEntry.from_redis(entry) for entry in raw]


def reclaim(
    client: Redis,
    stream: str,
    group: str,
    consumer: str,
    *,
    idle_ms: int,
    count: int,
    max_deliveries: int,
    dlq: DeadLetterQueue,
) -> list[Message]:
    """Take over abandoned messages; dead-letter the ones that keep killing consumers.

    Returns the messages now owned by ``consumer``, to be processed like any other.
    """
    entries = pending_entries(client, stream, group, idle_ms=idle_ms, count=count)
    if not entries:
        return []

    poisoned = [entry for entry in entries if entry.delivery_count > max_deliveries]
    retryable = [entry for entry in entries if entry.delivery_count <= max_deliveries]

    for entry in poisoned:
        _dead_letter(client, stream, group, entry, dlq)

    if not retryable:
        return []

    claimed = client.xclaim(
        stream,
        group,
        consumer,
        min_idle_time=idle_ms,
        message_ids=[entry.message_id for entry in retryable],
    )
    messages = [(str(mid), fields) for mid, fields in claimed if fields]
    if messages:
        logger.info("reclaimed %d message(s) to %s", len(messages), consumer)
    return messages


def _dead_letter(
    client: Redis, stream: str, group: str, entry: PendingEntry, dlq: DeadLetterQueue
) -> None:
    """Move one poison message out of the way, then acknowledge it.

    Acknowledging is what stops the loop. The message is already recorded in the DLQ, so
    the information is not lost — only the retry is.
    """
    found = client.xrange(stream, min=entry.message_id, max=entry.message_id, count=1)
    fields = found[0][1] if found else {}
    dlq.send(
        entry.message_id,
        fields,
        f"delivered {entry.delivery_count} times (limit {entry.delivery_count - 1}); "
        "poison message, not retried",
    )
    client.xack(stream, group, entry.message_id)


def _flatten(batch: Any) -> list[Message]:
    """XREADGROUP returns [(stream, [(id, fields), ...]), ...]; we read one stream."""
    if not batch:
        return []
    messages: list[Message] = []
    for _stream, entries in batch:
        messages.extend((str(mid), fields) for mid, fields in entries)
    return messages
