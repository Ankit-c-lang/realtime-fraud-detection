"""The only code that turns features into decisions (PLAN §8, §16 responsibility rules).

The scorer, the API and the re-score check all call this class. That is not tidiness: if
any of them computed a score its own way, the re-score check in §9.6 would be comparing
two implementations rather than verifying one, and a serving bug could hide behind an
offline path that never ran it.

**Startup refuses rather than degrades.** A feature spec that has moved on, or a
scikit-learn whose pickle format has shifted, produces a model that still returns
plausible-looking numbers. Those are the worst kind of wrong, so the checks below stop
the process instead. A scorer that will not start is a page; a scorer quietly scoring
against the wrong feature order is an incident nobody notices for a week.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from fraud.config import Settings
from fraud.features.spec import CATEGORICAL_FEATURES, FEATURE_SPEC_VERSION
from fraud.modeling import artifacts, blend
from fraud.modeling.artifacts import Artifacts
from fraud.modeling.decisions import decide
from fraud.scoring import reasons
from fraud.scoring.reasons import Reason

logger = logging.getLogger(__name__)

MODEL_VERSION_ENV: Final[str] = "MODEL_VERSION"
# Pickles are version-sensitive, so major.minor has to match; patch releases do not
# change the format and pinning them would make the artifact needlessly brittle (§8).
PINNED_LIBRARIES: Final[tuple[str, ...]] = ("xgboost", "scikit-learn")


class ModelCompatibilityError(RuntimeError):
    """Raised at startup when an artifact does not match the running code."""


@dataclass(frozen=True, slots=True)
class ScoredBatch:
    """One batch of scores, in the shape the scored-row schema wants (§9.2)."""

    p_xgb: np.ndarray
    anomaly_pct: np.ndarray
    risk: np.ndarray
    decision: np.ndarray
    reasons: list[list[Reason]]
    model_version: str
    feature_spec_version: str

    def __len__(self) -> int:
        return len(self.risk)

    def to_frame(self) -> pd.DataFrame:
        import json

        return pd.DataFrame(
            {
                "p_xgb": self.p_xgb,
                "anomaly_pct": self.anomaly_pct,
                "risk": self.risk,
                "decision": self.decision,
                "reasons": [
                    json.dumps([reason.to_dict() for reason in row]) for row in self.reasons
                ],
                "model_version": self.model_version,
                "feature_spec_version": self.feature_spec_version,
            }
        )


def _minor(version: str) -> str:
    return ".".join(str(version).split(".")[:2])


class RiskModel:
    """XGBoost + Isolation Forest + blend + decision, behind one call."""

    def __init__(self, bundle: Artifacts) -> None:
        self._bundle = bundle
        self._levels = pd.CategoricalDtype(categories=list(bundle.metadata["categorical_levels"]))
        self._feature_names: tuple[str, ...] = tuple(bundle.metadata["feature_names"])

    @classmethod
    def load(
        cls, version: str | None = None, *, model_dir: Path | None = None, check: bool = True
    ) -> RiskModel:
        """Load ``MODEL_VERSION``, else ``models/CURRENT`` (§8 loading rules)."""
        settings = Settings.from_env()
        root = model_dir or settings.model_dir
        chosen = version or os.environ.get(MODEL_VERSION_ENV) or _current(root)

        directory = root / chosen
        if not directory.is_dir():
            raise FileNotFoundError(
                f"no model folder at {directory}. Build one with `make train V={chosen}`."
            )

        bundle = artifacts.load(directory)
        if check:
            _check_compatibility(bundle)
        logger.info(
            "loaded model %s (spec %s, w=%g, review %.4f, hold %s)",
            bundle.model_version,
            bundle.metadata["feature_spec_version"],
            bundle.blend_weight,
            bundle.thresholds.review,
            "disabled" if bundle.thresholds.hold is None else f"{bundle.thresholds.hold:.4f}",
        )
        return cls(bundle)

    @property
    def bundle(self) -> Artifacts:
        return self._bundle

    @property
    def model_version(self) -> str:
        return self._bundle.model_version

    @property
    def blend_weight(self) -> float:
        return self._bundle.blend_weight

    def model_input(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Features in the artifact's own order, with the artifact's category levels.

        Both come from the metadata rather than from the running code, so a frame whose
        columns drifted cannot be silently reordered into a plausible-looking score (L7,
        L8).
        """
        missing = [name for name in self._feature_names if name not in frame.columns]
        if missing:
            raise KeyError(f"feature columns missing from the frame: {missing}")

        matrix = frame[list(self._feature_names)].copy()
        # Which columns are categorical is part of the feature spec, and the spec version
        # was already checked at load, so the running list is the artifact's list.
        for column in CATEGORICAL_FEATURES:
            if column in matrix.columns:
                matrix[column] = matrix[column].astype(self._levels)
        return matrix

    def score_batch(self, frame: pd.DataFrame, *, explain: bool = True) -> ScoredBatch:
        """Probability, anomaly percentile, blended risk and decision, vectorised."""
        matrix = self.model_input(frame)
        bundle = self._bundle

        margin = reasons.margins(bundle.booster, matrix)
        # binary:logistic, so the probability is the logistic of the margin. Computing it
        # from the margin rather than a second predict call keeps the reasons and the
        # score provably consistent: both come from the same numbers.
        p_xgb = 1.0 / (1.0 + np.exp(-margin))

        anomaly = bundle.detector.percentile(frame)
        risk = blend.blend(p_xgb, anomaly, bundle.blend_weight)
        decisions = decide(risk, bundle.thresholds)

        explanations: list[list[Reason]] = [[] for _ in range(len(risk))]
        if explain:
            alerts = decisions != "ALLOW"
            if alerts.any():
                # Contributions are the expensive part, so only alerts are explained:
                # §7.10 only ever shows reasons for REVIEW and HOLD rows.
                rows = np.flatnonzero(alerts)
                subset = matrix.iloc[rows]
                explained = reasons.explain(
                    reasons.contributions(bundle.booster, subset),
                    subset,
                    anomaly[rows],
                    bundle.detector.top_features(frame.iloc[rows]),
                )
                for position, row in enumerate(rows):
                    explanations[row] = explained[position]

        return ScoredBatch(
            p_xgb=p_xgb,
            anomaly_pct=anomaly,
            risk=risk,
            decision=decisions,
            reasons=explanations,
            model_version=bundle.model_version,
            feature_spec_version=str(bundle.metadata["feature_spec_version"]),
        )


def _current(model_dir: Path) -> str:
    path = model_dir / artifacts.CURRENT_FILE
    if not path.is_file():
        raise FileNotFoundError(
            f"no {path}. Build a model with `make train V=v1`, which writes it (PLAN §8)."
        )
    return path.read_text(encoding="utf-8").strip()


def _check_compatibility(bundle: Artifacts) -> None:
    """Refuse to start on a mismatch (§8 loading rules)."""
    stored_spec = str(bundle.metadata["feature_spec_version"])
    if stored_spec != FEATURE_SPEC_VERSION:
        raise ModelCompatibilityError(
            f"{bundle.directory} was built against feature spec {stored_spec}, the code "
            f"is on {FEATURE_SPEC_VERSION}. The features no longer mean the same thing; "
            "rebuild the model rather than scoring against it."
        )

    running = artifacts.library_versions()
    stored: dict[str, Any] = bundle.metadata.get("library_versions", {})
    for library in PINNED_LIBRARIES:
        was, now = stored.get(library), running.get(library)
        if was is None or now is None:
            continue
        if _minor(was) != _minor(now):
            raise ModelCompatibilityError(
                f"{bundle.directory} was built with {library} {was}, this process has "
                f"{now}. Pickled estimators are version-sensitive (PLAN §8); rebuild the "
                "model or pin the library back."
            )
