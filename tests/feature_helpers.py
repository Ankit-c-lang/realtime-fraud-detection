"""Builders for hand-written event sequences (PLAN §13, `make_events`).

Kept out of conftest.py on purpose: these import the feature engine, which does not exist
until P2.2, and a failed import in conftest would break collection for the whole suite
instead of just the feature tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fraud.features.accounts import AccountDirectory
from fraud.features.engine import FeatureEngine
from fraud.features.store_memory import InMemoryStore
from fraud.schemas import TransactionEvent

# Two real cities from configs/cities.csv, far enough apart to be unambiguous.
MUMBAI = (19.0760, 72.8777)
DELHI = (28.6139, 77.2090)

ACCOUNT = "A0000001"
HOME_CITY = "Mumbai"
# Naive on purpose: PLAN §3.5 specifies IST timestamps with no offset, and every window
# in §5.1 is event time, so a tzinfo here would misrepresent the data the engine sees.
CREATED_AT = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001
DAY = datetime(2026, 3, 1)  # noqa: DTZ001


def at(hour: int, minute: int = 0, second: int = 0, *, day: datetime = DAY) -> datetime:
    """A timestamp on the reference day, so examples read as wall-clock times."""
    return day.replace(hour=hour, minute=minute, second=second)


def make_event(
    txn_id: str,
    event_time: datetime,
    *,
    account_id: str = ACCOUNT,
    merchant_id: str = "M000001",
    merchant_category: str = "grocery",
    amount: float = 100.0,
    channel: str = "POS",
    device_id: str = "D0000001",
    ip: str = "49.1.1.1",
    location: tuple[float, float] = MUMBAI,
    city: str = HOME_CITY,
    country: str = "IN",
    status: str = "APPROVED",
) -> TransactionEvent:
    return TransactionEvent(
        txn_id=txn_id,
        event_time=event_time,
        account_id=account_id,
        merchant_id=merchant_id,
        merchant_category=merchant_category,
        amount=amount,
        channel=channel,
        device_id=device_id,
        ip=ip,
        lat=location[0],
        lon=location[1],
        city=city,
        country=country,
        status=status,
    )


def make_events(specs: list[dict[str, Any]]) -> list[TransactionEvent]:
    """Build a sequence, numbering the transactions in the order given."""
    return [make_event(f"T{index:07d}", **spec) for index, spec in enumerate(specs)]


def directory(
    accounts: dict[str, tuple[str, tuple[float, float], datetime]] | None = None,
) -> AccountDirectory:
    """Account reference data: home city, home location, creation time."""
    rows = accounts or {ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT)}
    return AccountDirectory.from_rows(
        [
            {
                "account_id": account_id,
                "home_city": city,
                "home_lat": location[0],
                "home_lon": location[1],
                "created_at": created,
            }
            for account_id, (city, location, created) in rows.items()
        ]
    )


def feature_rows(
    events: list[TransactionEvent],
    *,
    accounts: AccountDirectory | None = None,
) -> list[dict[str, Any]]:
    """Process a sequence in order and return one feature dict per event."""
    engine = FeatureEngine(InMemoryStore(), accounts or directory())
    return [engine.process(event) for event in events]


def last_row(events: list[TransactionEvent], **kwargs: Any) -> dict[str, Any]:
    """Features of the final event, which is what most hand-worked examples check."""
    return feature_rows(events, **kwargs)[-1]


def minutes(count: float) -> timedelta:
    return timedelta(minutes=count)
