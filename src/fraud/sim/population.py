"""Accounts, merchants, devices and IP pools: the static population (PLAN §4.3).

No transactions are produced here. This module builds the cast of actors that
``legit.py`` and ``patterns.py`` then generate events for.

Two modelling rules are fixed here because everything downstream depends on them:

*Merchant is_online vs category online_share.* These are deliberately separate.
``merchants.online_merchant_share`` decides which merchants have no physical storefront
(§4.3, "~35% online") and is reference data on the merchant table (§4.2). It does not
decide any transaction's channel. The channel comes from the category's ``online_share``
in ``legit.py``, and the model feature ``is_online`` is derived from the event channel,
not from this column (§5.2, feature 4).

*IP geolocation.* A shared IP is placed in its members' own city wherever possible.
Placing carrier-NAT or office IPs randomly would make ordinary online shopping look like
impossible travel and poison ``geo_speed_kmh`` for every legitimate account.

Ring mule accounts are NOT created here. Their ages are defined relative to the ring's
start date, so ``patterns.py`` creates them and appends them to the accounts table.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import pandas as pd

from fraud.config import CONFIG_DIR

# Independent generator per component (PLAN §4.8). APPEND ONLY: inserting a name
# shifts every stream after it, which silently changes the whole dataset.
STREAMS: Final[tuple[str, ...]] = (
    "population_accounts",
    "population_merchants",
    "population_regulars",
    "population_devices",
    "population_ips",
    "legit",
    "pattern_velocity",
    "pattern_ato",
    "pattern_card_testing",
    "pattern_ring",
)

# Plausible-looking IPv4 space per pool. 100.64.0.0/10 is the real carrier-grade NAT
# range, which is exactly what the NAT pool represents.
_IP_PREFIXES: Final[dict[str, tuple[int, int]]] = {
    "home": (49, 106),
    "nat": (100, 100),
    "office": (14, 203),
    "vpn": (45, 185),
}

_DEGREES_PER_KM: Final[float] = 1.0 / 111.0
_CITY_JITTER_KM: Final[float] = 4.0
_SECOND_DEVICE_WEIGHT: Final[float] = 0.35


@dataclass(frozen=True, slots=True)
class Population:
    """Every actor in the simulation, before any event exists."""

    accounts: pd.DataFrame
    merchants: pd.DataFrame
    devices: pd.DataFrame
    account_devices: pd.DataFrame
    ips: pd.DataFrame
    account_ips: pd.DataFrame
    regular_merchants: pd.DataFrame
    # sim-v2: one unused handset per account, picked up only when legit.py decides a
    # trip or a VPN session happens on a different machine. Deliberately NOT in
    # account_devices, so it stays new to the account until first used.
    spare_devices: pd.DataFrame = field(default_factory=pd.DataFrame)

    def fingerprint(self) -> str:
        """Stable hash of every table, used to prove two runs agree (PLAN §4.8)."""
        digest = hashlib.sha256()
        for frame in (
            self.accounts,
            self.merchants,
            self.devices,
            self.account_devices,
            self.ips,
            self.account_ips,
            self.regular_merchants,
            self.spare_devices,
        ):
            digest.update(
                pd.util.hash_pandas_object(
                    frame.reset_index(drop=True), index=True
                ).values.tobytes()
            )
        return digest.hexdigest()


def spawn_streams(seed: int, names: tuple[str, ...] = STREAMS) -> dict[str, np.random.Generator]:
    """One independent generator per named component, all derived from one seed."""
    children = np.random.SeedSequence(seed).spawn(len(names))
    return {name: np.random.default_rng(child) for name, child in zip(names, children, strict=True)}


def load_cities() -> pd.DataFrame:
    """Read configs/cities.csv. Weights are relative within a country group."""
    with (CONFIG_DIR / "cities.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    frame = pd.DataFrame(rows)
    for column in ("lat", "lon", "weight"):
        frame[column] = frame[column].astype(float)
    return frame


def build_population(config: dict[str, Any], categories: list[dict[str, Any]]) -> Population:
    """Build every actor from a simulator config (PLAN §4.3)."""
    rngs = spawn_streams(int(config["seed"]))
    cities = load_cities()

    start = pd.Timestamp(config["start"])
    end = start + pd.Timedelta(days=int(config["days"]))

    accounts = _build_accounts(rngs["population_accounts"], config, cities, start, end)
    merchants = _build_merchants(
        rngs["population_merchants"], config, categories, cities, start, end
    )
    regulars = _build_regular_merchants(rngs["population_regulars"], config, accounts, merchants)
    devices, account_devices, spares = _build_devices(
        rngs["population_devices"], config, accounts, start, end
    )
    ips, account_ips = _build_ips(rngs["population_ips"], config, accounts, cities)

    return Population(
        accounts=accounts,
        merchants=merchants,
        devices=devices,
        account_devices=account_devices,
        ips=ips,
        account_ips=account_ips,
        regular_merchants=regulars,
        spare_devices=spares,
    )


# --- accounts ---------------------------------------------------------------


def _build_accounts(
    rng: np.random.Generator,
    config: dict[str, Any],
    cities: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    spec = config["accounts"]
    n = int(spec["n"])

    # 10% are created mid-simulation. Without them "young account" would be a
    # perfect fraud predictor and the ring pattern would be trivial (PLAN §4.4).
    n_existing = round(n * float(spec["existing_share"]))
    created = np.empty(n, dtype="datetime64[s]")

    old_start = pd.Timestamp(spec["existing_created_start"])
    old_end = pd.Timestamp(spec["existing_created_end"])
    created[:n_existing] = _uniform_timestamps(rng, n_existing, old_start, old_end)
    created[n_existing:] = _uniform_timestamps(rng, n - n_existing, start, end)

    domestic = cities[cities["country"] == "IN"].reset_index(drop=True)
    picked = rng.choice(len(domestic), size=n, p=domestic["weight"] / domestic["weight"].sum())

    jitter = _CITY_JITTER_KM * _DEGREES_PER_KM
    spend = spec["spend_level"]
    rate = spec["activity_rate"]

    frame = pd.DataFrame(
        {
            "account_id": _ids("A", n),
            "created_at": created,
            "home_city": domestic["city"].to_numpy()[picked],
            "home_lat": domestic["lat"].to_numpy()[picked] + rng.normal(0.0, jitter, n),
            "home_lon": domestic["lon"].to_numpy()[picked] + rng.normal(0.0, jitter, n),
            "spend_level": _lognormal(rng, float(spend["median"]), float(spend["sigma"]), n),
            "activity_rate": _lognormal(
                rng, float(rate["median_per_day"]), float(rate["sigma"]), n
            ),
            "is_night_owl": rng.random(n) < float(config["legit"]["night_owl_share"]),
            "family_id": pd.Series([pd.NA] * n, dtype="string"),
        }
    )

    frame["activity_rate"] *= _rate_scale(config, frame, start, end)
    return frame.sort_values("account_id", ignore_index=True)


def _rate_scale(
    config: dict[str, Any], accounts: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> float:
    """Rescale activity so the legitimate stream lands on target (PLAN §4.3).

    Accounts created mid-simulation are active for less than the full window, and the
    weekday factor does not average to 1, so both are accounted for. The fraud targets
    are subtracted because those events are injected on top; the hard negatives add a
    further couple of percent, which target_tolerance absorbs.
    """
    active_days = (end - accounts["created_at"].clip(lower=start)).dt.total_seconds() / 86400.0
    weekday_mean = float(np.mean(config["legit"]["weekday_factor"]))
    expected = float((accounts["activity_rate"] * active_days).sum()) * weekday_mean
    if expected <= 0.0:
        return 1.0

    fraud = sum(int(p["target_transactions"]) for p in config["patterns"].values())
    bursts = _expected_micro_burst_events(config, len(accounts))

    # Shopping sprees add events in proportion to the number of ACTIVE account-days,
    # which is itself proportional to the base volume, so it folds into the divisor.
    spree = config["hard_negatives"]["shopping_spree"]
    per_spree = (int(spree["extra_txns_min"]) + int(spree["extra_txns_max"])) / 2.0
    # P(at least one event) / E[events] for a small-lambda Poisson, i.e. active days
    # per event. Exact enough at the rates in §4.3 and avoids a second pass.
    active_day_ratio = 0.91
    inflation = 1.0 + float(spree["account_day_share"]) * per_spree * active_day_ratio

    budget = int(config["target_transactions"]) - fraud - bursts
    return max(budget, 1) / (expected * inflation)


def _expected_micro_burst_events(config: dict[str, Any], n_accounts: int) -> float:
    """Micro-bursts are a fixed count per account, independent of the base volume."""
    spec = config["hard_negatives"]["micro_burst"]
    episodes = (int(spec["episodes_min"]) + int(spec["episodes_max"])) / 2.0
    per_episode = (int(spec["txns_min"]) + int(spec["txns_max"])) / 2.0
    return n_accounts * float(spec["account_share"]) * episodes * per_episode


# --- merchants --------------------------------------------------------------


def _build_merchants(
    rng: np.random.Generator,
    config: dict[str, Any],
    categories: list[dict[str, Any]],
    cities: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    spec = config["merchants"]
    n = int(spec["n"])

    names = [c["name"] for c in categories]
    shares = np.array([float(c["merchant_share"]) for c in categories])
    category = np.array(names)[rng.choice(len(names), size=n, p=shares / shares.sum())]

    # Zipf popularity: a few hubs carry most of the volume, and a long tail of small
    # merchants that the colluding ones have to hide in (PLAN §4.4).
    rank = rng.permutation(n) + 1
    popularity = 1.0 / rank ** float(spec["zipf_exponent"])
    popularity /= popularity.sum()

    # Reference data only: no physical storefront. The channel of a transaction is
    # decided by the category's online_share in legit.py, never by this flag.
    is_online = rng.random(n) < float(spec["online_merchant_share"])

    international = rng.random(n) < float(spec["international_share"])
    is_foreign = (cities["country"] != "IN").to_numpy()
    city_index = np.empty(n, dtype=int)
    for foreign in (False, True):
        positions = np.flatnonzero(is_foreign == foreign)
        weights = cities["weight"].to_numpy()[positions]
        mask = international == foreign
        city_index[mask] = rng.choice(positions, size=int(mask.sum()), p=weights / weights.sum())

    jitter = _CITY_JITTER_KM * _DEGREES_PER_KM
    frame = pd.DataFrame(
        {
            "merchant_id": _ids("M", n),
            "name": _merchant_names(rng, category),
            "category": category,
            "city": cities["city"].to_numpy()[city_index],
            "country": cities["country"].to_numpy()[city_index],
            "lat": cities["lat"].to_numpy()[city_index] + rng.normal(0.0, jitter, n),
            "lon": cities["lon"].to_numpy()[city_index] + rng.normal(0.0, jitter, n),
            "is_online": is_online,
            "created_at": _uniform_timestamps(rng, n, start - pd.Timedelta(days=1825), start),
            "popularity": popularity,
            "is_low_friction": np.zeros(n, dtype=bool),
            "is_colluding": np.zeros(n, dtype=bool),
        }
    )

    _flag_low_friction(rng, frame, spec)
    _flag_colluding(rng, frame, spec, start, end)
    return frame.sort_values("merchant_id", ignore_index=True)


def _flag_low_friction(
    rng: np.random.Generator, merchants: pd.DataFrame, spec: dict[str, Any]
) -> None:
    """Tiny digital merchants that card testing abuses and real customers also use."""
    eligible = merchants.index[merchants["category"].isin(spec["low_friction_categories"])]
    count = min(int(spec["n_low_friction"]), len(eligible))
    merchants.loc[rng.choice(eligible, size=count, replace=False), "is_low_friction"] = True


def _flag_colluding(
    rng: np.random.Generator,
    merchants: pd.DataFrame,
    spec: dict[str, Any],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    """Ring cash-out points: created mid-simulation, and small enough to hide.

    They are drawn from the unpopular half so they sit among the ordinary small
    merchants that form the hard negative in PLAN §4.4.
    """
    eligible = merchants.index[
        merchants["category"].isin(spec["colluding_categories"])
        & (merchants["popularity"] <= merchants["popularity"].median())
    ]
    count = min(int(spec["n_colluding"]), len(eligible))
    chosen = rng.choice(eligible, size=count, replace=False)

    merchants.loc[chosen, "is_colluding"] = True
    merchants.loc[chosen, "created_at"] = _uniform_timestamps(rng, count, start, end)


def _merchant_names(rng: np.random.Generator, category: np.ndarray) -> list[str]:
    """Readable names without pulling in Faker, which is an optional dependency."""
    prefixes = np.array(
        [
            "Shree",
            "Nova",
            "Urban",
            "Prime",
            "Royal",
            "Metro",
            "Swift",
            "Bharat",
            "Lotus",
            "Apex",
            "Green",
            "Star",
            "Sunrise",
            "Classic",
            "Elite",
        ]
    )
    suffixes = {
        "grocery": ["Mart", "Bazaar", "Stores", "Supermart"],
        "fuel": ["Fuels", "Petro", "Energy"],
        "restaurants": ["Kitchen", "Diner", "Cafe", "Bistro"],
        "pharmacy": ["Pharmacy", "Medicals", "Chemist"],
        "utilities": ["Utilities", "Power", "Services"],
        "ecommerce": ["Online", "Shop", "Retail"],
        "electronics": ["Electronics", "Digital", "Devices"],
        "travel": ["Travels", "Holidays", "Voyages"],
        "digital_goods": ["Digital", "Credits", "Downloads"],
        "gift_cards_wallet": ["Wallet", "Giftcards", "Vouchers"],
    }
    first = prefixes[rng.integers(0, len(prefixes), len(category))]
    second = [rng.choice(suffixes[c]) for c in category]
    return [f"{a} {b}" for a, b in zip(first, second, strict=True)]


def reachable_merchants(merchants: pd.DataFrame, city: str) -> tuple[np.ndarray, np.ndarray]:
    """Merchants an account in ``city`` can actually use, with normalised weights.

    An online merchant is reachable from anywhere; a physical one only in its own city.
    Drawing regulars globally instead would give a Mumbai account a regular shop in
    Delhi, so every ordinary visit would look like travel and ``geo_speed_kmh`` would be
    meaningless (PLAN §5.2).
    """
    eligible = np.flatnonzero(
        merchants["is_online"].to_numpy() | (merchants["city"].to_numpy() == city)
    )
    weights = merchants["popularity"].to_numpy()[eligible]
    return eligible, weights / weights.sum()


def _build_regular_merchants(
    rng: np.random.Generator,
    config: dict[str, Any],
    accounts: pd.DataFrame,
    merchants: pd.DataFrame,
) -> pd.DataFrame:
    """The 5-15 merchants each account keeps returning to (PLAN §4.4)."""
    spec = config["accounts"]
    low, high = int(spec["regular_merchants_min"]), int(spec["regular_merchants_max"])
    n = len(accounts)

    counts = rng.integers(low, high + 1, n)
    account_ids = accounts["account_id"].to_numpy()
    merchant_ids = merchants["merchant_id"].to_numpy()
    home_city = accounts["home_city"].to_numpy()

    owner: list[str] = []
    picked: list[str] = []
    # Drawn per city, because the reachable pool depends on where the account lives.
    for city in np.unique(home_city):
        members = np.flatnonzero(home_city == city)
        eligible, weights = reachable_merchants(merchants, str(city))
        draws = rng.choice(eligible, size=(len(members), high), p=weights)

        for offset, row in enumerate(members):
            # Duplicates are dropped, so an account can end up with slightly fewer
            # regulars than were drawn.
            unique = pd.unique(draws[offset, : counts[row]])
            owner.extend([account_ids[row]] * len(unique))
            picked.extend(merchant_ids[unique])

    return pd.DataFrame({"account_id": owner, "merchant_id": picked})


# --- devices ----------------------------------------------------------------


def _build_devices(
    rng: np.random.Generator,
    config: dict[str, Any],
    accounts: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Personal devices, household devices, widely shared devices and upgrades.

    A device is valid over a window, so an upgrade is just two non-overlapping
    windows rather than a special case downstream.
    """
    spec = config["devices"]
    n = len(accounts)
    account_ids = accounts["account_id"].to_numpy()
    first_seen = accounts["created_at"].to_numpy().astype("datetime64[s]")
    floor = np.maximum(first_seen, np.datetime64(start.to_datetime64(), "s"))

    counter = 0

    def next_device(kind: str) -> str:
        """Allocate a device id AND register it.

        Registration used to be the caller's job, and a caller forgot: the spare
        handsets were issued ids but never added to the registry, so patterns.py
        started its own counter below them and handed attacker devices the same ids.
        Allocating and registering together makes that impossible.
        """
        nonlocal counter
        device_id = f"D{counter:07d}"
        counter += 1
        registry.append((device_id, kind))
        return device_id

    registry: list[tuple[str, str]] = []
    links: list[dict[str, Any]] = []

    has_two = rng.random(n) < float(spec["two_device_share"])
    # Upgraders come from the single-device, non-family accounts, which keeps the
    # per-account weights unambiguous.
    family_members = _assign_families(rng, spec, accounts)
    upgrades = (rng.random(n) < float(spec["upgrade_share"])) & ~has_two & ~family_members.notna()

    upgrade_at = _uniform_timestamps(
        rng,
        n,
        start + pd.Timedelta(days=int(config["days"] * 0.2)),
        start + pd.Timedelta(days=int(config["days"] * 0.8)),
    )

    for row in range(n):
        personal: list[tuple[str, np.datetime64, np.datetime64, float]] = []
        primary = next_device("personal")

        if upgrades[row]:
            swap = max(upgrade_at[row], floor[row])
            replacement = next_device("personal")
            personal.append((primary, floor[row], swap, 1.0))
            personal.append((replacement, swap, np.datetime64(end.to_datetime64(), "s"), 1.0))
        elif has_two[row]:
            secondary = next_device("personal")
            stop = np.datetime64(end.to_datetime64(), "s")
            personal.append((primary, floor[row], stop, 1.0 - _SECOND_DEVICE_WEIGHT))
            personal.append((secondary, floor[row], stop, _SECOND_DEVICE_WEIGHT))
        else:
            personal.append((primary, floor[row], np.datetime64(end.to_datetime64(), "s"), 1.0))

        share = 0.0 if pd.isna(family_members.iloc[row]) else float(spec["family_device_share"])
        for device_id, valid_from, valid_to, weight in personal:
            links.append(
                {
                    "account_id": account_ids[row],
                    "device_id": device_id,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "weight": weight * (1.0 - share),
                }
            )

    household = _build_family_devices(family_members, accounts, spec, start, end, next_device)
    links.extend(household)

    # sim-v2: legitimate accounts sharing one device with many others, which is the
    # band a fraud ring lives in. Family members are excluded so no account carries
    # two shared-device weightings.
    links.extend(
        _build_shared_devices(rng, spec, accounts, family_members, start, end, next_device, links)
    )

    spares = [
        {"account_id": account_ids[row], "device_id": next_device("spare")} for row in range(n)
    ]

    devices = pd.DataFrame(registry, columns=["device_id", "device_type"])
    accounts["family_id"] = family_members.to_numpy()
    return devices, pd.DataFrame(links), pd.DataFrame(spares)


def _build_shared_devices(
    rng: np.random.Generator,
    spec: dict[str, Any],
    accounts: pd.DataFrame,
    family_members: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
    next_device: Any,
    links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Devices shared by 5-15 unrelated accounts (PLAN §4.8 sim-v2 revision).

    Before this existed no legitimate account shared a device with five others, so
    ``dev_accts_30d`` nearly identified a ring by itself and the graph layer had
    nothing left to demonstrate. See reports/sim_realism_review.md.
    """
    n = len(accounts)
    account_ids = accounts["account_id"].to_numpy()
    share = float(spec.get("shared_device_share", 0.0))
    if share <= 0.0:
        return []

    eligible = np.flatnonzero(family_members.isna().to_numpy())
    wanted = min(round(n * share), len(eligible))
    pool = list(rng.permutation(eligible)[:wanted])

    low = int(spec["shared_group_min"])
    high = int(spec["shared_group_max"])
    weight = float(spec["shared_device_usage_share"])
    created = accounts["created_at"].to_numpy().astype("datetime64[s]")
    floor = np.maximum(created, np.datetime64(start.to_datetime64(), "s"))
    stop = np.datetime64(end.to_datetime64(), "s")

    by_account: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        by_account.setdefault(link["account_id"], []).append(link)

    extra: list[dict[str, Any]] = []
    while len(pool) >= low:
        size = int(rng.integers(low, min(high, len(pool)) + 1))
        members = [pool.pop() for _ in range(size)]
        device_id = next_device("shared")

        for row in members:
            # Scale the account's own devices down so its weights still total 1.
            for link in by_account.get(account_ids[row], []):
                link["weight"] *= 1.0 - weight
            extra.append(
                {
                    "account_id": account_ids[row],
                    "device_id": device_id,
                    "valid_from": floor[row],
                    "valid_to": stop,
                    "weight": weight,
                }
            )
    return extra


def _assign_families(
    rng: np.random.Generator, spec: dict[str, Any], accounts: pd.DataFrame
) -> pd.Series:
    """Households of 2-4 sharing one device: the legitimate shared-device cluster."""
    n = len(accounts)
    family_of = pd.Series([pd.NA] * n, dtype="string")

    target = round(n * float(spec["family_share"]))
    if target < int(spec["family_size_min"]):
        return family_of

    candidates = rng.permutation(n)[:target]
    low, high = int(spec["family_size_min"]), int(spec["family_size_max"])
    cursor = 0
    group = 0
    while cursor + low <= len(candidates):
        size = int(rng.integers(low, high + 1))
        members = candidates[cursor : cursor + size]
        if len(members) < low:
            break
        family_of.iloc[members] = f"F{group:05d}"
        cursor += size
        group += 1

    return family_of


def _build_family_devices(
    family_of: pd.Series,
    accounts: pd.DataFrame,
    spec: dict[str, Any],
    start: pd.Timestamp,
    end: pd.Timestamp,
    next_device: Any,
) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    account_ids = accounts["account_id"].to_numpy()
    created = accounts["created_at"].to_numpy().astype("datetime64[s]")
    floor = np.maximum(created, np.datetime64(start.to_datetime64(), "s"))
    stop = np.datetime64(end.to_datetime64(), "s")

    for members in family_of.dropna().groupby(family_of.dropna()).groups.values():
        device_id = next_device("family")
        for row in members:
            links.append(
                {
                    "account_id": account_ids[row],
                    "device_id": device_id,
                    # A member who joins mid-simulation cannot have used the
                    # household device before they existed.
                    "valid_from": floor[row],
                    "valid_to": stop,
                    "weight": float(spec["family_device_share"]),
                }
            )
    return links


# --- IP pools ---------------------------------------------------------------


def _build_ips(
    rng: np.random.Generator,
    config: dict[str, Any],
    accounts: pd.DataFrame,
    cities: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Home, carrier-NAT, office and VPN pools (PLAN §4.3).

    Shared IPs are placed in their members' own city. A NAT or office IP in a random
    city would make ordinary online shopping look like impossible travel.
    """
    spec = config["ip_pools"]
    n = len(accounts)
    account_ids = accounts["account_id"].to_numpy()
    home_city = accounts["home_city"].to_numpy()
    family_of = accounts["family_id"].to_numpy()

    domestic = cities[cities["country"] == "IN"].reset_index(drop=True)
    foreign = cities[cities["country"] != "IN"].reset_index(drop=True)
    by_city = cities.set_index("city")

    registry: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    pool = _IpAllocator(rng)

    # Home: one per account, shared within a household.
    family_home: dict[str, str] = {}
    for row in range(n):
        key = family_of[row]
        if key is not None and not pd.isna(key) and key in family_home:
            address = family_home[key]
        else:
            address = pool.take("home")
            registry.append(_ip_row(address, "home", home_city[row], by_city))
            if key is not None and not pd.isna(key):
                family_home[key] = address
        links.append(
            {"account_id": account_ids[row], "ip": address, "ip_type": "home", "usage_share": 1.0}
        )

    _attach_shared_pool(
        rng,
        spec["carrier_nat"],
        "nat",
        accounts,
        domestic,
        by_city,
        pool,
        registry,
        links,
        usage_share=float(spec["carrier_nat"]["usage_share"]),
    )
    _attach_office_pool(rng, spec["office"], accounts, domestic, by_city, pool, registry, links)

    # VPN: foreign exit nodes on the account's own device. The legitimate
    # "impossible travel" hard negative (PLAN §4.4).
    vpn = spec["vpn"]
    addresses = [pool.take("vpn") for _ in range(int(vpn["n_ips"]))]
    picked = rng.choice(len(foreign), size=len(addresses))
    for address, index in zip(addresses, picked, strict=True):
        registry.append(_ip_row(address, "vpn", foreign["city"].iloc[index], by_city))

    users = rng.permutation(n)[: round(n * float(vpn["account_share"]))]
    for row in users:
        links.append(
            {
                "account_id": account_ids[row],
                "ip": addresses[int(rng.integers(0, len(addresses)))],
                "ip_type": "vpn",
                "usage_share": float(vpn["usage_share"]),
            }
        )

    return pd.DataFrame(registry), pd.DataFrame(links)


def _nearest_city_order(cities: pd.DataFrame) -> dict[str, list[str]]:
    """For each city, every city ordered by great-circle distance.

    Used when a shared pool has no IP in an account's own city: the next best option is
    the closest city it does cover, not a random one, which keeps geo_speed_kmh sane.
    """
    names = cities["city"].to_numpy()
    lat = np.radians(cities["lat"].to_numpy())
    lon = np.radians(cities["lon"].to_numpy())

    dlat = lat[:, None] - lat[None, :]
    dlon = lon[:, None] - lon[None, :]
    inner = (
        np.sin(dlat / 2.0) ** 2
        + np.cos(lat[:, None]) * np.cos(lat[None, :]) * np.sin(dlon / 2.0) ** 2
    )
    distance = 6371.0 * 2.0 * np.arcsin(np.sqrt(np.clip(inner, 0.0, 1.0)))

    return {
        name: list(names[np.argsort(distance[index], kind="stable")])
        for index, name in enumerate(names)
    }


def _attach_shared_pool(
    rng: np.random.Generator,
    spec: dict[str, Any],
    ip_type: str,
    accounts: pd.DataFrame,
    domestic: pd.DataFrame,
    by_city: pd.DataFrame,
    pool: _IpAllocator,
    registry: list[dict[str, Any]],
    links: list[dict[str, Any]],
    usage_share: float,
) -> None:
    """Carrier NAT: a handful of IPs behind which thousands of accounts sit."""
    n = len(accounts)
    addresses = [pool.take(ip_type) for _ in range(int(spec["n_ips"]))]
    cities_for_ip = domestic["city"].to_numpy()[
        rng.choice(
            len(domestic), size=len(addresses), p=domestic["weight"] / domestic["weight"].sum()
        )
    ]
    for address, city in zip(addresses, cities_for_ip, strict=True):
        registry.append(_ip_row(address, ip_type, city, by_city))

    by_city_index: dict[str, list[str]] = {}
    for address, city in zip(addresses, cities_for_ip, strict=True):
        by_city_index.setdefault(city, []).append(address)

    # A pool rarely covers every city, so fall back to the nearest one it does.
    nearest = _nearest_city_order(domestic)
    resolved: dict[str, list[str]] = {}
    for city in domestic["city"]:
        for candidate in nearest[city]:
            if candidate in by_city_index:
                resolved[city] = by_city_index[candidate]
                break

    users = rng.permutation(n)[: round(n * float(spec["account_share"]))]
    account_ids = accounts["account_id"].to_numpy()
    home_city = accounts["home_city"].to_numpy()

    for row in users:
        local = resolved.get(home_city[row], addresses)
        links.append(
            {
                "account_id": account_ids[row],
                "ip": local[int(rng.integers(0, len(local)))],
                "ip_type": ip_type,
                "usage_share": usage_share,
            }
        )


def _attach_office_pool(
    rng: np.random.Generator,
    spec: dict[str, Any],
    accounts: pd.DataFrame,
    domestic: pd.DataFrame,
    by_city: pd.DataFrame,
    pool: _IpAllocator,
    registry: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> None:
    """Mid-size clusters the graph fan-out cap must keep, unlike NAT (PLAN §6.1)."""
    n = len(accounts)
    n_ips = int(spec["n_ips"])
    target = round(n * float(spec["account_share"]))
    sizes = _allocate_group_sizes(
        rng,
        n_ips,
        target,
        int(spec["group_size_min"]),
        int(spec["group_size_max"]),
        float(spec["group_size_alpha"]),
    )

    addresses = [pool.take("office") for _ in range(n_ips)]
    city_for_ip = domestic["city"].to_numpy()[
        rng.choice(len(domestic), size=n_ips, p=domestic["weight"] / domestic["weight"].sum())
    ]
    for address, city in zip(addresses, city_for_ip, strict=True):
        registry.append(_ip_row(address, "office", city, by_city))

    account_ids = accounts["account_id"].to_numpy()
    home_city = accounts["home_city"].to_numpy()
    # One candidate queue per city. An office fills from its own city first and then
    # from the nearest cities, so colleagues stay geographically plausible.
    available = {
        city: list(rng.permutation(np.flatnonzero(home_city == city))) for city in domestic["city"]
    }
    nearest = _nearest_city_order(domestic)

    for address, city, size in zip(addresses, city_for_ip, sizes, strict=True):
        members: list[int] = []
        for candidate in nearest[city]:
            queue = available.get(candidate, [])
            while queue and len(members) < size:
                members.append(queue.pop())
            if len(members) >= size:
                break

        for row in members:
            links.append(
                {
                    "account_id": account_ids[row],
                    "ip": address,
                    "ip_type": "office",
                    # legit.py applies the weekday and working-hours gate on top of
                    # this probability; population.py only carries the value.
                    "usage_share": float(spec["usage_share"]),
                }
            )


def _allocate_group_sizes(
    rng: np.random.Generator, n_groups: int, target_total: int, low: int, high: int, alpha: float
) -> np.ndarray:
    """Heavy-tailed group sizes in [low, high] that add up to roughly target_total.

    Drawn from a Pareto and then rescaled towards the target, re-clipping each time so
    the clip does not quietly lose mass. If the cap makes the target unreachable the
    result is simply as large as the cap allows.
    """
    sizes = low * (1.0 + rng.pareto(alpha, n_groups))
    sizes = np.clip(sizes, low, high)

    for _ in range(64):
        total = sizes.sum()
        if total <= 0:
            break
        scale = target_total / total
        if abs(scale - 1.0) < 1e-6 or np.all(sizes >= high - 1e-9):
            break
        sizes = np.clip(sizes * scale, low, high)

    return np.clip(np.floor(sizes), low, high).astype(int)


class _IpAllocator:
    """Hands out unique, plausible IPv4 addresses per pool."""

    def __init__(self, rng: np.random.Generator) -> None:
        self._rng = rng
        self._seen: set[str] = set()

    def take(self, ip_type: str) -> str:
        first_options = _IP_PREFIXES[ip_type]
        while True:
            first = int(self._rng.choice(first_options))
            second = int(
                self._rng.integers(64, 128) if ip_type == "nat" else self._rng.integers(0, 256)
            )
            address = f"{first}.{second}.{self._rng.integers(0, 256)}.{self._rng.integers(1, 255)}"
            if address not in self._seen:
                self._seen.add(address)
                return address


def _ip_row(address: str, ip_type: str, city: str, by_city: pd.DataFrame) -> dict[str, Any]:
    row = by_city.loc[city]
    return {
        "ip": address,
        "ip_type": ip_type,
        "city": city,
        "country": row["country"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
    }


# --- small shared helpers ---------------------------------------------------


def _ids(prefix: str, n: int) -> list[str]:
    width = 7 if prefix == "A" else 6
    return [f"{prefix}{i:0{width}d}" for i in range(n)]


def _lognormal(rng: np.random.Generator, median: float, sigma: float, n: int) -> np.ndarray:
    return np.asarray(median * np.exp(rng.normal(0.0, sigma, n)), dtype=float)


def _uniform_timestamps(
    rng: np.random.Generator, n: int, start: pd.Timestamp, end: pd.Timestamp
) -> np.ndarray:
    span = int((end - start).total_seconds())
    offsets = rng.integers(0, max(span, 1), n)
    return (np.datetime64(start.to_datetime64(), "s") + offsets.astype("timedelta64[s]")).astype(
        "datetime64[s]"
    )
