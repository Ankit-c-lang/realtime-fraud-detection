"""Legitimate behaviour and the hard negatives (PLAN §4.4, §13).

Each hard negative imitates a signal one of the fraud patterns leaves. If any of them
stops appearing, the models get a shortcut and the evaluation stops meaning anything, so
these tests assert the negatives EXIST in workable numbers rather than pinning exact counts.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from fraud.config import load_yaml
from fraud.sim.legit import EVENT_COLUMNS, generate_legit
from fraud.sim.population import Population, build_population, spawn_streams

LABEL_COLUMNS = ("is_fraud", "fraud_type", "attack_id", "ring_id", "label_available_at")


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return load_yaml("sim_tiny")


@pytest.fixture(scope="module")
def categories() -> list[dict[str, Any]]:
    return load_yaml("categories")["categories"]


@pytest.fixture(scope="module")
def population(config: dict[str, Any], categories: list[dict[str, Any]]) -> Population:
    return build_population(config, categories)


@pytest.fixture(scope="module")
def events(
    config: dict[str, Any], categories: list[dict[str, Any]], population: Population
) -> pd.DataFrame:
    rng = spawn_streams(int(config["seed"]))["legit"]
    return generate_legit(config, categories, population, rng)


@pytest.fixture(scope="module")
def with_home(events: pd.DataFrame, population: Population) -> pd.DataFrame:
    return events.merge(population.accounts[["account_id", "home_city"]], on="account_id")


# --- schema and ordering (PLAN §3.5, §4.8) ----------------------------------


def test_columns_are_exactly_the_event_schema(events: pd.DataFrame) -> None:
    assert tuple(events.columns) == EVENT_COLUMNS


def test_no_label_columns_leak_into_events(events: pd.DataFrame) -> None:
    """Invariant 4: labels never enter events, the stream or the scorer."""
    assert not set(events.columns) & set(LABEL_COLUMNS)


def test_events_are_sorted_and_inside_the_window(
    events: pd.DataFrame, config: dict[str, Any]
) -> None:
    start = pd.Timestamp(config["start"])
    end = start + pd.Timedelta(days=int(config["days"]))

    assert events["event_time"].is_monotonic_increasing
    assert events["event_time"].min() >= start
    assert events["event_time"].max() < end


def test_no_event_precedes_its_account(events: pd.DataFrame, population: Population) -> None:
    joined = events.merge(population.accounts[["account_id", "created_at"]], on="account_id")
    assert (joined["event_time"] >= joined["created_at"]).all()


def test_amounts_are_positive_money(events: pd.DataFrame) -> None:
    assert (events["amount"] > 0).all()
    assert np.allclose(events["amount"], events["amount"].round(2))


def test_generation_is_deterministic(
    config: dict[str, Any], categories: list[dict[str, Any]], population: Population
) -> None:
    def build() -> pd.DataFrame:
        return generate_legit(
            config, categories, population, spawn_streams(int(config["seed"]))["legit"]
        )

    pd.testing.assert_frame_equal(build(), build())


# --- base behaviour (PLAN §4.4 steps 1-6) -----------------------------------


def test_channel_mix_comes_from_category_online_share(
    events: pd.DataFrame, categories: list[dict[str, Any]]
) -> None:
    """online_share is the ONLY thing that sets the channel (§4.4 step 5)."""
    share = {c["name"]: float(c["online_share"]) for c in categories}
    implied = events["merchant_category"].map(share).mean()
    realised = (events["channel"] == "ONLINE").mean()
    assert realised == pytest.approx(implied, abs=0.06)


def test_card_present_events_happen_at_a_local_storefront(
    with_home: pd.DataFrame, population: Population
) -> None:
    """Otherwise an ordinary shop trip would look like a location jump (§5.2)."""
    joined = with_home.merge(population.merchants[["merchant_id", "is_online"]], on="merchant_id")
    present = joined[joined["channel"] == "POS"]

    assert not present["is_online"].any(), "card present at a merchant with no storefront"
    # The remainder are travellers, who are a hard negative in their own right.
    assert (present["city"] == present["home_city"]).mean() > 0.95


def test_decline_rate_matches_config(events: pd.DataFrame, config: dict[str, Any]) -> None:
    declined = (events["status"] == "DECLINED").mean()
    assert declined == pytest.approx(float(config["legit"]["decline_rate"]), abs=0.01)


def test_large_amounts_are_declined_more_often(events: pd.DataFrame) -> None:
    """§4.4 step 6: "slightly more often for large amounts"."""
    big = events["amount"] > events["amount"].quantile(0.9)
    assert (events.loc[big, "status"] == "DECLINED").mean() > (
        events.loc[~big, "status"] == "DECLINED"
    ).mean()


def test_activity_follows_a_diurnal_profile(events: pd.DataFrame, config: dict[str, Any]) -> None:
    """Peaks near 13:00 and 20:00, with quiet small hours (§4.4 step 2)."""
    hour = events["event_time"].dt.hour
    share = hour.value_counts(normalize=True)

    low, high = config["legit"]["diurnal"]["quiet_hours"]
    quiet = share.reindex(range(int(low), int(high)), fill_value=0.0).sum()
    busy = share.reindex([12, 13, 19, 20], fill_value=0.0).sum()
    assert quiet < 0.02
    assert busy > 0.20


def test_night_owls_shift_later(
    events: pd.DataFrame, population: Population, config: dict[str, Any]
) -> None:
    joined = events.merge(population.accounts[["account_id", "is_night_owl"]], on="account_id")
    hour = joined["event_time"].dt.hour
    late = hour.between(0, 5) | hour.between(22, 23)

    owls = late[joined["is_night_owl"]].mean()
    rest = late[~joined["is_night_owl"]].mean()
    assert owls > rest


# --- hard negatives (PLAN §4.4 table) ---------------------------------------


def test_travel_produces_real_trips_with_believable_gaps(
    with_home: pd.DataFrame, config: dict[str, Any]
) -> None:
    """Location jumps that imitate ATO, but with time to have made the journey."""
    spec = config["hard_negatives"]["travel"]
    present = with_home[with_home["channel"] == "POS"].sort_values(
        ["account_id", "event_time"], kind="stable"
    )

    away = present[present["city"] != present["home_city"]]
    assert away["account_id"].nunique() >= 5, "nobody travelled"

    grouped = present.groupby("account_id")
    previous_city = grouped["city"].shift()
    previous_time = grouped["event_time"].shift()
    changed = present[previous_city.notna() & (previous_city != present["city"])]

    gaps = (changed["event_time"] - previous_time[changed.index]).dt.total_seconds() / 3600.0
    assert gaps.min() >= float(spec["min_gap_hours_domestic"])


def test_vpn_use_looks_like_impossible_travel(
    events: pd.DataFrame, population: Population, config: dict[str, Any]
) -> None:
    """A foreign IP on an ordinary account: the ATO signal, legitimately produced.

    Most VPN traffic stays on a device the account already owns, which is what makes
    the negative hard. A minority comes from another machine (sim-v2), because
    "foreign AND new device" being unique to account takeover is exactly what made the
    two classes perfectly separable (reports/sim_realism_review.md).
    """
    registry = population.ips[["ip", "ip_type", "country"]].rename(
        columns={"country": "ip_country"}
    )
    joined = events.merge(registry, on="ip")
    vpn = joined[joined["ip_type"] == "vpn"]

    assert len(vpn) > 0, "no VPN traffic at all"
    assert (vpn["ip_country"] != "IN").all()

    owned = population.account_devices.groupby("account_id")["device_id"].apply(set)
    on_own_device = [
        row["device_id"] in owned.get(row["account_id"], set()) for _, row in vpn.iterrows()
    ]
    assert sum(on_own_device) > 0, "every VPN session came from an unknown device"


def test_legitimate_devices_are_shared_across_accounts(
    events: pd.DataFrame, population: Population
) -> None:
    """The shared-device clusters a fraud ring has to be told apart from.

    Two populations provide this: households of 2-4, and the widely shared devices
    added in sim-v2 that reach into the 5-15 band a ring occupies. Without the second,
    a device count alone nearly identified a ring
    (reports/sim_realism_review.md).
    """
    per_device = events.groupby("device_id")["account_id"].nunique()
    shared = per_device[per_device > 1]
    assert len(shared) >= 5

    legitimate = set(
        population.devices.loc[
            population.devices["device_type"].isin(["family", "shared"]), "device_id"
        ]
    )
    assert set(shared.index) <= legitimate


def test_some_legitimate_devices_reach_the_ring_band(
    events: pd.DataFrame, population: Population, config: dict[str, Any]
) -> None:
    """sim-v2: legitimate accounts must exist at 5+ accounts per device.

    A ring puts 6-15 accounts on 2-4 devices. If no honest device ever reaches five,
    dev_accts_30d separates the classes by itself and the graph layer proves nothing.
    """
    floor = int(config["devices"]["shared_group_min"])
    per_device = events.groupby("device_id")["account_id"].nunique()

    assert (per_device >= floor).any(), "no legitimate device reaches the fraud-ring band"


def test_offices_share_an_ip_during_working_hours_only(
    events: pd.DataFrame, population: Population, config: dict[str, Any]
) -> None:
    """Mid-size shared-IP clusters, gated to weekday office hours by legit.py."""
    spec = config["ip_pools"]["office"]
    joined = events.merge(population.ips[["ip", "ip_type"]], on="ip")
    office = joined[joined["ip_type"] == "office"]

    assert len(office) > 0
    assert set(office["event_time"].dt.dayofweek) <= {0, 1, 2, 3, 4}
    # Injected sprees and bursts inherit their seed's IP, so a few carry the office
    # address on a card-present row. The base draw only ever puts a shared IP online.
    assert (office["channel"] == "ONLINE").mean() > 0.85

    # Sprees and bursts can run a little past closing, since they reuse the seed's IP.
    low, high = spec["active_hours"]
    inside = office["event_time"].dt.hour.between(int(low), int(high))
    assert inside.mean() > 0.90


def test_office_ip_use_is_not_all_or_nothing(events: pd.DataFrame, population: Population) -> None:
    """usage_share 0.5: affiliated accounts still use their home IP at work."""
    affiliated = population.account_ips[population.account_ips["ip_type"] == "office"]
    joined = (
        events[events["account_id"].isin(affiliated["account_id"])]
        .merge(population.ips[["ip", "ip_type"]], on="ip")
        .query("channel == 'ONLINE'")
    )
    mix = joined["ip_type"].value_counts(normalize=True)
    assert mix.get("office", 0.0) > 0.0
    assert mix.get("home", 0.0) > 0.0


def test_micro_payment_bursts_imitate_card_testing(
    events: pd.DataFrame, config: dict[str, Any]
) -> None:
    """Runs of tiny payments at one merchant from the account's own device."""
    spec = config["hard_negatives"]["micro_burst"]
    small = events[events["amount"] <= float(spec["amount_max"])]
    assert len(small) > 0

    window = pd.Timedelta(minutes=int(spec["window_minutes"]))
    found = 0
    for _, rows in small.groupby("account_id"):
        times = rows["event_time"].sort_values().to_numpy()
        for index in range(len(times)):
            within = (times >= times[index]) & (times < times[index] + window)
            if within.sum() >= int(spec["txns_min"]):
                found += 1
                break

    assert found >= 3, "no legitimate micro-payment bursts to confuse card-testing detection"


def test_shopping_sprees_imitate_velocity_abuse(
    events: pd.DataFrame, config: dict[str, Any]
) -> None:
    """Bursts of ordinary purchases at several merchants inside 90 minutes."""
    spec = config["hard_negatives"]["shopping_spree"]
    window = pd.Timedelta(minutes=int(spec["window_minutes"]))

    found = 0
    for _, rows in events.groupby("account_id"):
        ordered = rows.sort_values("event_time")
        times = ordered["event_time"].to_numpy()
        for index in range(len(times)):
            within = (times >= times[index]) & (times < times[index] + window)
            if within.sum() >= int(spec["extra_txns_min"]):
                found += 1
                break

    assert found >= 5, "no legitimate velocity bursts"


def test_new_legitimate_accounts_transact(
    events: pd.DataFrame, population: Population, config: dict[str, Any]
) -> None:
    """Young accounts must not be a free fraud signal for the ring pattern."""
    start = pd.Timestamp(config["start"])
    accounts = population.accounts
    newcomers = accounts.loc[accounts["created_at"] >= start, "account_id"]

    assert len(newcomers) > 0
    active = events[events["account_id"].isin(newcomers)]
    assert active["account_id"].nunique() >= len(newcomers) * 0.5


def test_low_friction_merchants_have_honest_customers(
    events: pd.DataFrame, population: Population
) -> None:
    """Card testing hides among real small digital purchases (§4.4)."""
    low_friction = population.merchants.loc[population.merchants["is_low_friction"], "merchant_id"]
    honest = events[events["merchant_id"].isin(low_friction)]

    assert len(honest) > 0
    assert (honest["status"] == "APPROVED").mean() > 0.9


def test_phone_upgrades_produce_a_new_device_mid_stream(
    events: pd.DataFrame, population: Population
) -> None:
    """A device the account has never used before is sometimes entirely innocent."""
    links = population.account_devices
    upgraders = links.groupby("account_id").filter(
        lambda rows: len(rows[rows["weight"] > 0.99]) > 1
    )
    assert not upgraders.empty

    used = events.groupby("account_id")["device_id"].nunique()
    assert (used.reindex(upgraders["account_id"].unique()).fillna(0) > 1).any()


@pytest.mark.slow
def test_full_config_volume_lands_on_target(categories: list[dict[str, Any]]) -> None:
    """The rescale has to absorb the hard negatives, not just the base draw (§4.3)."""
    config = load_yaml("sim")
    population = build_population(config, categories)
    events = generate_legit(
        config, categories, population, spawn_streams(int(config["seed"]))["legit"]
    )

    fraud = sum(int(p["target_transactions"]) for p in config["patterns"].values())
    target = int(config["target_transactions"])
    assert len(events) + fraud == pytest.approx(target, rel=float(config["target_tolerance"]))
