"""The only loader of split data (PLAN §4.7, §7.11, invariant 6).

Nothing else may slice the dataset by date. Split boundaries live in
``configs/splits.yaml`` and reach the rest of the code only through here, so a stray
date literal cannot quietly redefine what "train" means.

Two protections matter more than the convenience:

* **Burn-in rows are dropped.** Their windows are only partly filled, so training on
  them teaches the model that a cold account is normal (leakage rule L5).
* **The test split is locked.** ``load("test")`` raises unless ``ALLOW_TEST=1``, which
  only ``make evaluate-test`` sets. Test data read by accident cannot be un-read, and the
  headline numbers stop being trustworthy the moment it happens (L10, §7.11).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pandas as pd

from fraud.config import Settings, load_yaml
from fraud.features.spec import FEATURE_NAMES, HOT_FEATURE_NAMES

TEST_SPLIT: Final[str] = "test"
BURN_IN_SPLIT: Final[str] = "burn_in"
ALLOW_TEST_ENV: Final[str] = "ALLOW_TEST"

LABEL_COLUMNS: Final[tuple[str, ...]] = ("is_fraud", "fraud_type", "attack_id", "ring_id")
TRAINABLE_SPLITS: Final[tuple[str, ...]] = ("train", "early_stop", "valid")


class SplitLockedError(RuntimeError):
    """Raised when something reaches for the test split without permission.

    Not named Test*: pytest tries to collect any such class and warns, and a name
    that fights the test runner is a name that gets quietly renamed later.
    """


@dataclass(frozen=True, slots=True)
class SplitWindow:
    name: str
    start: pd.Timestamp
    end: pd.Timestamp


def windows() -> dict[str, SplitWindow]:
    """Every split interval, half-open [start, end)."""
    return {
        name: SplitWindow(name, pd.Timestamp(spec["start"]), pd.Timestamp(spec["end"]))
        for name, spec in load_yaml("splits")["splits"].items()
    }


def allow_test_enabled() -> bool:
    """True only when ALLOW_TEST=1.

    Deliberately not named ``test_*``: pytest would collect it as a test case, and a
    guard that silently becomes a test is a guard nobody is checking.
    """
    return os.environ.get(ALLOW_TEST_ENV) == "1"


def _guard(split: str) -> None:
    if split == TEST_SPLIT and not allow_test_enabled():
        raise SplitLockedError(
            "The test split is read only by evaluate_test.py, once, at the end "
            f"(PLAN §7.11). Set {ALLOW_TEST_ENV}=1 to override, which `make evaluate-test` "
            "does. If you are tuning anything, you want 'valid'."
        )


def load(
    split: str,
    *,
    features_path: Path | None = None,
    labels_path: Path | None = None,
) -> pd.DataFrame:
    """Features joined to labels for one split, with burn-in rows already gone."""
    _guard(split)

    known = windows()
    if split not in known:
        raise KeyError(f"unknown split {split!r}; configs/splits.yaml defines {sorted(known)}")

    settings = Settings.from_env()
    features = pd.read_parquet(features_path or settings.features_dir / "hot_features.parquet")
    labels = pd.read_parquet(
        labels_path or settings.raw_dir / "labels.parquet",
        columns=["txn_id", *LABEL_COLUMNS],
    )

    frame = features.merge(labels, on="txn_id", validate="one_to_one")
    return _select(frame, split)


def load_many(splits: list[str], **kwargs: Path | None) -> dict[str, pd.DataFrame]:
    """Several splits from one read, since the parquet file is the expensive part."""
    for split in splits:
        _guard(split)

    settings = Settings.from_env()
    features = pd.read_parquet(
        kwargs.get("features_path") or settings.features_dir / "hot_features.parquet"
    )
    labels = pd.read_parquet(
        kwargs.get("labels_path") or settings.raw_dir / "labels.parquet",
        columns=["txn_id", *LABEL_COLUMNS],
    )
    frame = features.merge(labels, on="txn_id", validate="one_to_one")
    return {split: _select(frame, split) for split in splits}


def _select(frame: pd.DataFrame, split: str) -> pd.DataFrame:
    chosen = frame[frame["split"] == split]
    # Belt and braces: burn-in is its own split, but a row flagged in_burn_in never
    # reaches a model regardless of how it came to be tagged.
    chosen = chosen[chosen["in_burn_in"] == 0]
    return chosen.reset_index(drop=True)


def feature_matrix(frame: pd.DataFrame, *, warm: bool = False) -> pd.DataFrame:
    """Only the columns in the spec, in spec order (PLAN §5.1 rule 6).

    Selecting by name from the spec rather than by dropping label columns means a new
    column in the data can never become a feature by accident, which is how identifiers
    leak into models (L8).
    """
    names = FEATURE_NAMES if warm else HOT_FEATURE_NAMES
    missing = [name for name in names if name not in frame.columns]
    if missing:
        raise KeyError(f"feature columns missing from the frame: {missing}")
    return frame[list(names)]


def target(frame: pd.DataFrame) -> pd.Series:
    return frame["is_fraud"].astype("int8")
