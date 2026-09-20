"""The simulator configs are frozen at the end of Phase 1, so they check themselves.

These assert the structural promises PLAN §4.3-§4.8 makes about configs/, not the
behaviour of any generator: they run before sim code exists and must keep passing after.
"""

from __future__ import annotations

import csv
import datetime as dt
from typing import Any

import pytest

from fraud.config import CONFIG_DIR, load_yaml

SPLIT_ORDER = ("burn_in", "train", "early_stop", "valid", "test")
SIM_CONFIGS = ("sim", "sim_tiny")


@pytest.fixture(scope="module")
def cities() -> list[dict[str, str]]:
    with (CONFIG_DIR / "cities.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _flat_keys(node: dict[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in node.items():
        keys.add(prefix + key)
        if isinstance(value, dict):
            keys |= _flat_keys(value, f"{prefix}{key}.")
    return keys


# --- splits (PLAN §4.7) -----------------------------------------------------


def test_splits_are_contiguous_and_half_open() -> None:
    """Half-open [start, end) intervals that touch exactly, so no event lands in two."""
    splits = load_yaml("splits")["splits"]
    previous_end: dt.date | None = None

    for name in SPLIT_ORDER:
        start, end = splits[name]["start"], splits[name]["end"]
        assert isinstance(start, dt.date) and isinstance(end, dt.date)
        assert start < end, f"{name} is empty or inverted"
        if previous_end is not None:
            assert start == previous_end, f"gap or overlap before {name}"
        previous_end = end


def test_splits_cover_exactly_the_simulated_window() -> None:
    config = load_yaml("splits")
    sim = load_yaml("sim")
    splits = config["splits"]

    first = splits["burn_in"]["start"]
    last = splits["test"]["end"]
    assert first == sim["start"]
    assert (last - first).days == sim["days"]


def test_checkpoint_is_the_test_boundary() -> None:
    """The live replay starts from state captured at test_start (PLAN §5.5, §9.5)."""
    config = load_yaml("splits")
    assert config["checkpoint_at"] == config["splits"]["test"]["start"]


# --- categories (PLAN §4.3) -------------------------------------------------


def test_categories_are_the_ten_fixed_levels_in_order() -> None:
    """This order fixes the categorical codes; reordering invalidates trained models."""
    expected = [
        "grocery",
        "fuel",
        "restaurants",
        "pharmacy",
        "utilities",
        "ecommerce",
        "electronics",
        "travel",
        "digital_goods",
        "gift_cards_wallet",
    ]
    names = [c["name"] for c in load_yaml("categories")["categories"]]
    assert names == expected


def test_category_distributions_are_usable() -> None:
    for category in load_yaml("categories")["categories"]:
        assert category["amount_median"] > 0, category["name"]
        assert category["amount_sigma"] > 0, category["name"]
        assert 0.0 <= category["online_share"] <= 1.0, category["name"]


def test_merchant_shares_sum_to_one() -> None:
    total = sum(c["merchant_share"] for c in load_yaml("categories")["categories"])
    assert total == pytest.approx(1.0)


# --- cities (PLAN §4.3) -----------------------------------------------------


def test_city_counts(cities: list[dict[str, str]]) -> None:
    domestic = [c for c in cities if c["country"] == "IN"]
    international = [c for c in cities if c["country"] != "IN"]
    assert len(domestic) == 15
    assert len(international) == 8


def test_city_names_are_unique(cities: list[dict[str, str]]) -> None:
    names = [c["city"] for c in cities]
    assert len(set(names)) == len(names)


def test_city_coordinates_are_valid(cities: list[dict[str, str]]) -> None:
    for city in cities:
        assert -90.0 <= float(city["lat"]) <= 90.0, city["city"]
        assert -180.0 <= float(city["lon"]) <= 180.0, city["city"]


def test_city_weights_sum_to_one_within_each_country_group(
    cities: list[dict[str, str]],
) -> None:
    """Weights are relative within a group, so domestic and foreign draws both normalise."""
    for domestic in (True, False):
        group = [c for c in cities if (c["country"] == "IN") is domestic]
        assert sum(float(c["weight"]) for c in group) == pytest.approx(1.0)


# --- simulator configs (PLAN §4.3-§4.6) -------------------------------------


def test_tiny_config_mirrors_the_full_one() -> None:
    """Same knobs in both files, so one cannot gain a setting the other never gets."""
    assert _flat_keys(load_yaml("sim")) == _flat_keys(load_yaml("sim_tiny"))


@pytest.mark.parametrize("name", SIM_CONFIGS)
def test_fraud_prevalence_target_is_in_band(name: str) -> None:
    """PLAN §4.1 wants 1.2-1.8%; §4.8 fails the run outside [1.0%, 2.0%]."""
    config = load_yaml(name)
    fraud = sum(p["target_transactions"] for p in config["patterns"].values())
    assert 0.010 <= fraud / config["target_transactions"] <= 0.020


@pytest.mark.parametrize("name", SIM_CONFIGS)
def test_all_four_patterns_are_present(name: str) -> None:
    patterns = load_yaml(name)["patterns"]
    assert set(patterns) == {"velocity", "ato", "card_testing", "ring"}
    for pattern in patterns.values():
        assert pattern["target_transactions"] > 0


@pytest.mark.parametrize("name", SIM_CONFIGS)
def test_every_referenced_category_exists(name: str) -> None:
    """A typo in a category list would otherwise surface as an empty merchant pool."""
    known = {c["name"] for c in load_yaml("categories")["categories"]}
    config = load_yaml(name)

    referenced = set(config["merchants"]["low_friction_categories"])
    referenced |= set(config["merchants"]["colluding_categories"])
    for key in ("velocity", "ato"):
        referenced |= set(config["patterns"][key]["categories"])
    referenced |= set(config["patterns"]["card_testing"]["cashout_categories"])

    assert referenced <= known, f"unknown categories: {sorted(referenced - known)}"


@pytest.mark.parametrize("name", SIM_CONFIGS)
def test_merchant_online_share_targets_the_plan_value(name: str) -> None:
    """PLAN §4.3 asks for ~35% online merchants, as its own knob (not derived)."""
    share = load_yaml(name)["merchants"]["online_merchant_share"]
    assert 0.30 <= share <= 0.40


@pytest.mark.parametrize("name", SIM_CONFIGS)
def test_office_usage_share_is_probabilistic(name: str) -> None:
    """A simulator design choice, not a PLAN value: office use must not be certain."""
    assert 0.0 < load_yaml(name)["ip_pools"]["office"]["usage_share"] < 1.0


@pytest.mark.parametrize("name", SIM_CONFIGS)
def test_label_delay_is_fourteen_days(name: str) -> None:
    """The graph job may only seed from labels available before the snapshot (PLAN §4.6)."""
    assert load_yaml(name)["labels"]["delay_days"] == 14


@pytest.mark.parametrize("name", SIM_CONFIGS)
def test_min_and_max_pairs_are_ordered(name: str) -> None:
    """Catches a swapped pair, which numpy would only report much later."""
    config = load_yaml(name)
    checked = 0

    def walk(node: dict[str, Any], path: str) -> None:
        nonlocal checked
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value, f"{path}{key}.")
            elif key.endswith("_min"):
                upper = node.get(f"{key[: -len('_min')]}_max")
                if upper is not None:
                    assert value <= upper, f"{path}{key} > its _max"
                    checked += 1

    walk(config, "")
    assert checked > 10, "expected many min/max pairs to verify"


@pytest.mark.parametrize("name", SIM_CONFIGS)
def test_shares_are_probabilities(name: str) -> None:
    def walk(node: dict[str, Any], path: str) -> None:
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value, f"{path}{key}.")
            elif key.endswith("_share") and isinstance(value, float):
                assert 0.0 <= value <= 1.0, f"{path}{key} = {value}"

    walk(load_yaml(name), "")


def test_ring_constraints_make_the_test_window_meaningful() -> None:
    """PLAN §4.5: without fresh rings in the test window the ring evaluation is hollow."""
    ring = load_yaml("sim")["patterns"]["ring"]
    assert ring["min_rings_in_test"] >= 12
    assert ring["min_rings_in_test_with_device_reuse"] >= 4
    assert ring["min_rings_fully_in_train"] >= 20
    assert ring["min_rings_in_test"] + ring["min_rings_fully_in_train"] <= ring["n_rings"]
