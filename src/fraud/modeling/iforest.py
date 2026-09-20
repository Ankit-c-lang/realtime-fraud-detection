"""Label-free Isolation Forest fitted on train only (PLAN §7.5).

The XGBoost half of the model knows exactly four fraud patterns, because those are the
four it was shown. This half knows none of them. It is fitted on every train row with
the labels ignored, so what it flags is "unlike the training distribution" rather than
"like the fraud I was taught", and that is the only part of the system with any chance
against a pattern nobody has labelled yet.

Three decisions carry the design.

**Label-free is structural, not cosmetic.** Passing labels here would make the detector
depend on the thing it is meant to complement (leakage rule L6), and it would break the
leave-one-pattern-out experiment in §7.6: the whole point of LOPO is that the anomaly
half is *identical* across the four runs, so any difference between them is the withheld
pattern rather than a differently-fitted detector.

**Percentiles, not min-max.** The raw score is mapped through 1,001 quantiles of the
train scores. Min-max scaling would let one extreme outlier compress every other score
into a sliver near zero, and the blend weight in §7.6 would then be tuning against a
constant.

**The spread used for reasons falls back to the standard deviation.** Most of these
features are counts that are zero for well over half the rows, so their IQR is exactly
zero and ``|x - median| / IQR`` would be a division by zero. A floor alone would make
the sparsest feature win every reason it appears in, so a degenerate IQR falls back to
the standard deviation, where the spread of such a feature actually lives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from fraud.config import load_yaml
from fraud.features.spec import FEATURE_SPEC_VERSION, NUMERIC_FEATURE_NAMES

logger = logging.getLogger(__name__)

# Stops a division by zero on a feature that is constant across all of train. The
# numerator is zero there too, so the value of the floor never reaches a result.
SPREAD_FLOOR: Final[float] = 1e-12


def numeric_matrix(frame: pd.DataFrame) -> np.ndarray:
    """The 35 numeric features, in spec order, as float64.

    Selected by name from the spec rather than by dropping everything non-numeric: a
    new numeric column in the training table must never become a model input by
    accident (L8), and the column order has to survive a round trip through an
    artifact (§8).
    """
    missing = [name for name in NUMERIC_FEATURE_NAMES if name not in frame.columns]
    if missing:
        raise KeyError(f"numeric feature columns missing from the frame: {missing}")
    return frame[list(NUMERIC_FEATURE_NAMES)].to_numpy(dtype=np.float64)


@dataclass(frozen=True, slots=True)
class AnomalyDetector:
    """A fitted forest plus everything needed to interpret its scores.

    The quantiles and the per-feature statistics are as much a part of the model as the
    trees: without them a raw score is an unbounded number with no scale, and §7.10 has
    no anomaly reason to write.
    """

    forest: IsolationForest
    quantiles: np.ndarray
    medians: np.ndarray
    spreads: np.ndarray
    feature_names: tuple[str, ...]
    train_rows: int
    feature_spec_version: str = FEATURE_SPEC_VERSION

    @property
    def levels(self) -> np.ndarray:
        """The percentile each stored quantile corresponds to, 0 to 1."""
        return np.linspace(0.0, 1.0, len(self.quantiles))

    def raw(self, frame: pd.DataFrame) -> np.ndarray:
        """``s = -score_samples(X)``: higher means more anomalous (§7.5)."""
        return -self.forest.score_samples(numeric_matrix(frame))

    def percentile(self, frame: pd.DataFrame) -> np.ndarray:
        """Where each row's raw score falls among the train scores, in [0, 1].

        ``np.interp`` clamps outside the stored range, so a score more extreme than
        anything in train lands at exactly 1.0 rather than extrapolating off the end.
        """
        return np.interp(self.raw(frame), self.quantiles, self.levels)

    def deviations(self, frame: pd.DataFrame) -> np.ndarray:
        """``|x - median| / spread`` per row and feature, on the train statistics."""
        matrix = numeric_matrix(frame)
        return np.abs(matrix - self.medians) / self.spreads

    def top_features(self, frame: pd.DataFrame, count: int = 2) -> list[tuple[str, ...]]:
        """The ``count`` features furthest from typical, per row (§7.5, §7.10).

        These name the anomaly reason. They are descriptive, not causal: the forest
        isolates on combinations, and this reports which parts of the combination are
        individually unusual.
        """
        deviations = self.deviations(frame)
        names = np.asarray(self.feature_names)
        # argsort ascending then take the tail, reversed: largest deviation first.
        order = np.argsort(deviations, axis=1)[:, -count:][:, ::-1]
        return [tuple(names[row]) for row in order]

    def metadata(self) -> dict[str, Any]:
        """What §8 records about this half of the model."""
        return {
            "n_estimators": int(self.forest.n_estimators),
            "max_samples": int(self.forest.max_samples_),
            "random_state": int(self.forest.random_state),
            "train_rows": self.train_rows,
            "n_features": len(self.feature_names),
            "quantile_points": len(self.quantiles),
            "feature_spec_version": self.feature_spec_version,
            "label_free": True,
        }


def fit(train: pd.DataFrame, *, n_jobs: int = -1) -> AnomalyDetector:
    """Fit on every train row, with the labels ignored (PLAN §7.5).

    ``train`` may carry its label columns; they are never read. Only the columns named
    in the spec reach the forest.
    """
    config = load_yaml("model")["iforest"]
    matrix = numeric_matrix(train)

    forest = IsolationForest(
        n_estimators=int(config["n_estimators"]),
        max_samples=int(config["max_samples"]),
        contamination=config["contamination"],
        random_state=int(config["random_state"]),
        n_jobs=n_jobs,
    ).fit(matrix)

    raw = -forest.score_samples(matrix)
    points = int(config["quantile_points"])
    quantiles = np.quantile(raw, np.linspace(0.0, 1.0, points))

    medians = np.median(matrix, axis=0)
    spreads = _spreads(matrix)

    logger.info(
        "isolation forest: %s train rows, %d features, raw score %.3f..%.3f",
        f"{len(matrix):,}",
        matrix.shape[1],
        float(raw.min()),
        float(raw.max()),
    )
    return AnomalyDetector(
        forest=forest,
        quantiles=quantiles,
        medians=medians,
        spreads=spreads,
        feature_names=NUMERIC_FEATURE_NAMES,
        train_rows=len(matrix),
    )


def _spreads(matrix: np.ndarray) -> np.ndarray:
    """Per-feature IQR, falling back to the standard deviation where the IQR is zero.

    A zero IQR means the middle half of the column is a single value, which is the norm
    for sparse counts like ``acct_declines_1h``. Dividing by a tiny floor instead would
    hand every anomaly reason to whichever count feature happens to be sparsest.
    """
    q75, q25 = np.percentile(matrix, [75, 25], axis=0)
    spreads = q75 - q25

    degenerate = spreads <= 0
    if degenerate.any():
        spreads = spreads.copy()
        spreads[degenerate] = matrix[:, degenerate].std(axis=0)

    # Whatever is still zero is a constant column, where |x - median| is zero as well.
    return np.where(spreads > 0, spreads, SPREAD_FLOOR)
