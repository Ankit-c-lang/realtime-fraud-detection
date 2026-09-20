"""The four injected fraud patterns (PLAN §4.5, §4.8, §13).

Each pattern has to leave the signature the feature set is built to catch, and the ring
quotas have to hold or the ring half of the evaluation measures nothing.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from fraud.config import load_yaml
from fraud.sim.legit import EVENT_COLUMNS, generate_legit
from fraud.sim.patterns import ATTACK_COLUMNS, FRAUD_TYPES, Attacks, inject_patterns
from fraud.sim.population import Population, build_population, spawn_streams

EARTH_RADIUS_KM = 6371.0


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
def legit(
    config: dict[str, Any], categories: list[dict[str, Any]], population: Population
) -> pd.DataFrame:
    return generate_legit(
        config, categories, population, spawn_streams(int(config["seed"]))["legit"]
    )


@pytest.fixture(scope="module")
def attacks(
    config: dict[str, Any],
    categories: list[dict[str, Any]],
    population: Population,
    legit: pd.DataFrame,
) -> Attacks:
    return inject_patterns(
        config, categories, population, legit, spawn_streams(int(config["seed"]))
    )


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = (np.radians(v) for v in (lat1, lon1, lat2, lon2))
    inner = (
        np.sin((lat2 - lat1) / 2.0) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2.0) ** 2
    )
    return float(EARTH_RADIUS_KM * 2.0 * np.arcsin(np.sqrt(np.clip(inner, 0.0, 1.0))))


# --- shape and labelling (PLAN §4.2, §4.6) ----------------------------------


def test_every_pattern_is_present(attacks: Attacks) -> None:
    assert set(attacks.events["fraud_type"]) == set(FRAUD_TYPES)


def test_columns_are_the_event_schema_plus_labels(attacks: Attacks) -> None:
    assert tuple(attacks.events.columns) == ATTACK_COLUMNS
    assert set(EVENT_COLUMNS) < set(ATTACK_COLUMNS)


def test_every_fraud_event_carries_an_attack_id(attacks: Attacks) -> None:
    assert attacks.events["attack_id"].notna().all()


def test_ring_id_is_set_for_rings_and_nothing_else(attacks: Attacks) -> None:
    """PLAN §4.5: ring_id equals attack_id for rings, and is null elsewhere."""
    events = attacks.events
    rings = events[events["fraud_type"] == "RING"]
    others = events[events["fraud_type"] != "RING"]

    assert rings["ring_id"].notna().all()
    assert (rings["ring_id"] == rings["attack_id"]).all()
    assert others["ring_id"].isna().all()


def test_events_are_sorted_and_inside_the_window(attacks: Attacks, config: dict[str, Any]) -> None:
    start = pd.Timestamp(config["start"])
    end = start + pd.Timedelta(days=int(config["days"]))
    times = attacks.events["event_time"]

    assert times.is_monotonic_increasing
    assert times.min() >= start
    assert times.max() < end


def test_injection_is_deterministic(
    config: dict[str, Any],
    categories: list[dict[str, Any]],
    population: Population,
    legit: pd.DataFrame,
) -> None:
    def run() -> pd.DataFrame:
        return inject_patterns(
            config, categories, population, legit, spawn_streams(int(config["seed"]))
        ).events

    pd.testing.assert_frame_equal(run(), run())


def test_new_actors_match_the_population_schema(attacks: Attacks, population: Population) -> None:
    """Ring mules are appended to the real tables, so the columns have to line up."""
    assert list(attacks.accounts.columns) == list(population.accounts.columns)
    assert list(attacks.devices.columns) == list(population.devices.columns)
    assert list(attacks.ips.columns) == list(population.ips.columns)
    assert not set(attacks.accounts["account_id"]) & set(population.accounts["account_id"])
    assert not set(attacks.devices["device_id"]) & set(population.devices["device_id"])
    assert not set(attacks.ips["ip"]) & set(population.ips["ip"])


# --- VELOCITY (PLAN §4.5) ---------------------------------------------------


def test_velocity_drains_a_card_inside_minutes(attacks: Attacks, config: dict[str, Any]) -> None:
    spec = config["patterns"]["velocity"]
    events = attacks.events[attacks.events["fraud_type"] == "VELOCITY"]

    for _, burst in events.groupby("attack_id"):
        span = (burst["event_time"].max() - burst["event_time"].min()).total_seconds() / 60.0
        assert len(burst) <= int(spec["txns_max"])
        assert span <= float(spec["window_minutes_max"])
        assert burst["account_id"].nunique() == 1, "one victim per velocity attack"

    assert (events["amount"] <= float(spec["amount_cap"])).all()
    assert set(events["merchant_category"]) <= set(spec["categories"])


# --- ATO (PLAN §4.5, §4.8) --------------------------------------------------


def test_ato_follows_a_genuine_purchase_at_home(
    attacks: Attacks, legit: pd.DataFrame, config: dict[str, Any]
) -> None:
    """The signature: a real card-present purchase minutes before the takeover."""
    spec = config["patterns"]["ato"]
    events = attacks.events[attacks.events["fraud_type"] == "ATO"]
    at_home = legit[legit["channel"] == "POS"]

    checked = 0
    for _, attack in events.groupby("attack_id"):
        first = attack.sort_values("event_time").iloc[0]
        earlier = at_home[
            (at_home["account_id"] == first["account_id"])
            & (at_home["event_time"] < first["event_time"])
        ]
        assert not earlier.empty, "no legitimate purchase precedes the takeover"

        lag = (first["event_time"] - earlier["event_time"].max()).total_seconds() / 60.0
        assert lag <= float(spec["lag_after_legit_minutes_max"]) + 1.0
        checked += 1

    assert checked > 0


def test_ato_produces_impossible_travel(attacks: Attacks, legit: pd.DataFrame) -> None:
    """PLAN §4.8: at least 90% of attacks exceed 900 km/h at the first fraud event."""
    events = attacks.events[attacks.events["fraud_type"] == "ATO"]
    at_home = legit[legit["channel"] == "POS"]

    speeds: list[float] = []
    for _, attack in events.groupby("attack_id"):
        first = attack.sort_values("event_time").iloc[0]
        earlier = at_home[
            (at_home["account_id"] == first["account_id"])
            & (at_home["event_time"] < first["event_time"])
        ]
        if earlier.empty:
            continue

        previous = earlier.loc[earlier["event_time"].idxmax()]
        km = _haversine(previous["lat"], previous["lon"], first["lat"], first["lon"])
        hours = max(
            (first["event_time"] - previous["event_time"]).total_seconds() / 3600.0, 1.0 / 60.0
        )
        speeds.append(km / hours)

    assert speeds
    assert np.mean(np.array(speeds) > 900.0) >= 0.9


def test_ato_uses_a_device_the_victim_has_never_used(
    attacks: Attacks, population: Population
) -> None:
    owned = population.account_devices.groupby("account_id")["device_id"].apply(set)
    events = attacks.events[attacks.events["fraud_type"] == "ATO"]

    for _, row in events.iterrows():
        assert row["device_id"] not in owned.get(row["account_id"], set())


# --- CARD TESTING (PLAN §4.5, §4.8) -----------------------------------------


def test_card_testing_touches_many_accounts_from_one_device(
    attacks: Attacks, config: dict[str, Any]
) -> None:
    """PLAN §4.8: every attack device sees >= 20 distinct accounts within 60 minutes."""
    spec = config["patterns"]["card_testing"]
    events = attacks.events[attacks.events["fraud_type"] == "CARD_TESTING"]
    window = pd.Timedelta(minutes=60)

    for _, attack in events.groupby("attack_id"):
        probes = attack[attack["amount"] <= float(spec["amount_max"])]
        start = probes["event_time"].min()
        inside = probes[probes["event_time"] < start + window]
        assert inside["account_id"].nunique() >= int(spec["victims_min"])
        assert attack["device_id"].nunique() == 1, "one device per card-testing attack"


def test_card_testing_probes_are_tiny_and_often_declined(
    attacks: Attacks, config: dict[str, Any]
) -> None:
    spec = config["patterns"]["card_testing"]
    events = attacks.events[attacks.events["fraud_type"] == "CARD_TESTING"]
    probes = events[events["amount"] <= float(spec["amount_max"])]

    assert (probes["amount"] >= float(spec["amount_min"])).all()
    # Wide tolerance on purpose: sim_tiny runs only a couple of attacks, so a few dozen
    # probes carry real sampling noise. What matters is that this looks nothing like the
    # 1.5% decline rate of ordinary traffic.
    declined = (probes["status"] == "DECLINED").mean()
    assert (
        float(spec["decline_rate_min"]) - 0.15 <= declined <= float(spec["decline_rate_max"]) + 0.15
    )


def test_card_testing_cashes_out_on_the_cards_that_worked(
    attacks: Attacks, config: dict[str, Any]
) -> None:
    """Large purchases hours later, on the same device, at cash-out merchants."""
    spec = config["patterns"]["card_testing"]
    events = attacks.events[attacks.events["fraud_type"] == "CARD_TESTING"]
    cashouts = events[events["amount"] >= float(spec["cashout_amount_min"])]

    assert len(cashouts) > 0, "no cash-out stage at all"
    assert set(cashouts["merchant_category"]) <= set(spec["cashout_categories"])

    for attack_id, rows in cashouts.groupby("attack_id"):
        probes = events[(events["attack_id"] == attack_id) & (events["amount"] <= 50.0)]
        gap = (rows["event_time"].min() - probes["event_time"].min()).total_seconds() / 3600.0
        assert gap >= float(spec["cashout_delay_hours_min"]) - 0.5


# --- RING (PLAN §4.5) -------------------------------------------------------


@pytest.fixture(scope="module")
def ring_spans(attacks: Attacks) -> pd.DataFrame:
    rings = attacks.events[attacks.events["fraud_type"] == "RING"]
    return rings.groupby("ring_id")["event_time"].agg(["min", "max"])


def test_rings_share_devices_and_ips_across_accounts(
    attacks: Attacks, config: dict[str, Any]
) -> None:
    """The only thing that gives a ring away, and the reason §6 exists."""
    spec = config["patterns"]["ring"]
    rings = attacks.events[attacks.events["fraud_type"] == "RING"]

    for _, members in rings.groupby("ring_id"):
        assert members["account_id"].nunique() >= int(spec["accounts_min"])
        assert members["account_id"].nunique() <= int(spec["accounts_max"])
        assert members["device_id"].nunique() <= int(spec["devices_max"])
        assert members["ip"].nunique() <= int(spec["ips_max"])
        # More accounts than devices is exactly what the graph projection keys on.
        assert members["account_id"].nunique() > members["device_id"].nunique()


def test_ring_accounts_are_created_shortly_before_the_ring_starts(
    attacks: Attacks, ring_spans: pd.DataFrame, config: dict[str, Any]
) -> None:
    spec = config["patterns"]["ring"]
    start = pd.Timestamp(config["start"])
    rings = attacks.events[attacks.events["fraud_type"] == "RING"]
    created = attacks.accounts.set_index("account_id")["created_at"]

    for ring_id, members in rings.groupby("ring_id"):
        begin = ring_spans.loc[ring_id, "min"]
        ages = (begin - pd.to_datetime(created[members["account_id"].unique()])).dt.days
        # Clipped at the simulation start, so only the upper bound is guaranteed.
        assert ages.max() <= int(spec["account_age_days_max"]) + int(spec["active_days_max"])
        assert (pd.to_datetime(created[members["account_id"].unique()]) >= start).all()


def test_ring_transactions_look_ordinary(
    attacks: Attacks, legit: pd.DataFrame, config: dict[str, Any]
) -> None:
    """PLAN §4.5: amounts, hours and approval rates must NOT stand out on their own."""
    rings = attacks.events[attacks.events["fraud_type"] == "RING"]

    declined = (rings["status"] == "DECLINED").mean()
    assert declined == pytest.approx(float(config["legit"]["decline_rate"]), abs=0.02), (
        "ring approval rate gives the pattern away"
    )

    night = rings["event_time"].dt.hour.between(1, 5).mean()
    assert night < 0.10, "rings transacting at odd hours would be trivially detectable"

    assert rings["amount"].median() < legit["amount"].median() * 3.0


def test_ring_quotas_hold(ring_spans: pd.DataFrame, config: dict[str, Any]) -> None:
    """Without these the ring evaluation measures nothing (PLAN §4.5)."""
    spec = config["patterns"]["ring"]
    splits = load_yaml("splits")["splits"]

    test_start = pd.Timestamp(splits["test"]["start"])
    test_end = pd.Timestamp(splits["test"]["end"])
    train_start = pd.Timestamp(splits["train"]["start"])
    train_end = pd.Timestamp(splits["train"]["end"])

    in_test = ring_spans[(ring_spans["min"] >= test_start) & (ring_spans["min"] < test_end)]
    in_train = ring_spans[(ring_spans["min"] >= train_start) & (ring_spans["max"] < train_end)]

    assert len(in_test) >= int(spec["min_rings_in_test"])
    assert len(in_train) >= int(spec["min_rings_fully_in_train"])


def test_some_rings_recycle_a_device_from_an_earlier_ring(
    attacks: Attacks, ring_spans: pd.DataFrame, config: dict[str, Any]
) -> None:
    """Device reuse is what gives personalised PageRank something to propagate (§6.3)."""
    spec = config["patterns"]["ring"]
    rings = attacks.events[attacks.events["fraud_type"] == "RING"]
    devices = rings.groupby("ring_id")["device_id"].unique()

    order = ring_spans.sort_values("min").index
    seen: dict[str, str] = {}
    reusers: list[str] = []
    for ring_id in order:
        if any(device in seen for device in devices[ring_id]):
            reusers.append(ring_id)
        for device in devices[ring_id]:
            seen.setdefault(device, ring_id)

    assert reusers, "no ring reuses a device"
    if int(spec["min_rings_in_test_with_device_reuse"]) > 0:
        splits = load_yaml("splits")["splits"]
        test_start = pd.Timestamp(splits["test"]["start"])
        in_test = ring_spans[ring_spans["min"] >= test_start].index
        assert len(set(reusers) & set(in_test)) >= int(spec["min_rings_in_test_with_device_reuse"])


# --- volume (PLAN §4.1, §4.8) -----------------------------------------------


def test_fraud_prevalence_is_in_band(attacks: Attacks, legit: pd.DataFrame) -> None:
    total = len(attacks.events) + len(legit)
    assert 0.010 <= len(attacks.events) / total <= 0.020


@pytest.mark.parametrize("pattern", ["velocity", "ato", "card_testing", "ring"])
def test_pattern_volume_is_within_thirty_percent_of_target(
    attacks: Attacks, config: dict[str, Any], pattern: str
) -> None:
    """PLAN §4.8: every pattern within ±30% of its configured target."""
    target = int(config["patterns"][pattern]["target_transactions"])
    produced = int((attacks.events["fraud_type"] == pattern.upper()).sum())
    assert abs(produced / target - 1.0) <= 0.30


def test_ring_target_agrees_with_the_ring_structure(config: dict[str, Any]) -> None:
    """The configured ring total must stay consistent with what the structure produces.

    PLAN §4.5 states ~3,000 but its own structure (~45 rings x 6-15 accounts x 6-12
    transactions each) multiplies out to ~4,250. The config records the corrected figure;
    this keeps the two from drifting apart again if a structural range is ever edited.
    """
    spec = config["patterns"]["ring"]
    implied = (
        int(spec["n_rings"])
        * (int(spec["accounts_min"]) + int(spec["accounts_max"]))
        / 2.0
        * (int(spec["txns_per_account_min"]) + int(spec["txns_per_account_max"]))
        / 2.0
    )
    assert abs(int(spec["target_transactions"]) / implied - 1.0) <= 0.10


@pytest.mark.slow
def test_full_config_meets_every_quota(categories: list[dict[str, Any]]) -> None:
    """The quotas only bite at full scale, where sim_tiny's thresholds are relaxed."""
    config = load_yaml("sim")
    population = build_population(config, categories)
    rngs = spawn_streams(int(config["seed"]))
    legit = generate_legit(config, categories, population, rngs["legit"])
    attacks = inject_patterns(config, categories, population, legit, rngs)

    spec = config["patterns"]["ring"]
    splits = load_yaml("splits")["splits"]
    rings = attacks.events[attacks.events["fraud_type"] == "RING"]
    spans = rings.groupby("ring_id")["event_time"].agg(["min", "max"])

    in_test = spans[
        (spans["min"] >= pd.Timestamp(splits["test"]["start"]))
        & (spans["min"] < pd.Timestamp(splits["test"]["end"]))
    ]
    in_train = spans[
        (spans["min"] >= pd.Timestamp(splits["train"]["start"]))
        & (spans["max"] < pd.Timestamp(splits["train"]["end"]))
    ]

    assert len(in_test) >= int(spec["min_rings_in_test"])
    assert len(in_train) >= int(spec["min_rings_fully_in_train"])

    total = len(attacks.events) + len(legit)
    assert 0.010 <= len(attacks.events) / total <= 0.020
