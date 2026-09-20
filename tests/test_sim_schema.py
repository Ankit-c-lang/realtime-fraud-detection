"""The written dataset matches PLAN §4.2 and leaks no labels (§4.8, §13)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from fraud.config import load_yaml
from fraud.sim.generate import (
    ACCOUNT_COLUMNS,
    LABEL_FILE_COLUMNS,
    MERCHANT_COLUMNS,
    NO_FRAUD,
    TXN_ID_COLUMNS,
    Dataset,
    build_dataset,
    write_dataset,
)
from fraud.sim.patterns import FRAUD_TYPES

LABEL_LEAK_COLUMNS = ("is_fraud", "fraud_type", "attack_id", "ring_id", "label_available_at")


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return load_yaml("sim_tiny")


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return build_dataset("sim_tiny")


# --- table shapes (PLAN §4.2) -----------------------------------------------


def test_events_have_exactly_the_documented_columns(dataset: Dataset) -> None:
    assert tuple(dataset.events.columns) == TXN_ID_COLUMNS


def test_events_carry_no_label_columns(dataset: Dataset) -> None:
    """Invariant 4. A label reaching the stream would make the whole result worthless."""
    assert not set(dataset.events.columns) & set(LABEL_LEAK_COLUMNS)


def test_accounts_and_merchants_match_the_plan(dataset: Dataset) -> None:
    assert tuple(dataset.accounts.columns) == ACCOUNT_COLUMNS
    assert tuple(dataset.merchants.columns) == MERCHANT_COLUMNS


def test_labels_match_the_plan(dataset: Dataset) -> None:
    assert tuple(dataset.labels.columns) == LABEL_FILE_COLUMNS


# --- identifiers and ordering (PLAN §4.8) -----------------------------------


def test_txn_id_is_unique_and_formatted(dataset: Dataset, config: dict[str, Any]) -> None:
    ids = dataset.events["txn_id"]
    assert ids.is_unique
    assert ids.iloc[0] == str(config["output"]["txn_id_format"]).format(0)


def test_events_are_sorted_and_txn_id_follows_that_order(dataset: Dataset) -> None:
    """Assigned after the sort, so the identifier itself carries the sequence."""
    assert dataset.events["event_time"].is_monotonic_increasing
    assert dataset.events["txn_id"].is_monotonic_increasing


def test_no_event_precedes_its_account(dataset: Dataset) -> None:
    joined = dataset.events.merge(
        dataset.accounts[["account_id", "created_at"]], on="account_id", how="left"
    )
    assert joined["created_at"].notna().all(), "an event references an unknown account"
    assert (joined["event_time"] >= joined["created_at"]).all()


def test_every_event_references_a_known_merchant(dataset: Dataset) -> None:
    assert set(dataset.events["merchant_id"]) <= set(dataset.merchants["merchant_id"])


# --- labels (PLAN §4.6) -----------------------------------------------------


def test_one_label_row_per_event(dataset: Dataset) -> None:
    assert len(dataset.labels) == len(dataset.events)
    assert dataset.labels["txn_id"].tolist() == dataset.events["txn_id"].tolist()


def test_label_delay_is_applied(dataset: Dataset, config: dict[str, Any]) -> None:
    """Chargebacks arrive late; the graph may only seed from labels already available."""
    delay = pd.Timedelta(days=int(config["labels"]["delay_days"]))
    merged = dataset.labels.merge(dataset.events[["txn_id", "event_time"]], on="txn_id")
    assert ((merged["label_available_at"] - merged["event_time"]) == delay).all()


def test_fraud_types_are_from_the_fixed_set(dataset: Dataset) -> None:
    assert set(dataset.labels["fraud_type"]) <= {NO_FRAUD, *FRAUD_TYPES}


def test_is_fraud_agrees_with_fraud_type(dataset: Dataset) -> None:
    labels = dataset.labels
    assert (labels.loc[labels["fraud_type"] == NO_FRAUD, "is_fraud"] == 0).all()
    assert (labels.loc[labels["fraud_type"] != NO_FRAUD, "is_fraud"] == 1).all()


def test_legitimate_rows_carry_no_attack_identifiers(dataset: Dataset) -> None:
    legit = dataset.labels[dataset.labels["is_fraud"] == 0]
    assert legit["attack_id"].isna().all()
    assert legit["ring_id"].isna().all()


def test_fraud_prevalence_is_in_band(dataset: Dataset) -> None:
    assert 0.010 <= dataset.labels["is_fraud"].mean() <= 0.020


# --- writing (PLAN §3.7) ----------------------------------------------------


def test_written_files_round_trip(dataset: Dataset, tmp_path: Path) -> None:
    write_dataset(dataset, tmp_path)

    for name in ("events", "accounts", "merchants", "labels"):
        written = pd.read_parquet(tmp_path / f"{name}.parquet")
        pd.testing.assert_frame_equal(written, getattr(dataset, name))


def test_manifest_records_what_produced_the_run(dataset: Dataset, config: dict[str, Any]) -> None:
    """§4.8: the config hash in the manifest is what the freeze pins."""
    manifest = dataset.manifest

    assert manifest["seed"] == int(config["seed"])
    assert len(manifest["config_sha256"]) == 64
    assert manifest["rows"]["events"] == len(dataset.events)
    assert set(manifest["fraud"]) == set(FRAUD_TYPES)
    assert manifest["rings"] > 0
