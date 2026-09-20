"""The feature engine: pure logic, no I/O (PLAN §5.1, §5.4).

One implementation serves both the offline replay and the live scorer. The stores differ;
this does not. Two implementations would quietly disagree, which is the failure the
parity test in §5.4 exists to prevent.

Three rules govern everything here (§5.1):

1. **Event time only.** Never ``time.time()``. A window is measured against the event's
   own timestamp, so a replay produces the same answer as a live run did.
2. **Half-open windows** ``[t - w, t)``. An event never counts itself; its own values
   reach the model only through the stateless context features.
3. **Compute, then update.** Features come from the state as it was BEFORE the event.
   Updating first would leak the event into its own features.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from fraud.features.accounts import AccountDirectory
from fraud.features.spec import FeatureConfig
from fraud.features.state import AccountState, to_millis
from fraud.schemas import TransactionEvent

EARTH_RADIUS_KM = 6371.0
MILLIS_PER_SECOND = 1000
SECONDS_PER_HOUR = 3600.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, lambda1, phi2, lambda2 = (math.radians(value) for value in (lat1, lon1, lat2, lon2))
    inner = (
        math.sin((phi2 - phi1) / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin((lambda2 - lambda1) / 2.0) ** 2
    )
    return EARTH_RADIUS_KM * 2.0 * math.asin(math.sqrt(min(max(inner, 0.0), 1.0)))


@dataclass(slots=True)
class StateView:
    """The account and its entity fan-out counts, as they stood before this event."""

    account: AccountState
    dev_accts_1h: int = 0
    dev_accts_30d: int = 0
    ip_accts_1h: int = 0
    ip_accts_30d: int = 0
    mer_accts_30d: int = 0
    seeded: bool = field(default=False)


class StateStore(Protocol):
    """PLAN §5.4. Both stores implement this; the engine talks only to it."""

    def committed(self, txn_id: str) -> dict[str, Any] | None: ...

    def load(self, event: TransactionEvent) -> StateView: ...

    def commit(
        self, event: TransactionEvent, account: AccountState, record: dict[str, Any]
    ) -> None: ...


def compute_features(
    event: TransactionEvent,
    view: StateView,
    accounts: AccountDirectory,
    cfg: FeatureConfig,
) -> dict[str, Any]:
    """The 30 hot features for one event. Pure: no I/O, no clock, no mutation."""
    now = to_millis(event.event_time)
    state = view.account
    facts = accounts.get(event.account_id)

    features: dict[str, Any] = {}
    features.update(_context_features(event, cfg))
    features.update(_velocity_features(state, now, cfg))
    features.update(_deviation_features(event, state, now, facts, cfg))
    features.update(_novelty_features(event, state, now, facts, cfg))
    features.update(
        {
            "dev_accts_1h": view.dev_accts_1h,
            "dev_accts_30d": view.dev_accts_30d,
            "ip_accts_1h": view.ip_accts_1h,
            "ip_accts_30d": view.ip_accts_30d,
            "mer_accts_30d": view.mer_accts_30d,
        }
    )
    return features


def _context_features(event: TransactionEvent, cfg: FeatureConfig) -> dict[str, Any]:
    """A. Stateless: derived from the event alone (PLAN §5.2 #1-#6)."""
    hour = event.event_time.hour
    return {
        "log_amount": math.log1p(event.amount),
        "hour_of_day": hour,
        "is_night": int(cfg.night_start <= hour < cfg.night_end),
        "is_online": int(event.channel == "ONLINE"),
        "is_international": int(event.country != "IN"),
        "merchant_category": event.merchant_category,
    }


def _velocity_features(state: AccountState, now: int, cfg: FeatureConfig) -> dict[str, Any]:
    """B. Counts over half-open windows ending at, but excluding, this event."""
    short = now - cfg.short_seconds * MILLIS_PER_SECOND
    hour = now - cfg.hour_seconds * MILLIS_PER_SECOND
    day = now - cfg.day_seconds * MILLIS_PER_SECOND

    count_5m = count_1h = count_24h = declines_1h = small_1h = 0
    amount_24h = 0.0
    merchants_1h: set[str] = set()

    for ts, amount, merchant_id, declined in state.recent:
        if not (day <= ts < now):
            continue
        count_24h += 1
        amount_24h += amount
        if hour <= ts:
            count_1h += 1
            merchants_1h.add(merchant_id)
            declines_1h += declined
            small_1h += int(amount < cfg.small_amount)
        if short <= ts:
            count_5m += 1

    return {
        "acct_cnt_5m": count_5m,
        "acct_cnt_1h": count_1h,
        "acct_cnt_24h": count_24h,
        "acct_amt_24h": amount_24h,
        "acct_merchants_1h": len(merchants_1h),
        "acct_declines_1h": declines_1h,
        "acct_small_1h": small_1h,
    }


def _deviation_features(
    event: TransactionEvent,
    state: AccountState,
    now: int,
    facts: Any,
    cfg: FeatureConfig,
) -> dict[str, Any]:
    """C. How far this event sits from the account's own habits (PLAN §5.2 #14-#19)."""
    if state.last_ts is None:
        since_last = float(cfg.secs_since_last_default)
    else:
        since_last = min(
            (now - state.last_ts) / MILLIS_PER_SECOND, float(cfg.secs_since_last_default)
        )

    mean = state.mean_amount()
    zscore = 0.0
    to_mean = cfg.amount_to_mean_default
    if mean is not None and mean > 0:
        to_mean = min(event.amount / mean, cfg.amount_to_mean_clip)
    if state.n_ok >= cfg.zscore_min_prior and mean is not None:
        # The floor stops a customer with identical spending from scoring infinity.
        denominator = max(
            state.std_amount(), cfg.zscore_std_floor_ratio * mean, cfg.zscore_std_floor_abs
        )
        zscore = min(
            max((event.amount - mean) / denominator, cfg.zscore_clip_low), cfg.zscore_clip_high
        )

    bucket = event.event_time.hour // cfg.hour_bucket_hours
    prior_in_bucket = state.hour_buckets[bucket] if bucket < len(state.hour_buckets) else 0
    # Smoothed, so a brand-new account is not automatically "unusual".
    unusualness = 1.0 - (prior_in_bucket + 1) / (state.n_all + cfg.hour_bucket_count)

    age_days = 0.0
    if facts is not None:
        age_days = (event.event_time - facts.created_at).total_seconds() / 86400.0

    return {
        "secs_since_last": since_last,
        "amount_zscore": zscore,
        "amount_to_mean": to_mean,
        "hour_unusualness": unusualness,
        "account_age_days": age_days,
        "acct_history_cnt": state.n_all,
    }


def _novelty_features(
    event: TransactionEvent,
    state: AccountState,
    now: int,
    facts: Any,
    cfg: FeatureConfig,
) -> dict[str, Any]:
    """D. First sightings and movement (PLAN §5.2 #20-#25)."""
    month_ago = now - cfg.month_seconds * MILLIS_PER_SECOND
    devices_30d = sum(1 for last in state.devices.values() if month_ago <= last < now)

    # The home city counts as seen from account creation, or every account's first
    # transaction would look like it had travelled.
    known_cities = state.cities if state.cities else set()
    home_city = facts.home_city if facts is not None else None
    seen_city = event.city in known_cities or event.city == home_city

    km_from_home = 0.0
    if facts is not None:
        km_from_home = haversine_km(facts.home_lat, facts.home_lon, event.lat, event.lon)

    speed = cfg.geo_speed_default
    if state.last_ts is not None and state.last_lat is not None and state.last_lon is not None:
        distance = haversine_km(state.last_lat, state.last_lon, event.lat, event.lon)
        elapsed = max((now - state.last_ts) / MILLIS_PER_SECOND, float(cfg.min_elapsed_seconds))
        speed = min(distance / (elapsed / SECONDS_PER_HOUR), cfg.speed_cap_kmh)

    return {
        "new_device": int(event.device_id not in state.devices),
        "acct_devices_30d": devices_30d,
        "new_merchant": int(event.merchant_id not in state.merchants),
        "new_city": int(not seen_city),
        "km_from_home": km_from_home,
        "geo_speed_kmh": speed,
    }


def update_account(
    state: AccountState, event: TransactionEvent, cfg: FeatureConfig, facts: Any = None
) -> AccountState:
    """Fold one event into the account state. Called AFTER features are computed."""
    now = to_millis(event.event_time)
    day_ago = now - cfg.day_seconds * MILLIS_PER_SECOND
    device_cutoff = now - cfg.device_retention_days * 86400 * MILLIS_PER_SECOND

    recent = [entry for entry in state.recent if entry[0] >= day_ago]
    recent.append((now, event.amount, event.merchant_id, int(not event.approved)))

    devices = {device: last for device, last in state.devices.items() if last >= device_cutoff}
    devices[event.device_id] = max(devices.get(event.device_id, now), now)

    bucket = event.event_time.hour // cfg.hour_bucket_hours
    buckets = list(state.hour_buckets)
    if bucket < len(buckets):
        buckets[bucket] += 1

    cities = set(state.cities)
    if not cities and facts is not None:
        cities.add(facts.home_city)
    cities.add(event.city)

    return AccountState(
        recent=recent,
        last_ts=now,
        last_lat=event.lat,
        last_lon=event.lon,
        n_all=state.n_all + 1,
        n_ok=state.n_ok + int(event.approved),
        sum_ok=state.sum_ok + (event.amount if event.approved else 0.0),
        sumsq_ok=state.sumsq_ok + (event.amount**2 if event.approved else 0.0),
        hour_buckets=buckets,
        devices=devices,
        merchants=set(state.merchants) | {event.merchant_id},
        cities=cities,
    )


class FeatureEngine:
    """Ties a store to the pure logic (PLAN §5.4)."""

    __slots__ = ("accounts", "cfg", "store")

    def __init__(
        self,
        store: StateStore,
        accounts: AccountDirectory,
        cfg: FeatureConfig | None = None,
    ) -> None:
        self.store = store
        self.accounts = accounts
        self.cfg = cfg or FeatureConfig.load()

    def process(
        self,
        event: TransactionEvent,
        *,
        commit: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Features for one event, committing the resulting state unless asked not to.

        A redelivered event returns the record stored the first time and changes nothing.
        That is what makes at-least-once delivery safe: without it a duplicate would
        double-count velocity and corrupt the running mean (PLAN §9.3).
        """
        if commit and (done := self.store.committed(event.txn_id)) is not None:
            return done

        view = self.store.load(event)
        features = compute_features(event, view, self.accounts, self.cfg)
        if not commit:
            return features

        record = {**features, **(extra or {})}
        facts = self.accounts.get(event.account_id)
        self.store.commit(event, update_account(view.account, event, self.cfg, facts), record)
        return record
