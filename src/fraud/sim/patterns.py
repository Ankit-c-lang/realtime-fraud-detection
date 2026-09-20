"""The four injected fraud patterns and their labels (PLAN §4.5).

Each injector returns events tagged with ``fraud_type``, ``attack_id`` and ``ring_id``.
``generate.py`` turns those into ``labels.parquet`` once ``txn_id`` exists; nothing here
writes a label into the event stream itself (invariant 4).

Three of the patterns operate on existing victims and leave loud per-event traces:
velocity abuse, account takeover and card testing. The fourth does not. A ring's
transactions are deliberately ordinary in amount, hour and approval rate, and the only
thing that gives it away is which accounts share a device or an IP, which is what the
graph layer in PLAN §6 exists to find.

Ring mule accounts are created here rather than in ``population.py``: their ages are
defined relative to each ring's own start date (§4.5), which the population cannot know.

*Ring placement.* §4.5 requires at least 20 rings starting and finishing inside the
training window, at least 12 starting inside the test window, and at least 4 of those
reusing a device from a recent ring. Left to chance those quotas fail, and the ring half
of the evaluation would then be measuring nothing, so placement is assigned deliberately
and `checks.py` re-verifies the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pandas as pd

from fraud.config import load_yaml
from fraud.sim.legit import EVENT_COLUMNS, time_of_day_profile
from fraud.sim.population import Population

LABEL_COLUMNS: Final[tuple[str, ...]] = ("fraud_type", "attack_id", "ring_id")
ATTACK_COLUMNS: Final[tuple[str, ...]] = EVENT_COLUMNS + LABEL_COLUMNS

FRAUD_TYPES: Final[tuple[str, ...]] = ("VELOCITY", "ATO", "CARD_TESTING", "RING")

_MIN_AMOUNT: Final[float] = 1.0
_BINS_PER_DAY: Final[int] = 96


@dataclass(frozen=True, slots=True)
class Attacks:
    """Fraud events plus every actor the patterns had to invent."""

    events: pd.DataFrame
    accounts: pd.DataFrame
    devices: pd.DataFrame
    account_devices: pd.DataFrame
    ips: pd.DataFrame
    account_ips: pd.DataFrame


class _Registry:
    """Hands out the attacker devices, IPs and mule accounts the patterns create."""

    def __init__(self, population: Population, rng: np.random.Generator) -> None:
        self._rng = rng
        self._device_seq = len(population.devices)
        self._account_seq = len(population.accounts)
        self._taken = set(population.ips["ip"])
        self.devices: list[dict[str, Any]] = []
        self.ips: list[dict[str, Any]] = []
        self.accounts: list[dict[str, Any]] = []
        self.account_devices: list[dict[str, Any]] = []
        self.account_ips: list[dict[str, Any]] = []

    def device(self, kind: str = "attacker") -> str:
        device_id = f"D{self._device_seq:07d}"
        self._device_seq += 1
        self.devices.append({"device_id": device_id, "device_type": kind})
        return device_id

    def account(self, **fields: Any) -> str:
        account_id = f"A{self._account_seq:07d}"
        self._account_seq += 1
        self.accounts.append({"account_id": account_id, **fields})
        return account_id

    def ip(self, kind: str, city: pd.Series) -> str:
        first = 45 if kind == "vpn" else 49
        while True:
            address = ".".join(
                str(x)
                for x in (
                    first,
                    self._rng.integers(0, 256),
                    self._rng.integers(0, 256),
                    self._rng.integers(1, 255),
                )
            )
            if address not in self._taken:
                break
        self._taken.add(address)
        self.ips.append(
            {
                "ip": address,
                "ip_type": kind,
                "city": city["city"],
                "country": city["country"],
                "lat": float(city["lat"]),
                "lon": float(city["lon"]),
            }
        )
        return address


def inject_patterns(
    config: dict[str, Any],
    categories: list[dict[str, Any]],
    population: Population,
    legit: pd.DataFrame,
    rngs: dict[str, np.random.Generator],
) -> Attacks:
    """Run all four injectors over the legitimate stream (PLAN §4.5)."""
    context = _Context(config, categories, population, legit, rngs)
    registry = _Registry(population, rngs["pattern_ring"])

    frames = [
        _inject_velocity(context, registry, rngs["pattern_velocity"]),
        _inject_ato(context, registry, rngs["pattern_ato"]),
        _inject_card_testing(context, registry, rngs["pattern_card_testing"]),
        _inject_rings(context, registry, rngs["pattern_ring"]),
    ]
    events = pd.concat([f for f in frames if len(f)], ignore_index=True)
    events = events[list(ATTACK_COLUMNS)].sort_values(
        "event_time", kind="stable", ignore_index=True
    )

    return Attacks(
        events=events,
        accounts=_frame(registry.accounts, population.accounts.columns),
        devices=_frame(registry.devices, population.devices.columns),
        account_devices=_frame(registry.account_devices, population.account_devices.columns),
        ips=_frame(registry.ips, population.ips.columns),
        account_ips=_frame(registry.account_ips, population.account_ips.columns),
    )


def _frame(rows: list[dict[str, Any]], columns: pd.Index) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(columns)) if rows else pd.DataFrame(columns=columns)


class _Context:
    """Everything the injectors need to look up, prepared once."""

    def __init__(
        self,
        config: dict[str, Any],
        categories: list[dict[str, Any]],
        population: Population,
        legit: pd.DataFrame,
        rngs: dict[str, np.random.Generator],
    ) -> None:
        self.config = config
        self.population = population
        self.legit = legit
        self.rngs = rngs
        self.by_category = {c["name"]: c for c in categories}

        self.start = pd.Timestamp(config["start"])
        self.end = self.start + pd.Timedelta(days=int(config["days"]))
        self.merchants = population.merchants
        self.accounts = population.accounts.set_index("account_id")

        splits = load_yaml("splits")["splits"]
        self.train = (pd.Timestamp(splits["train"]["start"]), pd.Timestamp(splits["train"]["end"]))
        self.test = (pd.Timestamp(splits["test"]["start"]), pd.Timestamp(splits["test"]["end"]))

        self.created_at = population.accounts["created_at"]
        counts = legit.groupby("account_id").size()
        self.activity = counts
        self.mean_amount = legit.groupby("account_id")["amount"].mean()

        cities = population.ips[["city", "country", "lat", "lon"]].drop_duplicates("city")
        self.cities = cities.set_index("city", drop=False)
        self.domestic = cities[cities["country"] == "IN"].reset_index(drop=True)
        self.foreign_cities = _foreign_cities()

    def merchants_in(self, names: list[str], online_only: bool = True) -> np.ndarray:
        mask = self.merchants["category"].isin(names).to_numpy().copy()
        if online_only:
            mask &= self.merchants["is_online"].to_numpy()
        found = np.flatnonzero(mask)
        return found if len(found) else np.flatnonzero(self.merchants["category"].isin(names))


def _haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: float, lon2: float) -> np.ndarray:
    lat1, lon1 = np.radians(lat1), np.radians(lon1)
    lat2, lon2 = np.radians(lat2), np.radians(lon2)
    inner = (
        np.sin((lat2 - lat1) / 2.0) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2.0) ** 2
    )
    return 6371.0 * 2.0 * np.arcsin(np.sqrt(np.clip(inner, 0.0, 1.0)))


def _far_domestic_city(
    context: _Context, lat: float, lon: float, rng: np.random.Generator
) -> pd.Series:
    """An Indian city far enough that reaching it in under an hour is impossible."""
    pool = context.domestic
    distance = _haversine_km(pool["lat"].to_numpy(), pool["lon"].to_numpy(), lat, lon)
    far = np.flatnonzero(distance > 900.0)
    if not len(far):
        far = np.array([int(np.argmax(distance))])
    return pool.iloc[int(rng.choice(far))]


def _foreign_cities() -> pd.DataFrame:
    from fraud.sim.population import load_cities

    cities = load_cities()
    return cities[cities["country"] != "IN"].reset_index(drop=True)


# --- shared row construction ------------------------------------------------


def _rows(
    context: _Context,
    *,
    times: np.ndarray,
    account_id: np.ndarray | str,
    merchant_rows: np.ndarray,
    amounts: np.ndarray,
    device_id: np.ndarray | str,
    ip: str,
    ip_city: pd.Series,
    declined: np.ndarray,
    fraud_type: str,
    attack_id: str,
    ring_id: str | None = None,
    channel: str = "ONLINE",
) -> pd.DataFrame:
    """One attack's events. Remote attacks are online, so location comes from the IP."""
    merchants = context.merchants
    category = merchants["category"].to_numpy()[merchant_rows]

    return pd.DataFrame(
        {
            "event_time": times,
            "account_id": account_id,
            "merchant_id": merchants["merchant_id"].to_numpy()[merchant_rows],
            "merchant_category": category,
            "amount": np.round(np.maximum(amounts, _MIN_AMOUNT), 2),
            "channel": channel,
            "device_id": device_id,
            "ip": ip,
            "lat": float(ip_city["lat"]),
            "lon": float(ip_city["lon"]),
            "city": ip_city["city"],
            "country": ip_city["country"],
            "status": np.where(declined, "DECLINED", "APPROVED"),
            "fraud_type": fraud_type,
            "attack_id": attack_id,
            "ring_id": ring_id,
        }
    )


def _spread(
    rng: np.random.Generator, origin: pd.Timestamp, count: int, minutes: float
) -> np.ndarray:
    """Sorted offsets inside a window, so a burst reads as one session.

    The first event lands exactly on the origin. Letting it drift meant an ATO could
    begin later than its configured lag after the victim's genuine purchase, which both
    broke the lag bound and weakened the impossible-travel signal it depends on.
    """
    offsets = np.sort(rng.uniform(0.0, minutes * 60.0, count))
    offsets[0] = 0.0
    return origin.to_datetime64() + offsets.astype("timedelta64[s]")


def _victim_pool(context: _Context, minimum: int) -> np.ndarray:
    """Accounts with enough history to be worth attacking."""
    eligible = context.activity[context.activity >= minimum]
    return eligible.index.to_numpy()


# --- VELOCITY ---------------------------------------------------------------


def _inject_velocity(
    context: _Context, registry: _Registry, rng: np.random.Generator
) -> pd.DataFrame:
    """A compromised card drained fast: many purchases in minutes (PLAN §4.5)."""
    spec = context.config["patterns"]["velocity"]
    pool = _victim_pool(context, 5)
    if not len(pool):
        return pd.DataFrame(columns=ATTACK_COLUMNS)

    merchants = context.merchants_in(list(spec["categories"]))
    frames: list[pd.DataFrame] = []

    for index in range(int(spec["n_attacks"])):
        victim = str(rng.choice(pool))
        history = context.legit.loc[context.legit["account_id"] == victim, "event_time"]
        if history.empty:
            continue

        origin = pd.Timestamp(rng.choice(history.to_numpy()))
        count = int(rng.integers(int(spec["txns_min"]), int(spec["txns_max"]) + 1))
        window = float(
            rng.uniform(float(spec["window_minutes_min"]), float(spec["window_minutes_max"]))
        )

        shops = int(rng.integers(int(spec["merchants_min"]), int(spec["merchants_max"]) + 1))
        picked = rng.choice(merchants, size=min(shops, len(merchants)), replace=False)

        # A new device 70% of the time; otherwise the victim's own, which is harder.
        if rng.random() < float(spec["new_device_share"]):
            device = registry.device()
            city = context.domestic.iloc[int(rng.integers(0, len(context.domestic)))]
            ip = registry.ip("attacker", city)
        else:
            device, ip, city = _victim_session(context, registry, victim, origin, rng)

        amount = np.minimum(
            float(spec["amount_median"])
            * np.exp(rng.normal(0.0, float(spec["amount_sigma"]), count)),
            float(spec["amount_cap"]),
        )
        rate = float(rng.uniform(float(spec["decline_rate_min"]), float(spec["decline_rate_max"])))

        frames.append(
            _rows(
                context,
                times=_spread(rng, origin, count, window),
                account_id=victim,
                merchant_rows=picked[rng.integers(0, len(picked), count)],
                amounts=amount,
                device_id=device,
                ip=ip,
                ip_city=city,
                declined=rng.random(count) < rate,
                fraud_type="VELOCITY",
                attack_id=f"VEL{index:04d}",
            )
        )

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=ATTACK_COLUMNS)


def _victim_session(
    context: _Context,
    registry: _Registry,
    victim: str,
    moment: pd.Timestamp,
    rng: np.random.Generator,
) -> tuple[str, str, pd.Series]:
    """Reuse the victim's own device and home IP: a hijacked session."""
    links = context.population.account_devices
    owned = links[links["account_id"] == victim]
    live = owned[(owned["valid_from"] <= moment) & (owned["valid_to"] > moment)]
    device = str((live if len(live) else owned)["device_id"].iloc[0])

    home = context.population.account_ips.query("account_id == @victim and ip_type == 'home'")
    if home.empty:
        city = context.domestic.iloc[int(rng.integers(0, len(context.domestic)))]
        return device, registry.ip("attacker", city), city

    address = str(home["ip"].iloc[0])
    city = context.cities.loc[context.population.ips.set_index("ip").loc[address, "city"]]
    return device, address, city


# --- ATO --------------------------------------------------------------------


def _inject_ato(context: _Context, registry: _Registry, rng: np.random.Generator) -> pd.DataFrame:
    """A takeover from a new device far away, minutes after the owner paid at home."""
    spec = context.config["patterns"]["ato"]
    pool = _victim_pool(context, int(spec["victim_min_prior_txns"]))
    if not len(pool):
        return pd.DataFrame(columns=ATTACK_COLUMNS)

    merchants = context.merchants_in(list(spec["categories"]))
    at_home = context.legit[context.legit["channel"] == "POS"]
    by_account = at_home.groupby("account_id").indices
    frames: list[pd.DataFrame] = []

    for index in range(int(spec["n_attacks"])):
        victim = str(rng.choice(pool))
        anchors = by_account.get(victim)
        if anchors is None or not len(anchors):
            continue

        # The signature: a genuine card-present purchase at home shortly before.
        anchor = at_home.iloc[int(rng.choice(anchors))]
        lag = float(
            rng.uniform(
                float(spec["lag_after_legit_minutes_min"]),
                float(spec["lag_after_legit_minutes_max"]),
            )
        )
        origin = pd.Timestamp(anchor["event_time"]) + pd.Timedelta(minutes=lag)
        if origin >= context.end:
            continue

        if rng.random() < float(spec["foreign_ip_share"]):
            city = context.foreign_cities.iloc[int(rng.integers(0, len(context.foreign_cities)))]
        else:
            # PLAN §4.8 requires >900 km/h at the first fraud event, and the lag is at
            # most an hour, so a neighbouring city would not register as a jump at all.
            city = _far_domestic_city(context, float(anchor["lat"]), float(anchor["lon"]), rng)

        count = int(rng.integers(int(spec["txns_min"]), int(spec["txns_max"]) + 1))
        window = float(
            rng.uniform(float(spec["window_minutes_min"]), float(spec["window_minutes_max"]))
        )
        typical = float(context.mean_amount.get(victim, 1000.0))
        multiplier = rng.uniform(
            float(spec["amount_mult_min"]), float(spec["amount_mult_max"]), count
        )

        frames.append(
            _rows(
                context,
                times=_spread(rng, origin, count, window),
                account_id=victim,
                merchant_rows=rng.choice(merchants, size=count),
                amounts=np.minimum(typical * multiplier, float(spec["amount_cap"])),
                device_id=registry.device(),
                ip=registry.ip("attacker", city),
                ip_city=city,
                declined=rng.random(count) < float(spec["decline_rate"]),
                fraud_type="ATO",
                attack_id=f"ATO{index:04d}",
            )
        )

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=ATTACK_COLUMNS)


# --- CARD TESTING -----------------------------------------------------------


def _inject_card_testing(
    context: _Context, registry: _Registry, rng: np.random.Generator
) -> pd.DataFrame:
    """One device checks a batch of stolen cards, then cashes out the live ones."""
    spec = context.config["patterns"]["card_testing"]
    accounts = context.population.accounts["account_id"].to_numpy()

    low_friction = np.flatnonzero(context.merchants["is_low_friction"].to_numpy())
    if not len(low_friction):
        return pd.DataFrame(columns=ATTACK_COLUMNS)
    cashout = context.merchants_in(list(spec["cashout_categories"]))

    frames: list[pd.DataFrame] = []
    for index in range(int(spec["n_attacks"])):
        attack_id = f"CT{index:04d}"
        device = registry.device()
        kind = "vpn" if rng.random() < float(spec["vpn_share"]) else "attacker"
        pool = context.foreign_cities if kind == "vpn" else context.domestic
        city = pool.iloc[int(rng.integers(0, len(pool)))]
        ip = registry.ip(kind, city)

        window = float(
            rng.uniform(float(spec["window_minutes_min"]), float(spec["window_minutes_max"]))
        )
        origin = context.start + pd.Timedelta(
            seconds=int(rng.integers(0, int((context.end - context.start).total_seconds())))
        )

        # Only cards that exist yet. Drawing from every account let an attack probe an
        # account created weeks later, which put events before their own account.
        existing = accounts[context.created_at.to_numpy() <= origin.to_datetime64()]
        wanted = int(rng.integers(int(spec["victims_min"]), int(spec["victims_max"]) + 1))
        if len(existing) < int(spec["victims_min"]):
            continue
        victims = rng.choice(existing, size=min(wanted, len(existing)), replace=False)

        shops = int(rng.integers(int(spec["merchants_min"]), int(spec["merchants_max"]) + 1))
        picked = rng.choice(low_friction, size=min(shops, len(low_friction)), replace=False)

        count = len(victims)
        rate = float(rng.uniform(float(spec["decline_rate_min"]), float(spec["decline_rate_max"])))
        declined = rng.random(count) < rate

        frames.append(
            _rows(
                context,
                times=_spread(rng, origin, count, window),
                account_id=victims,
                merchant_rows=picked[rng.integers(0, len(picked), count)],
                amounts=rng.uniform(float(spec["amount_min"]), float(spec["amount_max"]), count),
                device_id=device,
                ip=ip,
                ip_city=city,
                declined=declined,
                fraud_type="CARD_TESTING",
                attack_id=attack_id,
            )
        )

        # The cards that went through are worth real money a few hours later.
        live = victims[~declined]
        chosen = live[rng.random(len(live)) < float(spec["cashout_share"])]
        if not len(chosen):
            continue

        delay = rng.uniform(
            float(spec["cashout_delay_hours_min"]),
            float(spec["cashout_delay_hours_max"]),
            len(chosen),
        )
        times = origin.to_datetime64() + (delay * 3600.0).astype("timedelta64[s]")
        frames.append(
            _rows(
                context,
                times=times,
                account_id=chosen,
                merchant_rows=rng.choice(cashout, size=len(chosen)),
                amounts=rng.uniform(
                    float(spec["cashout_amount_min"]),
                    float(spec["cashout_amount_max"]),
                    len(chosen),
                ),
                device_id=device,
                ip=ip,
                ip_city=city,
                declined=np.zeros(len(chosen), dtype=bool),
                fraud_type="CARD_TESTING",
                attack_id=attack_id,
            )
        )

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=ATTACK_COLUMNS)


# --- RING -------------------------------------------------------------------


def _ring_schedule(
    context: _Context, spec: dict[str, Any], rng: np.random.Generator
) -> list[tuple[pd.Timestamp, int]]:
    """Place rings so the §4.5 quotas hold instead of depending on luck."""
    low, high = int(spec["active_days_min"]), int(spec["active_days_max"])
    total = int(spec["n_rings"])
    schedule: list[tuple[pd.Timestamp, int]] = []

    train_start, train_end = context.train
    test_start, test_end = context.test

    for _ in range(int(spec["min_rings_fully_in_train"])):
        span = int(rng.integers(low, high + 1))
        latest = (train_end - train_start).days - span
        offset = int(rng.integers(0, max(latest, 1)))
        schedule.append((train_start + pd.Timedelta(days=offset), span))

    for _ in range(int(spec["min_rings_in_test"])):
        span = int(rng.integers(low, high + 1))
        offset = int(rng.integers(0, max((test_end - test_start).days, 1)))
        schedule.append((test_start + pd.Timedelta(days=offset), span))

    while len(schedule) < total:
        span = int(rng.integers(low, high + 1))
        latest = (context.end - context.start).days - span
        offset = int(rng.integers(0, max(latest, 1)))
        schedule.append((context.start + pd.Timedelta(days=offset), span))

    # Chronological: the device-reuse lookback asks which rings were active in the
    # previous 30 days, which is meaningless if the rings are visited out of order.
    return sorted(schedule[:total], key=lambda item: item[0])


def _inject_rings(context: _Context, registry: _Registry, rng: np.random.Generator) -> pd.DataFrame:
    """Mule accounts sharing devices and IPs, cashing out through colluding merchants.

    Every individual transaction is unremarkable. Only the shared infrastructure gives
    the ring away, which is the whole reason the graph layer exists (PLAN §6).
    """
    spec = context.config["patterns"]["ring"]
    schedule = _ring_schedule(context, spec, rng)

    colluding = np.flatnonzero(context.merchants["is_colluding"].to_numpy())
    popular = np.argsort(-context.merchants["popularity"].to_numpy())[:200]
    if not len(colluding):
        return pd.DataFrame(columns=ATTACK_COLUMNS)

    profile = time_of_day_profile(context.config["legit"]["diurnal"], 0.0)
    legit_spec = context.config["legit"]
    history: list[tuple[pd.Timestamp, list[str]]] = []
    frames: list[pd.DataFrame] = []

    for index, (begin, span) in enumerate(schedule):
        ring_id = f"RING{index:04d}"
        finish = min(begin + pd.Timedelta(days=span), context.end)

        n_devices = int(rng.integers(int(spec["devices_min"]), int(spec["devices_max"]) + 1))
        devices = [registry.device("ring") for _ in range(n_devices)]

        # 30% of rings recycle a device from a ring active in the last 30 days, which is
        # what makes the graph's personalised PageRank seeds worth having.
        recent = [
            device
            for when, pool in history
            if (begin - when).days <= int(spec["device_reuse_lookback_days"])
            for device in pool
        ]
        if recent and rng.random() < float(spec["device_reuse_share"]):
            devices[0] = str(rng.choice(recent))
        history.append((begin, devices))

        city = context.domestic.iloc[int(rng.integers(0, len(context.domestic)))]
        addresses = [
            registry.ip("ring", city)
            for _ in range(int(rng.integers(int(spec["ips_min"]), int(spec["ips_max"]) + 1)))
        ]

        members = int(rng.integers(int(spec["accounts_min"]), int(spec["accounts_max"]) + 1))
        for _ in range(members):
            age = int(
                rng.integers(
                    int(spec["account_age_days_min"]), int(spec["account_age_days_max"]) + 1
                )
            )
            created = max(begin - pd.Timedelta(days=age), context.start)
            account_id = registry.account(
                created_at=created.to_datetime64().astype("datetime64[s]"),
                home_city=city["city"],
                home_lat=float(city["lat"]) + rng.normal(0.0, 0.03),
                home_lon=float(city["lon"]) + rng.normal(0.0, 0.03),
                spend_level=float(np.exp(rng.normal(0.0, 0.4))),
                activity_rate=0.0,
                is_night_owl=False,
                family_id=pd.NA,
            )

            for device in devices:
                registry.account_devices.append(
                    {
                        "account_id": account_id,
                        "device_id": device,
                        "valid_from": created.to_datetime64().astype("datetime64[s]"),
                        "valid_to": context.end.to_datetime64().astype("datetime64[s]"),
                        "weight": 1.0 / len(devices),
                    }
                )
            for address in addresses:
                registry.account_ips.append(
                    {
                        "account_id": account_id,
                        "ip": address,
                        "ip_type": "ring",
                        "usage_share": 1.0 / len(addresses),
                    }
                )

            count = int(
                rng.integers(
                    int(spec["txns_per_account_min"]), int(spec["txns_per_account_max"]) + 1
                )
            )
            at_colluding = rng.random(count) < float(spec["colluding_share"])
            merchant_rows = np.where(
                at_colluding,
                rng.choice(colluding, size=count),
                rng.choice(popular, size=count),
            )

            times = _normal_looking_times(rng, profile, begin, finish, count)
            category = context.merchants["category"].to_numpy()[merchant_rows]
            median = np.array([float(context.by_category[c]["amount_median"]) for c in category])
            sigma = np.array([float(context.by_category[c]["amount_sigma"]) for c in category])
            spend = float(registry.accounts[-1]["spend_level"])

            frames.append(
                _rows(
                    context,
                    times=times,
                    account_id=account_id,
                    merchant_rows=merchant_rows,
                    amounts=median * spend * np.exp(rng.normal(0.0, sigma)),
                    device_id=str(rng.choice(devices, size=1)[0]),
                    ip=str(rng.choice(addresses, size=1)[0]),
                    ip_city=city,
                    # Ordinary approval odds: nothing about a single ring payment is odd.
                    declined=rng.random(count) < float(legit_spec["decline_rate"]),
                    fraud_type="RING",
                    attack_id=ring_id,
                    ring_id=ring_id,
                )
            )

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=ATTACK_COLUMNS)


def _normal_looking_times(
    rng: np.random.Generator,
    profile: np.ndarray,
    begin: pd.Timestamp,
    finish: pd.Timestamp,
    count: int,
) -> np.ndarray:
    """Spread across the ring's active days using the ordinary diurnal profile."""
    span = max((finish - begin).days, 1)
    day = rng.integers(0, span, count)
    bucket = rng.choice(_BINS_PER_DAY, size=count, p=profile)
    seconds = 86400 // _BINS_PER_DAY
    offset = day * 86400 + bucket * seconds + rng.integers(0, seconds, count)
    return np.sort(begin.to_datetime64() + offset.astype("timedelta64[s]"))
