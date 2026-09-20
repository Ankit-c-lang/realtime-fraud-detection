"""The static population: accounts, merchants, devices and IP pools (PLAN §4.3).

Everything runs against sim_tiny.yaml so the suite stays fast. One slow-marked test
covers the full config, because a few properties (the ring-scale IP pools, the volume
rescale) only mean something at full size.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from fraud.config import load_yaml
from fraud.sim.population import (
    STREAMS,
    Population,
    _nearest_city_order,
    build_population,
    load_cities,
    spawn_streams,
)


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
def window(config: dict[str, Any]) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(config["start"])
    return start, start + pd.Timedelta(days=int(config["days"]))


# --- seeding and determinism (PLAN §4.8) ------------------------------------


def test_stream_order_is_pinned() -> None:
    """APPEND ONLY. Inserting a name shifts every later stream and changes the dataset."""
    assert STREAMS == (
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


def test_streams_are_independent() -> None:
    streams = spawn_streams(42)
    assert set(streams) == set(STREAMS)
    draws = {name: rng.random(64).tobytes() for name, rng in streams.items()}
    assert len(set(draws.values())) == len(STREAMS), "two streams produced identical draws"


def test_same_seed_reproduces_every_table(
    config: dict[str, Any], categories: list[dict[str, Any]]
) -> None:
    assert (
        build_population(config, categories).fingerprint()
        == build_population(config, categories).fingerprint()
    )


def test_a_different_seed_changes_the_population(
    config: dict[str, Any], categories: list[dict[str, Any]], population: Population
) -> None:
    other = build_population({**config, "seed": int(config["seed"]) + 1}, categories)
    assert other.fingerprint() != population.fingerprint()


# --- accounts (PLAN §4.3) ---------------------------------------------------


def test_account_count_and_ids(population: Population, config: dict[str, Any]) -> None:
    accounts = population.accounts
    assert len(accounts) == int(config["accounts"]["n"])
    assert accounts["account_id"].is_unique
    assert accounts["account_id"].str.match(r"^A\d{7}$").all()


def test_ten_percent_of_accounts_are_created_during_the_simulation(
    population: Population, config: dict[str, Any], window: tuple[pd.Timestamp, pd.Timestamp]
) -> None:
    """Otherwise "young account" is a perfect fraud predictor (PLAN §4.4)."""
    start, _ = window
    created = population.accounts["created_at"]
    existing = (created < start).mean()
    assert existing == pytest.approx(float(config["accounts"]["existing_share"]), abs=0.01)


def test_no_account_is_created_after_the_window_ends(
    population: Population, window: tuple[pd.Timestamp, pd.Timestamp]
) -> None:
    _, end = window
    assert (population.accounts["created_at"] < end).all()


def test_accounts_live_in_indian_cities(population: Population) -> None:
    cities = load_cities()
    domestic = set(cities.loc[cities["country"] == "IN", "city"])
    assert set(population.accounts["home_city"]) <= domestic


def test_account_coordinates_sit_near_their_city(population: Population) -> None:
    """Jitter spreads accounts around a centre; it must not move them to another city."""
    cities = load_cities().set_index("city")
    accounts = population.accounts
    centre = cities.loc[accounts["home_city"]]

    off_lat = (accounts["home_lat"].to_numpy() - centre["lat"].to_numpy()) * 111.0
    off_lon = (accounts["home_lon"].to_numpy() - centre["lon"].to_numpy()) * 111.0
    assert np.hypot(off_lat, off_lon).max() < 40.0


def test_spend_and_activity_are_positive(population: Population) -> None:
    assert (population.accounts["spend_level"] > 0).all()
    assert (population.accounts["activity_rate"] > 0).all()


def test_expected_volume_lands_on_target(
    population: Population, config: dict[str, Any], window: tuple[pd.Timestamp, pd.Timestamp]
) -> None:
    """Activity is rescaled so the legitimate stream hits target_transactions (§4.3).

    Fraud is injected on top, so the base is scaled to the target minus the pattern
    totals. Hard negatives add a little more, which target_tolerance absorbs.
    """
    start, end = window
    accounts = population.accounts
    active_days = (end - accounts["created_at"].clip(lower=start)).dt.total_seconds() / 86400.0
    weekday_mean = float(np.mean(config["legit"]["weekday_factor"]))
    expected = float((accounts["activity_rate"] * active_days).sum()) * weekday_mean

    fraud = sum(int(p["target_transactions"]) for p in config["patterns"].values())
    target = int(config["target_transactions"])
    assert expected + fraud == pytest.approx(target, rel=float(config["target_tolerance"]))


# --- merchants (PLAN §4.3) --------------------------------------------------


def test_merchant_count_and_categories(
    population: Population, config: dict[str, Any], categories: list[dict[str, Any]]
) -> None:
    merchants = population.merchants
    assert len(merchants) == int(config["merchants"]["n"])
    assert merchants["merchant_id"].is_unique
    assert set(merchants["category"]) <= {c["name"] for c in categories}


def test_popularity_is_a_zipf_distribution(population: Population) -> None:
    """A few hubs carry the volume, with a long tail for colluding merchants to hide in."""
    popularity = population.merchants["popularity"]
    assert popularity.sum() == pytest.approx(1.0)
    assert (popularity > 0).all()

    top_decile = popularity.nlargest(max(len(popularity) // 10, 1)).sum()
    assert top_decile > 0.5


def test_online_merchant_share_matches_its_own_knob(
    population: Population, config: dict[str, Any]
) -> None:
    """PLAN §4.3 wants ~35% of merchants to have no physical storefront."""
    share = population.merchants["is_online"].mean()
    assert share == pytest.approx(float(config["merchants"]["online_merchant_share"]), abs=0.05)


def test_online_merchant_flag_is_independent_of_category_online_share(
    population: Population, categories: list[dict[str, Any]]
) -> None:
    """The two are separate concerns and must not be derivable from each other.

    ``online_share`` is the per-transaction channel probability used by legit.py; the
    model feature is_online comes from the event channel (§5.2, feature 4). This column
    is merchant reference data (§4.2) and nothing more.
    """
    merchants = population.merchants
    online_share = merchants["category"].map(
        {c["name"]: float(c["online_share"]) for c in categories}
    )

    # A category-threshold rule would reproduce the flag exactly. None may.
    for threshold in (0.0, 0.5, 0.95, 1.0):
        assert not ((online_share >= threshold) == merchants["is_online"]).all()


def test_international_merchant_share(population: Population, config: dict[str, Any]) -> None:
    foreign = (population.merchants["country"] != "IN").mean()
    assert foreign == pytest.approx(float(config["merchants"]["international_share"]), abs=0.03)


def test_low_friction_merchants(population: Population, config: dict[str, Any]) -> None:
    """Card testing's targets, which real customers also use (PLAN §4.4)."""
    spec = config["merchants"]
    low_friction = population.merchants[population.merchants["is_low_friction"]]
    assert len(low_friction) == int(spec["n_low_friction"])
    assert set(low_friction["category"]) <= set(spec["low_friction_categories"])


def test_colluding_merchants_are_new_and_small(
    population: Population, config: dict[str, Any], window: tuple[pd.Timestamp, pd.Timestamp]
) -> None:
    """Created mid-simulation and drawn from the long tail, so they look ordinary."""
    start, end = window
    spec = config["merchants"]
    merchants = population.merchants
    colluding = merchants[merchants["is_colluding"]]

    assert len(colluding) == int(spec["n_colluding"])
    assert set(colluding["category"]) <= set(spec["colluding_categories"])
    assert ((colluding["created_at"] >= start) & (colluding["created_at"] < end)).all()
    assert (colluding["popularity"] <= merchants["popularity"].median()).all()


def test_non_colluding_merchants_predate_the_window(
    population: Population, window: tuple[pd.Timestamp, pd.Timestamp]
) -> None:
    start, _ = window
    ordinary = population.merchants[~population.merchants["is_colluding"]]
    assert (ordinary["created_at"] < start).all()


def test_regular_merchants_per_account(population: Population, config: dict[str, Any]) -> None:
    spec = config["accounts"]
    counts = population.regular_merchants.groupby("account_id").size()

    assert len(counts) == len(population.accounts)
    assert counts.max() <= int(spec["regular_merchants_max"])
    # Duplicate draws are dropped, so an account can fall just under the minimum.
    assert counts.min() >= 1
    assert counts.median() >= int(spec["regular_merchants_min"]) - 1


# --- devices (PLAN §4.3) ----------------------------------------------------


def test_every_account_owns_a_device(population: Population) -> None:
    assert population.account_devices["account_id"].nunique() == len(population.accounts)


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.75, 0.99])
def test_device_weights_sum_to_one_at_any_instant(
    population: Population, window: tuple[pd.Timestamp, pd.Timestamp], fraction: float
) -> None:
    """A phone upgrade is two non-overlapping windows, so totals only hold per instant."""
    start, end = window
    moment = start + (end - start) * fraction

    links = population.account_devices
    live = links[(links["valid_from"] <= moment) & (links["valid_to"] > moment)]
    totals = live.groupby("account_id")["weight"].sum()

    assert np.allclose(totals.to_numpy(), 1.0)


def test_upgrade_windows_do_not_overlap(population: Population) -> None:
    links = population.account_devices.sort_values(["account_id", "valid_from"])
    for _, rows in links.groupby("account_id"):
        personal = rows[rows["weight"] == pytest.approx(1.0)]
        if len(personal) < 2:
            continue
        starts = personal["valid_from"].to_numpy()
        stops = personal["valid_to"].to_numpy()
        assert (starts[1:] >= stops[:-1]).all(), "two full-weight devices overlap"


def test_devices_appear_only_after_their_owner_exists(population: Population) -> None:
    owned = population.account_devices.merge(
        population.accounts[["account_id", "created_at"]], on="account_id"
    )
    personal = owned[
        owned["device_id"].isin(
            population.devices.loc[population.devices["device_type"] == "personal", "device_id"]
        )
    ]
    assert (personal["valid_from"] >= personal["created_at"]).all()


def test_families_share_one_household_device(
    population: Population, config: dict[str, Any]
) -> None:
    """The legitimate shared-device cluster rings must be told apart from (PLAN §4.4)."""
    spec = config["devices"]
    accounts = population.accounts
    members = accounts[accounts["family_id"].notna()]

    assert members["family_id"].nunique() > 0
    sizes = members.groupby("family_id").size()
    assert sizes.min() >= int(spec["family_size_min"])
    assert sizes.max() <= int(spec["family_size_max"])

    household = population.devices.loc[population.devices["device_type"] == "family", "device_id"]
    shared = population.account_devices[population.account_devices["device_id"].isin(household)]
    per_device = shared.groupby("device_id")["account_id"].nunique()
    assert per_device.min() >= int(spec["family_size_min"])
    assert per_device.max() <= int(spec["family_size_max"])


# --- IP pools (PLAN §4.3) ---------------------------------------------------


def test_every_account_has_exactly_one_home_ip(population: Population) -> None:
    home = population.account_ips[population.account_ips["ip_type"] == "home"]
    assert len(home) == len(population.accounts)
    assert home["account_id"].is_unique


def test_families_share_a_home_ip(population: Population) -> None:
    accounts = population.accounts
    home = population.account_ips[population.account_ips["ip_type"] == "home"]
    joined = home.merge(accounts[["account_id", "family_id"]], on="account_id")
    families = joined[joined["family_id"].notna()]

    per_family = families.groupby("family_id")["ip"].nunique()
    assert (per_family == 1).all(), "a household must sit behind one home IP"


def test_ip_addresses_are_unique_across_pools(population: Population) -> None:
    assert population.ips["ip"].is_unique


def test_carrier_nat_is_a_small_pool_behind_many_accounts(
    population: Population, config: dict[str, Any]
) -> None:
    """Huge shared IPs the graph fan-out cap has to drop (PLAN §6.1)."""
    spec = config["ip_pools"]["carrier_nat"]
    links = population.account_ips[population.account_ips["ip_type"] == "nat"]

    assert links["ip"].nunique() == int(spec["n_ips"])
    share = links["account_id"].nunique() / len(population.accounts)
    assert share == pytest.approx(float(spec["account_share"]), abs=0.02)


def test_office_ip_use_is_probabilistic(population: Population, config: dict[str, Any]) -> None:
    """Affiliated accounts split weekday-daytime online activity with their home IP.

    population.py only carries the probability; legit.py applies the weekday and
    09:00-18:00 gate on top of it.
    """
    spec = config["ip_pools"]["office"]
    links = population.account_ips[population.account_ips["ip_type"] == "office"]

    assert np.allclose(links["usage_share"].to_numpy(), float(spec["usage_share"]))
    assert 0.0 < float(spec["usage_share"]) < 1.0, "office use must not be all-or-nothing"


def test_office_groups_stay_within_their_configured_size(
    population: Population, config: dict[str, Any]
) -> None:
    """Mid-size clusters the cap must NOT drop, unlike carrier NAT."""
    spec = config["ip_pools"]["office"]
    links = population.account_ips[population.account_ips["ip_type"] == "office"]
    sizes = links.groupby("ip")["account_id"].nunique()

    assert links["ip"].nunique() == int(spec["n_ips"])
    assert sizes.min() >= int(spec["group_size_min"])
    assert sizes.max() <= int(spec["group_size_max"])


def test_vpn_exit_nodes_are_foreign(population: Population, config: dict[str, Any]) -> None:
    """The legitimate impossible-travel hard negative (PLAN §4.4)."""
    spec = config["ip_pools"]["vpn"]
    registry = population.ips
    vpn = registry[registry["ip_type"] == "vpn"]

    assert len(vpn) == int(spec["n_ips"])
    assert (vpn["country"] != "IN").all()


def test_domestic_pools_stay_in_india(population: Population) -> None:
    registry = population.ips
    domestic = registry[registry["ip_type"].isin(["home", "nat", "office"])]
    assert (domestic["country"] == "IN").all()


def _shared_ip_membership(population: Population) -> pd.DataFrame:
    return (
        population.account_ips[population.account_ips["ip_type"].isin(["nat", "office"])]
        .merge(population.accounts[["account_id", "home_city"]], on="account_id")
        .merge(population.ips[["ip", "city"]].rename(columns={"city": "ip_city"}), on="ip")
    )


def test_shared_ips_sit_in_the_nearest_city_the_pool_covers(population: Population) -> None:
    """A shared IP in a random city would fake impossible travel for every member.

    Exact same-city placement is impossible whenever a pool has fewer IPs than there are
    cities, so the invariant is the nearest city that pool actually reaches. That holds
    at any scale; the raw same-city rate does not.
    """
    domestic = load_cities()
    domestic = domestic[domestic["country"] == "IN"].reset_index(drop=True)
    nearest = _nearest_city_order(domestic)
    joined = _shared_ip_membership(population)

    for ip_type in ("nat", "office"):
        pool = joined[joined["ip_type"] == ip_type]
        covered = set(pool["ip_city"])
        best = {
            home: next(city for city in nearest[home] if city in covered)
            for home in pool["home_city"].unique()
        }
        assert (pool["ip_city"] == pool["home_city"].map(best)).all(), ip_type


# --- integrity --------------------------------------------------------------


def test_every_reference_resolves(population: Population) -> None:
    accounts = set(population.accounts["account_id"])
    assert set(population.account_devices["account_id"]) <= accounts
    assert set(population.account_ips["account_id"]) <= accounts
    assert set(population.regular_merchants["account_id"]) <= accounts
    assert set(population.account_devices["device_id"]) <= set(population.devices["device_id"])
    assert set(population.account_ips["ip"]) <= set(population.ips["ip"])
    assert set(population.regular_merchants["merchant_id"]) <= set(
        population.merchants["merchant_id"]
    )


def test_no_missing_values_where_they_would_break_generation(population: Population) -> None:
    for frame, optional in (
        (population.accounts, {"family_id"}),
        (population.merchants, set()),
        (population.devices, set()),
        (population.account_devices, set()),
        (population.ips, set()),
        (population.account_ips, set()),
        (population.regular_merchants, set()),
    ):
        required = [c for c in frame.columns if c not in optional]
        assert not frame[required].isna().any().any()


@pytest.mark.slow
def test_full_config_builds_to_scale(categories: list[dict[str, Any]]) -> None:
    """A few properties only mean anything at 22,000 accounts."""
    config = load_yaml("sim")
    population = build_population(config, categories)

    assert len(population.accounts) == 22000
    assert len(population.merchants) == 2000

    per_ip = (
        population.account_ips[population.account_ips["ip_type"] == "nat"]
        .groupby("ip")["account_id"]
        .nunique()
    )
    office = (
        population.account_ips[population.account_ips["ip_type"] == "office"]
        .groupby("ip")["account_id"]
        .nunique()
    )
    # The fan-out cap in Phase 4 has to separate these two populations.
    assert per_ip.median() > office.max()

    # With 40 NAT and 60 office IPs there are enough to cover every city directly.
    joined = _shared_ip_membership(population)
    assert (joined["home_city"] == joined["ip_city"]).mean() > 0.90
