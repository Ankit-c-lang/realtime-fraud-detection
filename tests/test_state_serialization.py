"""AccountState round-trips byte for byte (PLAN §5.3, §13).

The blob is what Redis holds per account. If serialising, restoring and serialising again
produced different bytes, the in-memory and Redis stores would drift apart and the parity
test in §5.4 would fail for reasons that had nothing to do with the feature logic.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from fraud.features.state import EPOCH, AccountState, to_millis


def _populated() -> AccountState:
    return AccountState(
        recent=[
            (1_772_000_000_000, 1234.56, "M000007", 0),
            (1_772_000_060_000, 9.99, "M000002", 1),
        ],
        last_ts=1_772_000_060_000,
        last_lat=19.076,
        last_lon=72.8777,
        n_all=7,
        n_ok=5,
        sum_ok=4321.5,
        sumsq_ok=9_876_543.21,
        hour_buckets=[1, 0, 3, 0, 2, 1],
        devices={"D0000002": 1_772_000_060_000, "D0000001": 1_771_000_000_000},
        merchants={"M000007", "M000002", "M000001"},
        cities={"Mumbai", "Delhi"},
    )


def test_round_trip_is_byte_identical() -> None:
    first = _populated().to_json()
    assert AccountState.from_json(first).to_json() == first


def test_round_trip_preserves_every_field() -> None:
    original = _populated()
    restored = AccountState.from_json(original.to_json())

    assert restored.recent == original.recent
    assert restored.last_ts == original.last_ts
    assert (restored.last_lat, restored.last_lon) == (original.last_lat, original.last_lon)
    assert (restored.n_all, restored.n_ok) == (original.n_all, original.n_ok)
    assert restored.sum_ok == original.sum_ok
    assert restored.sumsq_ok == original.sumsq_ok
    assert restored.hour_buckets == original.hour_buckets
    assert restored.devices == original.devices
    assert restored.merchants == original.merchants
    assert restored.cities == original.cities


def test_an_empty_state_round_trips() -> None:
    blob = AccountState().to_json()
    assert AccountState.from_json(blob).to_json() == blob


def test_sets_are_written_sorted_so_the_blob_is_stable() -> None:
    """Python set iteration order is not guaranteed across processes."""
    one = AccountState(merchants={"M3", "M1", "M2"}, cities={"Delhi", "Mumbai"})
    two = AccountState(merchants={"M2", "M3", "M1"}, cities={"Mumbai", "Delhi"})
    assert one.to_json() == two.to_json()

    payload = json.loads(one.to_json())
    assert payload["merchants"] == ["M1", "M2", "M3"]
    assert payload["cities"] == ["Delhi", "Mumbai"]


def test_keys_are_sorted() -> None:
    payload = json.loads(_populated().to_json())
    assert list(payload) == sorted(payload)


def test_floats_survive_exactly() -> None:
    """Amount sums feed the z-score, so a rounding drift would move a feature."""
    state = AccountState(sum_ok=0.1 + 0.2, sumsq_ok=1e-9, last_lat=19.076000000000001)
    restored = AccountState.from_json(state.to_json())

    assert restored.sum_ok == state.sum_ok
    assert restored.sumsq_ok == state.sumsq_ok
    assert restored.last_lat == state.last_lat


def test_statistics_helpers() -> None:
    """mean and sample std over prior approved amounts 100, 200, 300."""
    state = AccountState(n_ok=3, sum_ok=600.0, sumsq_ok=140_000.0)
    assert state.mean_amount() == pytest.approx(200.0)
    assert state.std_amount() == pytest.approx(100.0)


def test_std_is_zero_below_two_observations() -> None:
    assert AccountState().std_amount() == 0.0
    assert AccountState(n_ok=1, sum_ok=50.0, sumsq_ok=2500.0).std_amount() == 0.0


def test_variance_never_goes_negative() -> None:
    """Floating point can make the sum-of-squares form slightly negative."""
    state = AccountState(n_ok=3, sum_ok=300.0, sumsq_ok=30_000.0 - 1e-9)
    assert state.std_amount() >= 0.0


def test_millisecond_conversion_is_machine_independent() -> None:
    """Never datetime.timestamp(): that reads a naive value in the local zone."""
    assert to_millis(EPOCH) == 0
    assert to_millis(datetime(1970, 1, 1, 0, 0, 1)) == 1000  # noqa: DTZ001
    assert to_millis(datetime(2026, 3, 1, 0, 0, 0)) == 1_772_323_200_000  # noqa: DTZ001
