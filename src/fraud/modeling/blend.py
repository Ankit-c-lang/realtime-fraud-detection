"""Blend weight chosen by leave-one-pattern-out validation (PLAN §7.6).

``risk = w * p_xgb + (1 - w) * anomaly_pct``.

The obvious way to pick ``w`` is to maximise something on validation, and it is the wrong
way. Validation holds only the four patterns the model was trained on, so the labelled
half wins on every one of them and the search drives ``w`` to 1.0 — which says nothing
except that a supervised model beats an unsupervised one at the job it was supervised
for. The Isolation Forest is not there for those four patterns. It is there for the
fifth one, the one nobody has labelled yet.

So the weight is chosen on a simulation of exactly that. Each pattern is withheld from
training in turn, producing four ``XGB_-k`` models that have genuinely never seen it,
and ``w`` is scored by mean overall recall on validation across those four worlds. The
anomaly half is fitted once, label-free, and reused unchanged in every run, so the only
thing that differs between them is the withheld pattern.

The test split is not involved at any point.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fraud.config import load_yaml
from fraud.modeling.iforest import AnomalyDetector
from fraud.modeling.metrics import operating_point

logger = logging.getLogger(__name__)


def weight_grid() -> tuple[float, ...]:
    return tuple(float(w) for w in load_yaml("model")["blend"]["weight_grid"])


def lopo_patterns() -> tuple[str, ...]:
    return tuple(str(p) for p in load_yaml("model")["blend"]["lopo_patterns"])


def blend(p_xgb: np.ndarray, anomaly_pct: np.ndarray, weight: float) -> np.ndarray:
    """The blended risk score (PLAN §7.6).

    Both inputs are already on [0, 1] — a probability and a percentile — which is why
    §7.2 forbids ``scale_pos_weight``: reweighting would push the probability off that
    scale and the weight would stop meaning anything.
    """
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"blend weight must be in [0, 1], got {weight}")
    return weight * np.asarray(p_xgb, dtype=float) + (1.0 - weight) * np.asarray(
        anomaly_pct, dtype=float
    )


@dataclass(frozen=True, slots=True)
class LopoRun:
    """One withheld pattern: what each weight recovers when that pattern is unseen."""

    pattern: str
    train_rows: int
    withheld_rows: int
    recall_by_weight: dict[float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "train_rows": self.train_rows,
            "withheld_rows": self.withheld_rows,
            "recall_by_weight": {f"{w:g}": round(r, 6) for w, r in self.recall_by_weight.items()},
        }


@dataclass(frozen=True, slots=True)
class E5Row:
    """One row of the §7.9 ensemble-evidence table."""

    pattern: str
    pattern_recall_alone: float
    pattern_recall_blended: float
    precision_alone: float
    precision_blended: float
    overall_recall_alone: float
    overall_recall_blended: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "pattern_recall": {
                "alone": round(self.pattern_recall_alone, 6),
                "blended": round(self.pattern_recall_blended, 6),
                "delta": round(self.pattern_recall_blended - self.pattern_recall_alone, 6),
            },
            "overall_precision": {
                "alone": round(self.precision_alone, 6),
                "blended": round(self.precision_blended, 6),
                "delta": round(self.precision_blended - self.precision_alone, 6),
            },
            "overall_recall": {
                "alone": round(self.overall_recall_alone, 6),
                "blended": round(self.overall_recall_blended, 6),
            },
        }


@dataclass(frozen=True, slots=True)
class LopoResult:
    """The chosen weight and the evidence behind it."""

    best_weight: float
    mean_recall_by_weight: dict[float, float]
    runs: list[LopoRun] = field(default_factory=list)
    e5_rows: list[E5Row] = field(default_factory=list)
    detector: dict[str, Any] = field(default_factory=dict)

    @property
    def isolation_forest_helps(self) -> bool:
        """False when ``w* == 1.0``: the anomaly half never earned any weight."""
        return self.best_weight < 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "best_weight": self.best_weight,
            "isolation_forest_helps": self.isolation_forest_helps,
            "mean_recall_by_weight": {
                f"{w:g}": round(r, 6) for w, r in self.mean_recall_by_weight.items()
            },
            "runs": [run.to_dict() for run in self.runs],
            "e5_rows": [row.to_dict() for row in self.e5_rows],
            "detector": self.detector,
            "protocol": (
                "PLAN §7.6. For each pattern k, XGB_-k is fitted on train rows excluding "
                "k (early stopping excludes k as well) and scored on the full valid "
                "split. R_k(w) is OVERALL recall at the budget-constrained threshold "
                "from §7.7, recomputed for every (k, w). w* = argmax_w mean_k R_k(w), "
                "ties to the larger w. The Isolation Forest is fitted once on all train "
                "rows, label-free, and is identical in every run."
            ),
        }


def select_weight(runs: list[LopoRun]) -> tuple[float, dict[float, float]]:
    """``w* = argmax_w mean_k R_k(w)``, ties to the larger ``w`` (PLAN §7.6).

    The tie break matters more than it looks: recall is a ratio of small integers, so
    exact ties are common, and preferring the larger ``w`` means the anomaly half only
    gets weight where it demonstrably bought something.
    """
    if not runs:
        raise ValueError("no leave-one-pattern-out runs to select from")

    weights = list(runs[0].recall_by_weight)
    means = {w: float(np.mean([run.recall_by_weight[w] for run in runs])) for w in weights}
    best = max(means, key=lambda w: (means[w], w))
    return best, means


def run_lopo(
    train: pd.DataFrame,
    early_stop: pd.DataFrame,
    valid: pd.DataFrame,
    params: dict[str, Any],
    detector: AnomalyDetector,
    *,
    warm: bool = True,
    n_jobs: int = -1,
) -> LopoResult:
    """The whole §7.6 procedure, on valid only."""
    from fraud.modeling import train_xgb
    from fraud.modeling.splits import target

    grid = weight_grid()
    anomaly = detector.percentile(valid)
    y_valid = target(valid).to_numpy()
    fraud_type = valid["fraud_type"].to_numpy()

    runs: list[LopoRun] = []
    scores: dict[str, np.ndarray] = {}

    for pattern in lopo_patterns():
        sub_train = train[train["fraud_type"] != pattern]
        sub_early = early_stop[early_stop["fraud_type"] != pattern]
        withheld = len(train) - len(sub_train)

        model = train_xgb.fit(sub_train, sub_early, params, warm=warm, n_jobs=n_jobs)
        p_xgb = train_xgb.predict(model, valid, warm=warm)
        scores[pattern] = p_xgb

        recalls = {w: operating_point(y_valid, blend(p_xgb, anomaly, w)).recall for w in grid}
        runs.append(
            LopoRun(
                pattern=pattern,
                train_rows=len(sub_train),
                withheld_rows=withheld,
                recall_by_weight=recalls,
            )
        )
        logger.info(
            "XGB_-%s: %s train rows (%s withheld)  %s",
            pattern,
            f"{len(sub_train):,}",
            f"{withheld:,}",
            "  ".join(f"w={w:g}:{r:.3f}" for w, r in recalls.items()),
        )

    best, means = select_weight(runs)
    logger.info(
        "mean recall by weight: %s", "  ".join(f"w={w:g}:{r:.4f}" for w, r in means.items())
    )
    logger.info("w* = %g", best)

    e5_rows = [
        _e5_row(pattern, scores[pattern], anomaly, best, y_valid, fraud_type)
        for pattern in lopo_patterns()
    ]
    return LopoResult(
        best_weight=best,
        mean_recall_by_weight=means,
        runs=runs,
        e5_rows=e5_rows,
        detector=detector.metadata(),
    )


def _e5_row(
    pattern: str,
    p_xgb: np.ndarray,
    anomaly: np.ndarray,
    weight: float,
    y_valid: np.ndarray,
    fraud_type: np.ndarray,
) -> E5Row:
    """§7.9's E5: what the blend recovers on the pattern its model never saw.

    "Alone" is the same model at ``w = 1.0``, not a separately fitted one, so the only
    difference between the two columns is the anomaly half.
    """
    alone = _at_operating_point(p_xgb, y_valid, fraud_type, pattern)
    blended = _at_operating_point(blend(p_xgb, anomaly, weight), y_valid, fraud_type, pattern)
    return E5Row(
        pattern=pattern,
        pattern_recall_alone=alone[0],
        pattern_recall_blended=blended[0],
        precision_alone=alone[1],
        precision_blended=blended[1],
        overall_recall_alone=alone[2],
        overall_recall_blended=blended[2],
    )


def _at_operating_point(
    risk: np.ndarray, y_valid: np.ndarray, fraud_type: np.ndarray, pattern: str
) -> tuple[float, float, float]:
    """(recall on ``pattern``, overall precision, overall recall) at the §7.7 threshold."""
    point = operating_point(y_valid, risk)
    flagged = risk >= point.threshold
    rows = fraud_type == pattern
    pattern_recall = float(flagged[rows].mean()) if rows.any() else 0.0
    return pattern_recall, point.precision, point.recall


def write(result: LopoResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("wrote %s", path)
    return path


def main(argv: list[str] | None = None) -> int:
    """Fit the detector, run LOPO on valid, and record w* (PLAN §7.6, prompt P5.1)."""
    import argparse

    from fraud.modeling import iforest
    from fraud.modeling.experiments import EXPERIMENTS_DIR, read
    from fraud.modeling.splits import load_many

    parser = argparse.ArgumentParser(description="Choose the blend weight by LOPO (PLAN §7.6).")
    parser.add_argument("--jobs", type=int, default=-1, help="XGBoost and forest n_jobs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # The final model is the 36-feature one (E3), so LOPO runs on the same feature set.
    frames = load_many(["train", "early_stop", "valid"], warm=True)
    params = read("E2")["details"]["params"]
    logger.info("using the E2 parameter set: %s", params)

    detector = iforest.fit(frames["train"], n_jobs=args.jobs)
    result = run_lopo(
        frames["train"],
        frames["early_stop"],
        frames["valid"],
        params,
        detector,
        n_jobs=args.jobs,
    )
    write(result, EXPERIMENTS_DIR / "lopo.json")

    for row in result.e5_rows:
        logger.info(
            "E5 %-13s recall on withheld pattern %.3f -> %.3f   overall precision %.3f -> %.3f",
            row.pattern,
            row.pattern_recall_alone,
            row.pattern_recall_blended,
            row.precision_alone,
            row.precision_blended,
        )

    if not result.isolation_forest_helps:
        logger.warning(
            "w* = 1.0: the Isolation Forest never earned any weight. PLAN §7.6 says to "
            "stop and report this before implementing the two-queue fallback (§3.8)."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
