"""Legitimate transactions and the hard negatives (PLAN §4.4).

Everything here is vectorised across the whole population: daily counts come from one
Poisson draw over an (accounts x days) matrix, and each later step operates on the
flattened event arrays. Nothing loops over individual events.

The hard negatives matter as much as the fraud. Each one imitates a signal a pattern
leaves, so a model cannot take the shortcut:

============================  ===========================================
Hard negative                 Fraud signal it imitates
============================  ===========================================
travel                        location jumps (ATO)
VPN                           impossible travel (ATO)
phone upgrade                 new device (ATO, velocity)
shopping spree                velocity abuse
micro-payment burst           card testing
low-friction regulars         card testing
families and offices          shared device and IP (ring)
new legitimate accounts       young accounts (ring)
small merchants               colluding merchants (ring)
============================  ===========================================

The last four fall out of the population: households share a device, offices share an
IP, 10% of accounts are created mid-simulation, and merchant popularity is Zipf, so the
tail is full of small legitimate merchants.

*Channel.* The channel is decided by the category's ``online_share`` and nothing else, so
the realised mix matches ``categories.yaml``. ``is_online`` never changes that probability.
It only says a merchant has no storefront, so once an event has come up POS it is grounded
on a merchant that has one, in the city the account is in at the time. Without that a card
present purchase could land at a merchant in another city and fake a location jump, which
is the very signal ATO detection relies on (PLAN §5.2).
"""

from __future__ import annotations

from typing import Any, Final

import numpy as np
import pandas as pd

from fraud.sim.population import Population, load_cities, reachable_merchants

EVENT_COLUMNS: Final[tuple[str, ...]] = (
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

_BINS_PER_DAY: Final[int] = 96  # 15-minute resolution for the time-of-day profile
_MIN_AMOUNT: Final[float] = 1.0
_IP_SLOTS: Final[tuple[str, ...]] = ("home", "nat", "office", "vpn")


def generate_legit(
    config: dict[str, Any],
    categories: list[dict[str, Any]],
    population: Population,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Every legitimate event, with the hard negatives applied (PLAN §4.4)."""
    start = pd.Timestamp(config["start"])
    days = int(config["days"])

    accounts = population.accounts
    merchants = population.merchants

    day_index, account_row = _draw_daily_counts(rng, config, accounts, start, days)
    seconds = _draw_times(rng, config, accounts, account_row, day_index)
    event_time = np.datetime64(start.to_datetime64(), "s") + seconds.astype("timedelta64[s]")

    merchant_row = _choose_merchants(rng, config, accounts, merchants, population, account_row)
    frame = _assemble(
        rng,
        config,
        categories,
        accounts,
        merchants,
        population,
        account_row,
        merchant_row,
        event_time,
    )

    extra = [
        _inject_shopping_sprees(rng, config, categories, accounts, merchants, frame),
        _inject_micro_bursts(
            rng, config, categories, accounts, merchants, population, frame, start, days
        ),
    ]
    frame = pd.concat([frame, *[e for e in extra if len(e)]], ignore_index=True)

    # Travel is applied last so it relocates the injected events too. Doing it first
    # left a spree seeded at home sitting inside the trip window in the wrong city,
    # which produced an instant city change no journey could explain.
    frame = _apply_travel(rng, config, accounts, merchants, frame)

    end = start + pd.Timedelta(days=days)
    frame = frame[(frame["event_time"] >= start) & (frame["event_time"] < end)]

    # Daily counts are floored at the creation DAY, so an account created at noon could
    # still draw a morning event. No event may precede its account (PLAN §4.8).
    born = frame["account_id"].map(accounts.set_index("account_id")["created_at"])
    frame = frame[frame["event_time"].to_numpy() >= born.to_numpy()]

    return frame.sort_values("event_time", kind="stable", ignore_index=True)


# --- when transactions happen -----------------------------------------------


def _draw_daily_counts(
    rng: np.random.Generator,
    config: dict[str, Any],
    accounts: pd.DataFrame,
    start: pd.Timestamp,
    days: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Poisson counts per account-day, with a weekday factor (PLAN §4.4 step 1)."""
    weekday_factor = np.asarray(config["legit"]["weekday_factor"], dtype=float)
    day_of_week = (start.dayofweek + np.arange(days)) % 7

    rate = accounts["activity_rate"].to_numpy()[:, None]
    lam = rate * weekday_factor[day_of_week][None, :]

    # An account cannot transact before it exists (PLAN §4.8).
    created_day = (
        accounts["created_at"].to_numpy() - np.datetime64(start.to_datetime64(), "s")
    ).astype("timedelta64[s]").astype(np.int64) // 86400
    alive = np.arange(days)[None, :] >= created_day[:, None]
    counts = rng.poisson(np.where(alive, lam, 0.0))

    account_row, day_index = np.nonzero(counts)
    repeats = counts[account_row, day_index]
    return np.repeat(day_index, repeats), np.repeat(account_row, repeats)


def time_of_day_profile(spec: dict[str, Any], shift_hours: float) -> np.ndarray:
    """A 15-minute-resolution density over the day (PLAN §4.4 step 2)."""
    centres = (np.arange(_BINS_PER_DAY) + 0.5) * (24.0 / _BINS_PER_DAY)
    density = np.full(_BINS_PER_DAY, float(spec["uniform_weight"]) / 24.0)

    for hour, sigma, weight in zip(
        spec["peaks_hour"], spec["peaks_sigma"], spec["peaks_weight"], strict=True
    ):
        peak = (float(hour) + shift_hours) % 24.0
        # Wrap the distance so a shifted peak near midnight still covers both sides.
        distance = np.abs(centres - peak)
        distance = np.minimum(distance, 24.0 - distance)
        density += float(weight) * np.exp(-0.5 * (distance / float(sigma)) ** 2)

    low, high = spec["quiet_hours"]
    quiet = (centres >= float(low)) & (centres < float(high))
    density[quiet] *= float(spec["quiet_factor"])
    return density / density.sum()


def _draw_times(
    rng: np.random.Generator,
    config: dict[str, Any],
    accounts: pd.DataFrame,
    account_row: np.ndarray,
    day_index: np.ndarray,
) -> np.ndarray:
    """Seconds from the start of the window for every event."""
    spec = config["legit"]["diurnal"]
    profiles = {
        False: time_of_day_profile(spec, 0.0),
        True: time_of_day_profile(spec, float(config["legit"]["night_owl_shift_hours"])),
    }

    night_owl = accounts["is_night_owl"].to_numpy()[account_row]
    bins = np.empty(len(account_row), dtype=np.int64)
    for owl, profile in profiles.items():
        mask = night_owl == owl
        bins[mask] = rng.choice(_BINS_PER_DAY, size=int(mask.sum()), p=profile)

    bin_seconds = 86400 // _BINS_PER_DAY
    within = bins * bin_seconds + rng.integers(0, bin_seconds, len(bins))
    return day_index.astype(np.int64) * 86400 + within


# --- what is bought ---------------------------------------------------------


def _choose_merchants(
    rng: np.random.Generator,
    config: dict[str, Any],
    accounts: pd.DataFrame,
    merchants: pd.DataFrame,
    population: Population,
    account_row: np.ndarray,
) -> np.ndarray:
    """70% at the account's regulars, 30% exploring by popularity (PLAN §4.4 step 3)."""
    position = {account: index for index, account in enumerate(accounts["account_id"])}
    merchant_position = {m: i for i, m in enumerate(merchants["merchant_id"])}

    regulars = population.regular_merchants
    owner = regulars["account_id"].map(position).to_numpy()
    target = regulars["merchant_id"].map(merchant_position).to_numpy()

    order = np.argsort(owner, kind="stable")
    flat = target[order]
    counts = np.bincount(owner, minlength=len(accounts))
    offsets = np.concatenate([[0], np.cumsum(counts)])

    chosen = np.empty(len(account_row), dtype=np.int64)
    loyal = rng.random(len(account_row)) < float(config["accounts"]["regular_share"])

    # Loyal purchases: a uniform pick inside the account's own slice of the flat array.
    rows = account_row[loyal]
    picks = (rng.random(len(rows)) * counts[rows]).astype(np.int64)
    chosen[loyal] = flat[offsets[rows] + np.minimum(picks, counts[rows] - 1)]

    # Exploring: by popularity, among merchants the account can actually reach.
    home_city = accounts["home_city"].to_numpy()
    explore_rows = account_row[~loyal]
    explored = np.empty(len(explore_rows), dtype=np.int64)
    for city in np.unique(home_city[explore_rows]):
        mask = home_city[explore_rows] == city
        eligible, weights = reachable_merchants(merchants, str(city))
        explored[mask] = rng.choice(eligible, size=int(mask.sum()), p=weights)
    chosen[~loyal] = explored

    return chosen


def _assemble(
    rng: np.random.Generator,
    config: dict[str, Any],
    categories: list[dict[str, Any]],
    accounts: pd.DataFrame,
    merchants: pd.DataFrame,
    population: Population,
    account_row: np.ndarray,
    merchant_row: np.ndarray,
    event_time: np.ndarray,
) -> pd.DataFrame:
    """Amount, channel, device, IP, location and authorisation outcome."""
    by_name = {c["name"]: c for c in categories}
    category = merchants["category"].to_numpy()[merchant_row]

    median = np.array([float(by_name[c]["amount_median"]) for c in category])
    sigma = np.array([float(by_name[c]["amount_sigma"]) for c in category])
    spend = accounts["spend_level"].to_numpy()[account_row]
    amount = np.maximum(median * spend * np.exp(rng.normal(0.0, sigma)), _MIN_AMOUNT).round(2)

    # Channel comes from the category alone, so the realised mix matches categories.yaml.
    online_share = np.array([float(by_name[c]["online_share"]) for c in category])
    online = rng.random(len(category)) < online_share

    # A card-present event has to happen at a merchant with a storefront in the city the
    # account is in; otherwise it would fake a location jump.
    home_city = accounts["home_city"].to_numpy()[account_row]
    merchant_row, online = _ground_card_present(rng, merchants, merchant_row, online, home_city)
    category = merchants["category"].to_numpy()[merchant_row]

    device = _choose_devices(rng, accounts, population, account_row, event_time)
    ip_row = _choose_ips(rng, config, accounts, population, account_row, event_time, online)

    ips = population.ips
    lat = np.where(online, ips["lat"].to_numpy()[ip_row], merchants["lat"].to_numpy()[merchant_row])
    lon = np.where(online, ips["lon"].to_numpy()[ip_row], merchants["lon"].to_numpy()[merchant_row])
    city = np.where(
        online, ips["city"].to_numpy()[ip_row], merchants["city"].to_numpy()[merchant_row]
    )
    country = np.where(
        online, ips["country"].to_numpy()[ip_row], merchants["country"].to_numpy()[merchant_row]
    )

    return pd.DataFrame(
        {
            "event_time": event_time,
            "account_id": accounts["account_id"].to_numpy()[account_row],
            "merchant_id": merchants["merchant_id"].to_numpy()[merchant_row],
            "merchant_category": category,
            "amount": amount,
            "channel": np.where(online, "ONLINE", "POS"),
            "device_id": device,
            "ip": ips["ip"].to_numpy()[ip_row],
            "lat": lat,
            "lon": lon,
            "city": city,
            "country": country,
            "status": _draw_status(rng, config, amount, median),
        }
    )


def _ground_card_present(
    rng: np.random.Generator,
    merchants: pd.DataFrame,
    merchant_row: np.ndarray,
    online: np.ndarray,
    city: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Move POS events onto a merchant with a storefront in the account's own city.

    The replacement keeps the category wherever possible, so amounts and the channel mix
    are untouched. A city with no storefront at all in that category falls back to any
    local storefront, and a city with none at all leaves the event online.
    """
    storefront = ~merchants["is_online"].to_numpy()
    merchant_city = merchants["city"].to_numpy()
    merchant_category = merchants["category"].to_numpy()

    stranded = np.flatnonzero(~online & ~storefront[merchant_row])
    if not len(stranded):
        return merchant_row, online

    merchant_row = merchant_row.copy()
    online = online.copy()

    wanted = merchant_category[merchant_row[stranded]]
    for place in np.unique(city[stranded]):
        local = storefront & (merchant_city == place)
        local_any = np.flatnonzero(local)
        in_place = stranded[city[stranded] == place]

        if not len(local_any):
            online[in_place] = True
            continue

        for name in np.unique(wanted[city[stranded] == place]):
            rows = in_place[merchant_category[merchant_row[in_place]] == name]
            pool = np.flatnonzero(local & (merchant_category == name))
            pool = pool if len(pool) else local_any
            merchant_row[rows] = rng.choice(pool, size=len(rows))

    return merchant_row, online


def _draw_status(
    rng: np.random.Generator, config: dict[str, Any], amount: np.ndarray, median: np.ndarray
) -> np.ndarray:
    """1.5% declined, a little more often for large amounts (PLAN §4.4 step 6)."""
    spec = config["legit"]
    ratio = np.maximum(amount / np.maximum(median, _MIN_AMOUNT), 1e-9)
    rate = float(spec["decline_rate"]) * ratio ** float(spec["decline_amount_exponent"])
    declined = rng.random(len(amount)) < np.minimum(rate, float(spec["decline_rate_max"]))
    return np.where(declined, "DECLINED", "APPROVED")


# --- which device and which IP ----------------------------------------------


def _slot_table(
    frame: pd.DataFrame, accounts: pd.DataFrame, key: str, columns: list[str]
) -> dict[str, np.ndarray]:
    """Pack a ragged per-account mapping into fixed-width (accounts x slots) arrays."""
    position = {account: index for index, account in enumerate(accounts["account_id"])}
    row = frame[key].map(position).to_numpy()
    slot = frame.groupby(key).cumcount().to_numpy()
    width = int(slot.max()) + 1 if len(slot) else 1

    packed = {name: np.zeros((len(accounts), width), dtype=object) for name in columns}
    for name in columns:
        packed[name][row, slot] = frame[name].to_numpy()
    packed["used"] = np.zeros((len(accounts), width), dtype=bool)
    packed["used"][row, slot] = True
    return packed


def _choose_devices(
    rng: np.random.Generator,
    accounts: pd.DataFrame,
    population: Population,
    account_row: np.ndarray,
    event_time: np.ndarray,
) -> np.ndarray:
    """Sample a device that was valid at the moment of the event, by weight."""
    links = population.account_devices
    packed = _slot_table(
        links, accounts, "account_id", ["device_id", "valid_from", "valid_to", "weight"]
    )

    valid_from = packed["valid_from"][account_row].astype("datetime64[s]")
    valid_to = packed["valid_to"][account_row].astype("datetime64[s]")
    weight = packed["weight"][account_row].astype(float)
    live = (
        packed["used"][account_row]
        & (valid_from <= event_time[:, None])
        & (valid_to > event_time[:, None])
    )

    weight = np.where(live, weight, 0.0)
    total = weight.sum(axis=1, keepdims=True)
    # A brand-new account can have no device valid yet; fall back to its first one.
    weight = np.where(total > 0, weight, packed["used"][account_row].astype(float))
    weight /= weight.sum(axis=1, keepdims=True)

    picked = (weight.cumsum(axis=1) < rng.random(len(account_row))[:, None]).sum(axis=1)
    picked = np.minimum(picked, weight.shape[1] - 1)
    return packed["device_id"][account_row, picked].astype(str)


def _choose_ips(
    rng: np.random.Generator,
    config: dict[str, Any],
    accounts: pd.DataFrame,
    population: Population,
    account_row: np.ndarray,
    event_time: np.ndarray,
    online: np.ndarray,
) -> np.ndarray:
    """Pick the IP an event came from.

    Office use is gated here, not in population.py: the weekday and working-hours test
    needs the event's timestamp. Priority is office, then VPN, then carrier NAT, then
    the account's own home IP.
    """
    ip_index = {address: index for index, address in enumerate(population.ips["ip"])}
    links = population.account_ips
    position = {account: index for index, account in enumerate(accounts["account_id"])}

    slot_of = {name: index for index, name in enumerate(_IP_SLOTS)}
    address = np.full((len(accounts), len(_IP_SLOTS)), -1, dtype=np.int64)
    share = np.zeros((len(accounts), len(_IP_SLOTS)), dtype=float)

    rows = links["account_id"].map(position).to_numpy()
    slots = links["ip_type"].map(slot_of).to_numpy()
    address[rows, slots] = links["ip"].map(ip_index).to_numpy()
    share[rows, slots] = links["usage_share"].to_numpy()

    chosen = address[account_row, slot_of["home"]]

    hour = (event_time.astype("datetime64[s]").astype(np.int64) % 86400) / 3600.0
    weekday = ((event_time.astype("datetime64[D]").astype(np.int64) + 3) % 7) < 5
    low, high = config["ip_pools"]["office"]["active_hours"]
    at_work = weekday & (hour >= float(low)) & (hour < float(high))
    if not config["ip_pools"]["office"]["weekdays_only"]:
        at_work = (hour >= float(low)) & (hour < float(high))

    # Lowest priority first, so a later assignment wins.
    for name, extra in (("nat", None), ("vpn", None), ("office", at_work)):
        slot = slot_of[name]
        candidate = address[account_row, slot]
        eligible = (
            (candidate >= 0) & online & (rng.random(len(account_row)) < share[account_row, slot])
        )
        if extra is not None:
            eligible &= extra
        chosen = np.where(eligible, candidate, chosen)

    return chosen


# --- hard negatives ---------------------------------------------------------


def _apply_travel(
    rng: np.random.Generator,
    config: dict[str, Any],
    accounts: pd.DataFrame,
    merchants: pd.DataFrame,
    frame: pd.DataFrame,
) -> pd.DataFrame:
    """One trip per traveller, with realistic gaps around it (PLAN §4.4).

    The gap is enforced by dropping the events that would violate it, which is what
    makes this a hard negative rather than an impossible-travel case: the jump is real
    but the traveller plausibly spent hours getting there.
    """
    spec = config["hard_negatives"]["travel"]
    cities = load_cities()

    travellers = rng.permutation(len(accounts))[
        : round(len(accounts) * float(spec["account_share"]))
    ]
    if not len(travellers):
        return frame

    start = pd.Timestamp(config["start"])
    window = int(config["days"])
    account_ids = accounts["account_id"].to_numpy()

    by_account = {account: group for account, group in frame.groupby("account_id").indices.items()}
    times = frame["event_time"].to_numpy()
    drop = np.zeros(len(frame), dtype=bool)
    moved: list[tuple[np.ndarray, str]] = []

    for row in travellers:
        account = account_ids[row]
        rows = by_account.get(account)
        if rows is None or not len(rows):
            continue

        domestic = rng.random() < float(spec["domestic_share"])
        pool = cities[(cities["country"] == "IN") == domestic]
        pool = pool[pool["city"] != accounts["home_city"].to_numpy()[row]]
        destination = str(pool["city"].iloc[int(rng.integers(0, len(pool)))])

        length = int(rng.integers(int(spec["trip_days_min"]), int(spec["trip_days_max"]) + 1))
        begin = start + pd.Timedelta(days=int(rng.integers(0, max(window - length, 1))))
        finish = begin + pd.Timedelta(days=length)

        away = (times[rows] >= begin.to_datetime64()) & (times[rows] < finish.to_datetime64())
        if not away.any():
            continue

        gap = np.timedelta64(
            int(
                3600
                * float(
                    spec["min_gap_hours_domestic"]
                    if domestic
                    else spec["min_gap_hours_international"]
                )
            ),
            "s",
        )
        drop[rows] |= _gap_violations(times[rows], away, gap)
        moved.append((rows[away & ~_gap_violations(times[rows], away, gap)], destination))

    frame = frame.copy()
    for rows, destination in moved:
        _relocate(frame, rows, destination, merchants, cities, rng)

    return frame[~drop].reset_index(drop=True)


def _gap_violations(times: np.ndarray, away: np.ndarray, gap: np.timedelta64) -> np.ndarray:
    """Events too close to a boundary for the journey to have been possible.

    Walks in time order against the last event that was KEPT. Checking only the single
    crossing event was not enough: once it was dropped, the next event on the far side
    inherited the too-short gap and survived, which is how a 54-minute Mumbai-to-Delhi
    hop got through.
    """
    violated = np.zeros(len(times), dtype=bool)
    order = np.argsort(times, kind="stable")

    last_time: np.datetime64 | None = None
    last_side: bool | None = None
    for index in order:
        side = bool(away[index])
        if last_side is None:
            last_side, last_time = side, times[index]
            continue

        if side != last_side:
            if times[index] - last_time < gap:
                violated[index] = True
                continue
            last_side = side
        last_time = times[index]

    return violated


def _relocate(
    frame: pd.DataFrame,
    rows: np.ndarray,
    destination: str,
    merchants: pd.DataFrame,
    cities: pd.DataFrame,
    rng: np.random.Generator,
) -> None:
    """Move a traveller's card-present purchases to the city they are visiting."""
    if not len(rows):
        return

    place = cities.set_index("city").loc[destination]
    pos = frame.index[rows]
    present = frame.loc[pos, "channel"].to_numpy() == "POS"
    if not present.any():
        return

    local = np.flatnonzero(
        (merchants["city"].to_numpy() == destination) & ~merchants["is_online"].to_numpy()
    )
    if not len(local):
        return

    chosen = rng.choice(local, size=int(present.sum()))
    target = pos[present]
    frame.loc[target, "merchant_id"] = merchants["merchant_id"].to_numpy()[chosen]
    frame.loc[target, "merchant_category"] = merchants["category"].to_numpy()[chosen]
    frame.loc[target, "lat"] = merchants["lat"].to_numpy()[chosen]
    frame.loc[target, "lon"] = merchants["lon"].to_numpy()[chosen]
    frame.loc[target, "city"] = destination
    frame.loc[target, "country"] = place["country"]


def _inject_shopping_sprees(
    rng: np.random.Generator,
    config: dict[str, Any],
    categories: list[dict[str, Any]],
    accounts: pd.DataFrame,
    merchants: pd.DataFrame,
    frame: pd.DataFrame,
) -> pd.DataFrame:
    """4-8 extra purchases inside 90 minutes: legitimate velocity (PLAN §4.4)."""
    spec = config["hard_negatives"]["shopping_spree"]
    if frame.empty:
        return frame.iloc[:0]

    # "2% of account-days", not 2% of events: one spree per chosen day, not per event.
    day = frame["event_time"].to_numpy().astype("datetime64[D]")
    seeds = (
        frame.groupby([frame["account_id"].to_numpy(), day])
        .head(1)
        .sample(frac=float(spec["account_day_share"]), random_state=int(rng.integers(1 << 31)))
    )
    if seeds.empty:
        return frame.iloc[:0]

    by_name = {c["name"]: c for c in categories}
    home_city = accounts.set_index("account_id")["home_city"]
    rows: list[pd.DataFrame] = []
    for _, seed in seeds.iterrows():
        # The account's OWN city, not the seed event's. A seed that went out over a
        # carrier-NAT IP in another city would otherwise pull the spree's card-present
        # rows to that city and fake a location jump minutes later.
        where = str(home_city[seed["account_id"]])
        count = int(rng.integers(int(spec["extra_txns_min"]), int(spec["extra_txns_max"]) + 1))
        shops = int(rng.integers(int(spec["merchants_min"]), int(spec["merchants_max"]) + 1))
        local = np.flatnonzero(
            (merchants["city"].to_numpy() == where) | merchants["is_online"].to_numpy()
        )
        if not len(local):
            continue

        picked = rng.choice(local, size=min(shops, len(local)), replace=False)
        picked = picked[rng.integers(0, len(picked), count)]
        offsets = rng.integers(1, int(spec["window_minutes"]) * 60, count)
        rows.append(_rows_from_template(rng, seed, merchants, picked, offsets, by_name, where))

    return pd.concat(rows, ignore_index=True) if rows else frame.iloc[:0]


def _inject_micro_bursts(
    rng: np.random.Generator,
    config: dict[str, Any],
    categories: list[dict[str, Any]],
    accounts: pd.DataFrame,
    merchants: pd.DataFrame,
    population: Population,
    frame: pd.DataFrame,
    start: pd.Timestamp,
    days: int,
) -> pd.DataFrame:
    """5-12 sub-₹100 payments inside an hour at a regular merchant: legitimate card testing."""
    spec = config["hard_negatives"]["micro_burst"]
    if frame.empty:
        return frame.iloc[:0]

    by_name = {c["name"]: c for c in categories}
    home_city = accounts.set_index("account_id")["home_city"]
    chosen = rng.permutation(len(accounts))[: round(len(accounts) * float(spec["account_share"]))]
    seeds_by_account = frame.groupby("account_id").indices
    account_ids = accounts["account_id"].to_numpy()

    rows: list[pd.DataFrame] = []
    for row in chosen:
        available = seeds_by_account.get(account_ids[row])
        if available is None or not len(available):
            continue

        for _ in range(int(rng.integers(int(spec["episodes_min"]), int(spec["episodes_max"]) + 1))):
            seed = frame.iloc[int(rng.choice(available))]
            count = int(rng.integers(int(spec["txns_min"]), int(spec["txns_max"]) + 1))
            offsets = rng.integers(1, int(spec["window_minutes"]) * 60, count)
            merchant = np.full(
                count,
                int(np.flatnonzero(merchants["merchant_id"].to_numpy() == seed["merchant_id"])[0]),
            )
            burst = _rows_from_template(
                rng, seed, merchants, merchant, offsets, by_name, str(home_city[seed["account_id"]])
            )
            # The defining feature: every amount is tiny, from the account's own device.
            burst["amount"] = np.round(
                rng.uniform(_MIN_AMOUNT, float(spec["amount_max"]), count), 2
            )
            rows.append(burst)

    return pd.concat(rows, ignore_index=True) if rows else frame.iloc[:0]


def _rows_from_template(
    rng: np.random.Generator,
    seed: pd.Series,
    merchants: pd.DataFrame,
    merchant_rows: np.ndarray,
    offsets: np.ndarray,
    by_name: dict[str, dict[str, Any]],
    home_city: str,
) -> pd.DataFrame:
    """Extra events that reuse a real event's account, device and IP.

    Inheriting the IP keeps the burst on one connection, which is the point of the
    hard negative. It does mean a card-present row can carry a shared office IP; that
    is harmless, since a POS location comes from the merchant, and the account really
    does belong to that office cluster.
    """
    category = merchants["category"].to_numpy()[merchant_rows]
    median = np.array([float(by_name[c]["amount_median"]) for c in category])
    sigma = np.array([float(by_name[c]["amount_sigma"]) for c in category])
    amount = np.maximum(median * np.exp(rng.normal(0.0, sigma)), _MIN_AMOUNT).round(2)

    online_share = np.array([float(by_name[c]["online_share"]) for c in category])
    online = rng.random(len(category)) < online_share
    merchant_rows, online = _ground_card_present(
        rng, merchants, merchant_rows, online, np.full(len(online), home_city)
    )
    category = merchants["category"].to_numpy()[merchant_rows]

    return pd.DataFrame(
        {
            "event_time": seed["event_time"] + offsets.astype("timedelta64[s]"),
            "account_id": seed["account_id"],
            "merchant_id": merchants["merchant_id"].to_numpy()[merchant_rows],
            "merchant_category": category,
            "amount": amount,
            "channel": np.where(online, "ONLINE", "POS"),
            "device_id": seed["device_id"],
            "ip": seed["ip"],
            "lat": np.where(online, seed["lat"], merchants["lat"].to_numpy()[merchant_rows]),
            "lon": np.where(online, seed["lon"], merchants["lon"].to_numpy()[merchant_rows]),
            "city": np.where(online, seed["city"], merchants["city"].to_numpy()[merchant_rows]),
            "country": np.where(
                online, seed["country"], merchants["country"].to_numpy()[merchant_rows]
            ),
            "status": "APPROVED",
        }
    )
