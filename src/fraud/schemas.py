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
