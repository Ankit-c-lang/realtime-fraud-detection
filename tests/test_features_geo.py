"""Distance and speed features, from hand-computed examples (PLAN §5.2, §13).

WRITTEN BEFORE THE IMPLEMENTATION (prompt P2.1). Every expected value below was worked
out by hand or from the haversine formula directly. The implementation has to match these
numbers; the numbers do not get adjusted to match an implementation.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from fraud.features.engine import haversine_km
from tests.feature_helpers import DELHI, MUMBAI, at, last_row, make_event

# Great-circle Delhi to Mumbai, computed directly from the two coordinate pairs.
DELHI_MUMBAI_KM = 1148.09
SPEED_CAP_KMH = 5000.0


def test_haversine_delhi_to_mumbai() -> None:
    """PLAN §13 names this one explicitly: about 1,150 km, within 15 km."""
    assert haversine_km(*DELHI, *MUMBAI) == pytest.approx(1150.0, abs=15.0)
    assert haversine_km(*DELHI, *MUMBAI) == pytest.approx(DELHI_MUMBAI_KM, abs=0.5)


def test_haversine_is_symmetric_and_zero_at_a_point() -> None:
    assert haversine_km(*MUMBAI, *DELHI) == pytest.approx(haversine_km(*DELHI, *MUMBAI))
    assert haversine_km(*MUMBAI, *MUMBAI) == pytest.approx(0.0)


def test_geo_speed_is_zero_without_a_previous_event() -> None:
    """Cold-start default (PLAN §5.1 rule 4)."""
    row = last_row([make_event("T0000000", at(10), location=MUMBAI)])
    assert row["geo_speed_kmh"] == 0.0


def test_geo_speed_over_two_hours() -> None:
    """1148.09 km in exactly 2 h = 574.05 km/h."""
    row = last_row(
        [
            make_event("T0000000", at(10), location=DELHI, city="Delhi"),
            make_event("T0000001", at(12), location=MUMBAI, city="Mumbai"),
        ]
    )
    assert row["geo_speed_kmh"] == pytest.approx(574.05, abs=0.5)


def test_geo_speed_uses_a_one_minute_floor() -> None:
    """A 1 km hop 10 s later is 60 km/h, not 360: Δt floors at one minute (§5.2 #25).

    Without the floor, two events in the same second give a division by zero and any
    rapid pair looks like a jet.
    """
    near = (MUMBAI[0] + 1.0 / 111.0, MUMBAI[1])  # 1 km due north
    row = last_row(
        [
            make_event("T0000000", at(10, 0, 0), location=MUMBAI),
            make_event("T0000001", at(10, 0, 10), location=near),
        ]
    )
    assert row["geo_speed_kmh"] == pytest.approx(60.0, abs=1.0)


def test_geo_speed_is_capped() -> None:
    """Delhi to Mumbai in 10 s floors to 1 min, giving ~68,886 km/h, capped at 5,000."""
    row = last_row(
        [
            make_event("T0000000", at(10, 0, 0), location=DELHI, city="Delhi"),
            make_event("T0000001", at(10, 0, 10), location=MUMBAI, city="Mumbai"),
        ]
    )
    assert row["geo_speed_kmh"] == SPEED_CAP_KMH


def test_geo_speed_is_zero_when_the_location_does_not_move() -> None:
    row = last_row(
        [
            make_event("T0000000", at(10), location=MUMBAI),
            make_event("T0000001", at(11), location=MUMBAI),
        ]
    )
    assert row["geo_speed_kmh"] == pytest.approx(0.0)


def test_km_from_home_is_zero_at_home() -> None:
    row = last_row([make_event("T0000000", at(10), location=MUMBAI)])
    assert row["km_from_home"] == pytest.approx(0.0, abs=0.01)


def test_km_from_home_measures_from_the_account_home() -> None:
    """The account's home is Mumbai, so an event in Delhi sits 1,148 km out."""
    row = last_row([make_event("T0000000", at(10), location=DELHI, city="Delhi")])
    assert row["km_from_home"] == pytest.approx(DELHI_MUMBAI_KM, abs=0.5)


def test_impossible_travel_is_what_separates_ato_from_a_real_trip() -> None:
    """The same two cities an hour apart is impossible; a day apart is a flight."""
    fast = last_row(
        [
            make_event("T0000000", at(10), location=DELHI, city="Delhi"),
            make_event("T0000001", at(11), location=MUMBAI, city="Mumbai"),
        ]
    )
    slow = last_row(
        [
            make_event("T0000000", at(10), location=DELHI, city="Delhi"),
            make_event("T0000001", at(10) + timedelta(days=1), location=MUMBAI, city="Mumbai"),
        ]
    )
    assert fast["geo_speed_kmh"] > 900.0
    assert slow["geo_speed_kmh"] < 100.0
