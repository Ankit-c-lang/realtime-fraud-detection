"""The §4.8 validation suite itself (PLAN §4.8, §13).

A checker that only ever passes is worthless, so alongside the happy path each group is
shown a deliberately corrupted dataset and has to fail on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from fraud.config import load_yaml
from fraud.sim.checks import (
    Check,
    Tables,
    _hard_negative_checks,
    _ring_checks,
    _schema_checks,
    _volume_checks,
    haversine_km,
    load_tables,
    run_checks,
    write_report,
)
from fraud.sim.generate import build_dataset, write_dataset


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return load_yaml("sim_tiny")


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("dataset")
    write_dataset(build_dataset("sim_tiny"), directory)
    return directory


@pytest.fixture(scope="module")
def tables(written: Path) -> Tables:
    return load_tables(written)


def _failures(checks: list[Check]) -> list[str]:
    return [check.name for check in checks if not check.passed]


# --- the happy path ---------------------------------------------------------


def test_a_clean_dataset_passes_everything(tables: Tables, config: dict[str, Any]) -> None:
    """Determinism is skipped here; it runs the generator twice and has its own test."""
    checks = run_checks(tables, config, tmp_root=None)
    assert _failures(checks) == []
    assert len(checks) >= 20


def test_report_records_the_config_hash(
    tables: Tables, config: dict[str, Any], tmp_path: Path
) -> None:
    """§4.8's freeze rule pins this hash, so the report has to carry it."""
    checks = run_checks(tables, config, tmp_root=None)
    report = tmp_path / "sim_report.md"
    write_report(tables, config, checks, report)

    text = report.read_text(encoding="utf-8")
    assert tables.manifest["config_sha256"] in text
    assert "All checks passed" in text
    assert "### VELOCITY" in text and "### RING" in text


# --- geography --------------------------------------------------------------


def test_haversine_matches_a_known_distance() -> None:
    """Delhi to Mumbai is about 1,150 km (PLAN §13, test_features_geo)."""
    assert haversine_km(28.6139, 77.2090, 19.0760, 72.8777) == pytest.approx(1150.0, abs=15.0)


def test_haversine_is_zero_for_the_same_point() -> None:
    assert haversine_km(19.0760, 72.8777, 19.0760, 72.8777) == pytest.approx(0.0)


# --- each group has to be able to fail --------------------------------------


def test_schema_check_catches_a_label_leak(tables: Tables) -> None:
    """The one that matters most: a label reaching the event stream (invariant 4)."""
    leaked = tables.events.copy()
    leaked["is_fraud"] = 0
    broken = Tables(leaked, tables.accounts, tables.merchants, tables.labels, tables.manifest)

    assert "no label column leaks into events" in _failures(_schema_checks(broken))


def test_schema_check_catches_unsorted_events(tables: Tables) -> None:
    shuffled = tables.events.iloc[::-1].reset_index(drop=True)
    broken = Tables(shuffled, tables.accounts, tables.merchants, tables.labels, tables.manifest)

    assert "events are sorted by event_time" in _failures(_schema_checks(broken))


def test_schema_check_catches_an_event_before_its_account_existed(tables: Tables) -> None:
    accounts = tables.accounts.copy()
    accounts.loc[accounts.index[0], "created_at"] = pd.Timestamp("2027-01-01")
    broken = Tables(tables.events, accounts, tables.merchants, tables.labels, tables.manifest)

    assert "no event precedes its account's creation" in _failures(_schema_checks(broken))


def test_volume_check_catches_prevalence_drift(tables: Tables, config: dict[str, Any]) -> None:
    labels = tables.labels.copy()
    labels["is_fraud"] = 1
    labels["fraud_type"] = "VELOCITY"
    broken = Tables(tables.events, tables.accounts, tables.merchants, labels, tables.manifest)

    assert "fraud prevalence within [1.0%, 2.0%]" in _failures(_volume_checks(broken, config))


def test_ring_check_catches_a_missing_test_window_quota(
    tables: Tables, config: dict[str, Any]
) -> None:
    """Drop the rings that start in test; the quota must notice."""
    splits = load_yaml("splits")["splits"]
    joined = tables.joined
    rings = joined[joined["fraud_type"] == "RING"]
    spans = rings.groupby("ring_id")["event_time"].agg(["min", "max"])
    starting_in_test = spans[spans["min"] >= pd.Timestamp(splits["test"]["start"])].index

    labels = tables.labels.copy()
    labels.loc[labels["ring_id"].isin(starting_in_test), ["is_fraud", "fraud_type"]] = (0, "NONE")
    broken = Tables(tables.events, tables.accounts, tables.merchants, labels, tables.manifest)

    failures = _failures(_ring_checks(broken, config))
    assert any("start inside the test window" in name for name in failures)


def test_hard_negative_check_catches_missing_shared_devices(
    tables: Tables, config: dict[str, Any]
) -> None:
    """If households stopped sharing a device, rings would be trivially separable."""
    events = tables.events.copy()
    events["device_id"] = events["account_id"]  # one device each, nothing shared
    broken = Tables(events, tables.accounts, tables.merchants, tables.labels, tables.manifest)

    assert "households share a device" in _failures(_hard_negative_checks(broken, config))


def test_hard_negative_check_catches_missing_vpn_travel(
    tables: Tables, config: dict[str, Any]
) -> None:
    events = tables.events.copy()
    events["country"] = "IN"
    broken = Tables(events, tables.accounts, tables.merchants, tables.labels, tables.manifest)

    assert "VPN use produces legitimate impossible travel" in _failures(
        _hard_negative_checks(broken, config)
    )
