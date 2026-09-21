"""RedisStore: the online state backend with atomic per-event commits (PLAN §5.4).

This is the second implementation of one contract, and the only reason it is allowed to
exist is that it holds state differently, never that it computes anything differently.
Every feature still comes from ``engine.compute_features``; what changes is where the
account blob and the entity fan-out counts are read from. ``test_parity_redis.py``
asserts the two stores agree feature for feature, because the moment they diverge the
offline metrics stop describing the online system.

Three mechanics carry the design.

**Half-open windows survive the translation.** ``InMemoryStore`` counts with a bisect on
``[low, high)``; here the same window is ``ZCOUNT key <low> (<high>`` — inclusive low,
exclusive high. Getting that bracket wrong would let an event count itself, which is the
leak §5.1 rule 2 exists to prevent, and it would show up as a one-row disagreement rather
than an error.

**Timestamps are epoch milliseconds**, following §5.1 rule 1 and ``state.py``. PLAN §3.6
says "epoch s" for these sorted sets, which contradicts §5.3; seconds are wrong here
because two events in the same second would collapse onto one score and the stores would
disagree. A double holds these integers exactly, so ZCOUNT boundaries are not fuzzy.

**Redelivery is handled before the transaction, not inside it.** One scorer consumes the
stream in order, so checking ``feat:{txn}`` and then committing is enough: a duplicate
finds its stored record and returns it untouched. With several consumers racing on the
same message this would need a Lua script; §9.7 says to say that in interviews rather
than build it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from redis import Redis

from fraud.features.engine import StateView
from fraud.features.spec import FEATURE_SPEC_VERSION, FeatureConfig
from fraud.features.state import AccountState, to_millis
from fraud.schemas import TransactionEvent

logger = logging.getLogger(__name__)

MILLIS_PER_SECOND: Final[int] = 1000

STATE_KEY: Final[str] = "state:acct:{}"
FEATURE_KEY: Final[str] = "feat:{}"
DEVICE_KEY: Final[str] = "ent:dev:{}"
IP_KEY: Final[str] = "ent:ip:{}"
MERCHANT_KEY: Final[str] = "ent:mer:{}"
STATE_VERSION_KEY: Final[str] = "meta:state_version"


class StateVersionMismatch(RuntimeError):
    """Redis holds state built for a different feature spec (PLAN §8).

    Not recoverable by retrying: the blobs mean something else now, and the only honest
    fix is a fresh backfill.
    """


class RedisStore:
    """Account blobs, entity sorted sets and committed records, in Redis.

    ``decode_responses=True`` is required. Feature records and account blobs are JSON
    text, and a client handing back bytes would turn every read into a silent
    ``bytes``-vs-``str`` bug at the far end of the pipeline.
    """

    __slots__ = ("_cfg", "_redis", "_since_trim")

    def __init__(self, client: Redis, cfg: FeatureConfig | None = None) -> None:
        if not client.connection_pool.connection_kwargs.get("decode_responses"):
            raise ValueError(
                "RedisStore needs a client built with decode_responses=True: state blobs "
                "and feature records are JSON text (PLAN §6.1)."
            )
        self._redis = client
        self._cfg = cfg or FeatureConfig.load()
        self._since_trim = 0

    @classmethod
    def from_url(cls, url: str, cfg: FeatureConfig | None = None) -> RedisStore:
        return cls(Redis.from_url(url, decode_responses=True), cfg)

    @property
    def client(self) -> Redis:
        return self._redis

    # --- StateStore protocol (PLAN §5.4) ---

    def committed(self, txn_id: str) -> dict[str, Any] | None:
        """The stored record for a transaction, or None (PLAN §5.4 idempotency check).

        A single GET. §5.4 also sketches a load that batches this with the account blob
        and the five ZCOUNTs in one pipeline, but the ``StateStore`` protocol in the same
        section hands ``committed`` only a ``txn_id`` — the entity keys are not knowable
        here. So a scored event costs two round trips: this, then one pipeline of six.
        Collapsing them would mean passing the whole event to ``committed``, a protocol
        change not worth making before Phase 9 has measured whether it matters.
        """
        record = self._redis.get(FEATURE_KEY.format(txn_id))
        return json.loads(record) if record else None

    def load(self, event: TransactionEvent) -> StateView:
        """Account state and entity counts as they stood before this event.

        One pipeline: the account blob plus the five windowed counts (§5.4).
        """
        now = to_millis(event.event_time)
        hour_ago = now - self._cfg.hour_seconds * MILLIS_PER_SECOND
        month_ago = now - self._cfg.month_seconds * MILLIS_PER_SECOND

        device, ip, merchant = self._entity_keys(event)

        pipe = self._redis.pipeline(transaction=False)
        pipe.get(STATE_KEY.format(event.account_id))
        # ZCOUNT with an exclusive upper bound is the half-open window [low, now) from
        # §5.1 rule 2. "(" is Redis's exclusive marker; without it the event's own
        # earlier touch at exactly `now` would be counted.
        pipe.zcount(device, hour_ago, f"({now}")
        pipe.zcount(device, month_ago, f"({now}")
        pipe.zcount(ip, hour_ago, f"({now}")
        pipe.zcount(ip, month_ago, f"({now}")
        pipe.zcount(merchant, month_ago, f"({now}")
        blob, dev_1h, dev_30d, ip_1h, ip_30d, mer_30d = pipe.execute()

        return StateView(
            account=AccountState.from_json(blob) if blob else AccountState(),
            dev_accts_1h=int(dev_1h),
            dev_accts_30d=int(dev_30d),
            ip_accts_1h=int(ip_1h),
            ip_accts_30d=int(ip_30d),
            mer_accts_30d=int(mer_30d),
        )

    def commit(
        self, event: TransactionEvent, account: AccountState, record: dict[str, Any]
    ) -> None:
        """Record, account blob and entity touches, in one MULTI/EXEC (PLAN §5.4).

        All of it or none of it. A partial commit is the failure that matters here: the
        record written without the state update makes a redelivery return a record whose
        state was never applied, and the state updated without the record makes a
        redelivery count the event twice.
        """
        now = to_millis(event.event_time)
        device, ip, merchant = self._entity_keys(event)
        # Raises on a non-JSON value rather than mangling it: `extra` carries things like
        # graph_snapshot_ts, and a datetime silently stringified here would come back a
        # different type on redelivery and break the re-score check (§9.6).
        blob = json.dumps(record, sort_keys=True, separators=(",", ":"))

        pipe = self._redis.pipeline(transaction=True)
        pipe.set(FEATURE_KEY.format(event.txn_id), blob, ex=self._cfg.redis_feature_ttl_seconds)
        pipe.set(STATE_KEY.format(event.account_id), account.to_json())
        for key in (device, ip, merchant):
            # GT so a late arrival never drags a last-seen timestamp backwards, which is
            # exactly what _EntityIndex.touch does in the memory store.
            pipe.zadd(key, {event.account_id: now}, gt=True)

        self._since_trim += 1
        if self._since_trim >= self._cfg.redis_trim_every:
            self._since_trim = 0
            self._trim(pipe, (device, ip, merchant), now)

        pipe.execute()

    # --- housekeeping ---

    def _trim(self, pipe: Any, keys: tuple[str, ...], now: int) -> None:
        """Drop entries that no window can reach again (PLAN §5.3).

        The cutoff is the start of the longest window, and the upper bound is exclusive,
        so an entry sitting exactly on the boundary survives. Event time only moves
        forward, so anything dropped here is already unreachable for every later event —
        trimming can never change a count.
        """
        cutoff = now - self._cfg.redis_entity_retention_seconds * MILLIS_PER_SECOND
        for key in keys:
            pipe.zremrangebyscore(key, "-inf", f"({cutoff}")

    def _entity_keys(self, event: TransactionEvent) -> tuple[str, str, str]:
        return (
            DEVICE_KEY.format(event.device_id),
            IP_KEY.format(event.ip),
            MERCHANT_KEY.format(event.merchant_id),
        )

    # --- state version (PLAN §8 loading rules, §9.2) ---

    def state_version(self) -> str | None:
        return self._redis.get(STATE_VERSION_KEY)

    def set_state_version(self, version: str = FEATURE_SPEC_VERSION) -> None:
        self._redis.set(STATE_VERSION_KEY, version)

    def check_state_version(self, expected: str = FEATURE_SPEC_VERSION) -> None:
        """Refuse to run against state built for a different spec (PLAN §8).

        An empty Redis is fine — that is a first run, and the marker is claimed. A
        *different* marker is not, because the blobs no longer mean what the code thinks
        and every feature computed from them would be quietly wrong.
        """
        found = self.state_version()
        if found is None:
            self.set_state_version(expected)
            logger.info("claimed %s = %s", STATE_VERSION_KEY, expected)
            return
        if found != expected:
            raise StateVersionMismatch(
                f"Redis holds state for feature spec {found!r}, this code is {expected!r}. "
                "Run a fresh backfill (PLAN §8, §9.2); the existing blobs cannot be reused."
            )
