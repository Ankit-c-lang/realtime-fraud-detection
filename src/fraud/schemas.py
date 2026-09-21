"""TransactionEvent and the shared API models (PLAN §3.5, §10)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

SCHEMA_VERSION: Final[int] = 1

# The stream message, minus ingest_ts and schema_version, which the replayer adds (§4.2).
EVENT_FIELDS: Final[tuple[str, ...]] = (
    "txn_id",
    "event_time",
    "account_id",
    "merchant_id",
    "merchant_category",
    "amount",
    "channel",
    "device_id",
    "ip",
    "lat",
    "lon",
    "city",
    "country",
    "status",
)


@dataclass(frozen=True, slots=True)
class TransactionEvent:
    """One transaction, exactly as PLAN §3.5 defines it.

    A plain dataclass rather than a Pydantic model: the offline replay builds one of
    these per event for the whole dataset, and validation there would cost more than it
    buys because the data comes from our own generator. The API validates its input with
    Pydantic before constructing one (§10).

    ``event_time`` is naive IST, matching what the simulator writes (§3.5, §5.1 rule 7).
    """

    txn_id: str
    event_time: datetime
    account_id: str
    merchant_id: str
    merchant_category: str
    amount: float
    channel: str
    device_id: str
    ip: str
    lat: float
    lon: float
    city: str
    country: str
    status: str

    @property
    def approved(self) -> bool:
        return self.status == "APPROVED"

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> TransactionEvent:
        return cls(**{field: row[field] for field in EVENT_FIELDS})

    @classmethod
    def from_message(cls, fields: dict[str, Any]) -> TransactionEvent:
        """Rebuild an event from a Redis Stream message (PLAN §3.5).

        Redis stores every field as a string, so somebody has to cast them back. Doing it
        here rather than in the scorer means the producer and the consumer cannot disagree
        about what ``amount`` or ``event_time`` mean — a mismatch that would not raise,
        it would just quietly produce different features online than offline.

        ``ingest_ts`` and ``schema_version`` are transport metadata, not part of the
        event, so they are ignored here and read separately by whoever needs them.
        """
        raw = {field: fields[field] for field in EVENT_FIELDS}
        event_time = raw["event_time"]
        return cls(
            **{
                **raw,
                "event_time": (
                    event_time
                    if isinstance(event_time, datetime)
                    else datetime.fromisoformat(str(event_time))
                ),
                "amount": float(raw["amount"]),
                "lat": float(raw["lat"]),
                "lon": float(raw["lon"]),
            }
        )


# --- API models (PLAN §10) ----------------------------------------------------------
#
# These are Pydantic; `TransactionEvent` above is not, deliberately. §10 asks for
# `extra="forbid"` on the event, and that is enforced here, at the boundary, which is
# where §10 itself says leakage rule L4 belongs. Converting the dataclass instead would
# put validation inside a loop that runs 494,156 times during the offline replay — a
# path already verified bit-identical against the live system — for no gain on data the
# generator produced.

from ipaddress import IPv4Address
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DECISIONS: Final[tuple[str, ...]] = ("ALLOW", "REVIEW", "HOLD")


class EventRequest(BaseModel):
    """A transaction submitted to ``POST /score`` (PLAN §3.5, §10).

    ``extra="forbid"`` is the point: a body carrying ``is_fraud`` is rejected with 422
    rather than quietly ignored. That is leakage rule L4 enforced where it can actually
    be enforced — at the edge — instead of trusted further in.
    """

    model_config = ConfigDict(extra="forbid")

    txn_id: str = Field(min_length=1, max_length=64)
    event_time: datetime
    account_id: str = Field(min_length=1, max_length=64)
    merchant_id: str = Field(min_length=1, max_length=64)
    merchant_category: str
    amount: float = Field(gt=0)
    channel: Literal["POS", "ONLINE"]
    device_id: str = Field(min_length=1, max_length=64)
    ip: IPv4Address
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    city: str = Field(min_length=1, max_length=64)
    country: str = Field(min_length=2, max_length=2)
    status: Literal["APPROVED", "DECLINED"]
    schema_version: Literal[1] = SCHEMA_VERSION

    @field_validator("merchant_category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        """Categories come from configs/categories.yaml, which also fixes their codes.

        An unknown category would become a null in the categorical column and the model
        would score it against a level it never saw, so it is refused rather than
        silently degraded (leakage rule L7).
        """
        from fraud.features.spec import category_levels

        levels = category_levels()
        if value not in levels:
            raise ValueError(f"unknown merchant_category {value!r}; expected one of {levels}")
        return value

    @field_validator("event_time")
    @classmethod
    def _naive(cls, value: datetime) -> datetime:
        """Naive IST, as the simulator writes (§3.5).

        A tz-aware value would silently shift every window by the offset, so it is
        rejected rather than converted — converting would guess at an intent nobody
        stated.
        """
        if value.tzinfo is not None:
            raise ValueError("event_time must be naive IST with no offset (PLAN §3.5)")
        return value

    def to_event(self) -> TransactionEvent:
        return TransactionEvent(
            **{
                **self.model_dump(exclude={"schema_version"}),
                "ip": str(self.ip),
            }
        )


class Reason(BaseModel):
    """One explanation line (PLAN §7.10)."""

    feature: str
    value: float | str
    contribution: float
    text: str


class ScoreResponse(BaseModel):
    """PLAN §10, verbatim.

    ``state_updated`` is pinned to False in the type, not merely documented: it is a
    promise the endpoint makes, and a literal cannot drift from the behaviour the way a
    docstring can.
    """

    txn_id: str
    fraud_probability: float
    anomaly_percentile: float
    risk_score: float
    decision: Literal["ALLOW", "REVIEW", "HOLD"]
    reasons: list[Reason]
    model_version: str
    graph_snapshot_ts: datetime | None
    already_processed: bool
    state_updated: Literal[False] = False


class Alert(BaseModel):
    """One entry of ``alerts:recent`` (PLAN §3.6, §11)."""

    txn_id: str
    account_id: str
    merchant_id: str
    amount: float
    event_time: str
    risk: float
    decision: str
    reason: str = ""


class HealthResponse(BaseModel):
    """Liveness plus the readiness details §10 asks for."""

    status: Literal["ok", "degraded"]
    redis: bool
    model_version: str | None
    feature_spec_version: str | None
    state_version: str | None
    latest_snapshot: datetime | None
    scorer_heartbeat_age_seconds: float | None


class MetricsResponse(BaseModel):
    """What §10's /metrics reports."""

    events: int
    alerts: int
    dead_lettered: int
    flushes: int
    stream_length: int
    consumer_lag: int | None
    pending: int
    dlq_size: int
    throughput_per_second: float
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    watermark: datetime | None
