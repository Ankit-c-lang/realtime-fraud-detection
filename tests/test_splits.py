"""Split loading and the test-split lock (PLAN §4.7, §7.11, leakage rules L5 and L10)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fraud.features.spec import FEATURE_NAMES, HOT_FEATURE_NAMES
from fraud.modeling import splits as split_module
from fraud.modeling.splits import (
    ALLOW_TEST_ENV,
    SplitLockedError,
    allow_test_enabled,
    feature_matrix,
    load,
    load_many,
    target,
    windows,
)

SPLIT_ORDER = ("burn_in", "train", "early_stop", "valid", "test")


@pytest.fixture
def dataset(tmp_path: Path) -> tuple[Path, Path]:
    """A tiny features/labels pair, two rows in every split plus burn-in."""
    moments = {
        "burn_in": "2026-01-05",
        "train": "2026-02-01",
        "early_stop": "2026-02-25",
        "valid": "2026-03-05",
        "test": "2026-03-20",
    }
    rows = []
    for index, (name, day) in enumerate(moments.items()):
        for offset in range(2):
            rows.append(
                {
                    "txn_id": f"T{index * 2 + offset:07d}",
                    "event_time": pd.Timestamp(day),
                    "account_id": "A0000001",
                    "split": name,
                    "in_burn_in": int(name == "burn_in"),
                    "feature_spec_version": "fs1",
                    **{feature: float(offset) for feature in HOT_FEATURE_NAMES},
                }
            )
    features = pd.DataFrame(rows)
    features["merchant_category"] = "grocery"

    labels = pd.DataFrame(
        {
            "txn_id": features["txn_id"],
            "is_fraud": [index % 2 for index in range(len(features))],
            "fraud_type": ["NONE" if index % 2 == 0 else "ATO" for index in range(len(features))],
            "attack_id": None,
            "ring_id": None,
        }
    )

    feature_path = tmp_path / "hot_features.parquet"
    label_path = tmp_path / "labels.parquet"
    features.to_parquet(feature_path, index=False)
    labels.to_parquet(label_path, index=False)
    return feature_path, label_path


# --- the lock (PLAN §7.11, L10) ---------------------------------------------


def test_the_test_split_is_locked_by_default(
    dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test data read by accident cannot be un-read (PLAN §7.11)."""
    monkeypatch.delenv(ALLOW_TEST_ENV, raising=False)
    features, labels = dataset

    with pytest.raises(SplitLockedError, match="evaluate_test"):
        load("test", features_path=features, labels_path=labels)


def test_the_lock_opens_only_for_the_exact_flag(
    dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    features, labels = dataset
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv(ALLOW_TEST_ENV, value)
        assert not allow_test_enabled()
        with pytest.raises(SplitLockedError):
            load("test", features_path=features, labels_path=labels)

    monkeypatch.setenv(ALLOW_TEST_ENV, "1")
    assert allow_test_enabled()
    assert len(load("test", features_path=features, labels_path=labels)) == 2


def test_load_many_is_locked_too(
    dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bulk loader is the obvious way to slip past a per-split guard."""
    monkeypatch.delenv(ALLOW_TEST_ENV, raising=False)
    features, labels = dataset

    with pytest.raises(SplitLockedError):
        load_many(["train", "test"], features_path=features, labels_path=labels)


def test_the_other_splits_are_never_locked(
    dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_TEST_ENV, raising=False)
    features, labels = dataset

    for split in ("train", "early_stop", "valid"):
        assert len(load(split, features_path=features, labels_path=labels)) == 2


# --- burn-in and boundaries (L5) --------------------------------------------


def test_burn_in_rows_never_reach_a_model(
    dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Their windows are only partly filled, so they teach the wrong lesson."""
    monkeypatch.delenv(ALLOW_TEST_ENV, raising=False)
    features, labels = dataset

    assert load("burn_in", features_path=features, labels_path=labels).empty
    for split in ("train", "early_stop", "valid"):
        frame = load(split, features_path=features, labels_path=labels)
        assert (frame["in_burn_in"] == 0).all()


def test_split_windows_are_contiguous_and_half_open() -> None:
    known = windows()
    previous = None
    for name in SPLIT_ORDER:
        window = known[name]
        assert window.start < window.end
        if previous is not None:
            assert window.start == previous
        previous = window.end


def test_splits_do_not_overlap(dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_TEST_ENV, "1")
    features, labels = dataset

    seen: set[str] = set()
    for split in SPLIT_ORDER:
        ids = set(load(split, features_path=features, labels_path=labels)["txn_id"])
        assert not ids & seen, f"{split} shares rows with an earlier split"
        seen |= ids


def test_an_unknown_split_is_rejected(dataset: tuple[Path, Path]) -> None:
    features, labels = dataset
    with pytest.raises(KeyError, match="unknown split"):
        load("holdout", features_path=features, labels_path=labels)


# --- the feature matrix (L8) ------------------------------------------------


def test_feature_matrix_returns_spec_order(
    dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_TEST_ENV, raising=False)
    features, labels = dataset
    frame = load("train", features_path=features, labels_path=labels)

    assert tuple(feature_matrix(frame).columns) == HOT_FEATURE_NAMES


def test_feature_matrix_never_passes_an_identifier_through(
    dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting BY the spec, not by dropping labels, is what makes L8 hold.

    A new column appearing in the data cannot become a feature by accident.
    """
    monkeypatch.delenv(ALLOW_TEST_ENV, raising=False)
    features, labels = dataset
    frame = load("train", features_path=features, labels_path=labels)
    frame["account_id_numeric"] = 7
    frame["attack_id"] = "VEL0001"

    columns = set(feature_matrix(frame).columns)
    assert not columns & {
        "account_id",
        "account_id_numeric",
        "attack_id",
        "ring_id",
        "txn_id",
        "is_fraud",
    }


def test_feature_matrix_complains_about_missing_columns(
    dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_TEST_ENV, raising=False)
    features, labels = dataset
    frame = load("train", features_path=features, labels_path=labels)

    with pytest.raises(KeyError, match="missing"):
        feature_matrix(frame.drop(columns=["acct_cnt_5m"]))


def test_warm_features_are_opt_in(
    dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Graph features do not exist until Phase 4, so hot-only is the default."""
    monkeypatch.delenv(ALLOW_TEST_ENV, raising=False)
    features, labels = dataset
    frame = load("train", features_path=features, labels_path=labels)

    assert len(feature_matrix(frame).columns) == 30
    for name in FEATURE_NAMES[30:]:
        frame[name] = 0.0
    assert len(feature_matrix(frame, warm=True).columns) == 36


def test_target_is_the_label(dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_TEST_ENV, raising=False)
    features, labels = dataset
    frame = load("train", features_path=features, labels_path=labels)

    assert set(target(frame)) <= {0, 1}
    assert len(target(frame)) == len(frame)


def test_module_exposes_only_one_way_in() -> None:
    """Invariant 6: splits.py is the only loader of split data."""
    assert hasattr(split_module, "load")
    assert hasattr(split_module, "load_many")
