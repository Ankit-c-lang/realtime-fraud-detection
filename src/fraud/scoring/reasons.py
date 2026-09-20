"""Reason codes from the model's own contributions (PLAN §7.10).

An explanation is only worth showing an analyst if it is the model's actual arithmetic.
XGBoost's native ``pred_contribs`` gives exactly that: one number per feature plus a
bias, summing to the log-odds margin the model predicted. A test asserts that sum to
1e-4, which is what makes "faithful" a claim rather than a hope.

The `shap` package is deliberately not in the serving path (§3.8, A8). It is a heavy
dependency that would explain only the XGBoost half of the score anyway; the anomaly
half gets its own reason from the Isolation Forest's feature statistics.

Only **positive** contributions become reasons. A feature that pushed the score *down*
is not why the transaction was flagged, and listing it would leave an analyst reading
evidence for innocence under a heading that says "risk".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

import numpy as np
import pandas as pd
import xgboost as xgb

from fraud.config import load_yaml
from fraud.features.spec import CATEGORICAL_FEATURES, FEATURE_NAMES

logger = logging.getLogger(__name__)

MARGIN_TOLERANCE: Final[float] = 1e-4

_TRANSFORMS: Final[dict[str, Any]] = {"expm1": np.expm1}


@dataclass(frozen=True, slots=True)
class Reason:
    """One line on an alert, traceable to the number that produced it."""

    feature: str
    value: float | str
    contribution: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "value": self.value,
            "contribution": round(float(self.contribution), 6),
            "text": self.text,
        }


@lru_cache(maxsize=1)
def templates() -> dict[str, dict[str, Any]]:
    """The reason templates, checked against the feature spec on first use.

    Loaded once and validated here rather than at each call: a missing template is a
    packaging mistake that should stop the process, not degrade one alert into a blank.
    """
    config = load_yaml("reason_codes")
    entries = config["features"]

    missing = [name for name in FEATURE_NAMES if name not in entries]
    extra = [name for name in entries if name not in FEATURE_NAMES]
    if missing or extra:
        raise ValueError(
            "configs/reason_codes.yaml must cover exactly the features in spec.py "
            f"(PLAN §7.10). Missing: {missing}. Unknown: {extra}."
        )
    return entries


@lru_cache(maxsize=1)
def anomaly_config() -> dict[str, Any]:
    return load_yaml("reason_codes")["anomaly"]


@lru_cache(maxsize=1)
def top_n() -> int:
    return int(load_yaml("reason_codes")["top_n"])


def label(feature: str) -> str:
    return str(templates()[feature]["label"])


def render(feature: str, value: float | str) -> str:
    """Fill one template.

    A template need not mention ``{value}``: a boolean flag reads better as a plain
    statement than as "new_device = 1.0".
    """
    entry = templates()[feature]
    shown = value
    transform = entry.get("transform")
    if transform is not None and not isinstance(value, str):
        shown = float(_TRANSFORMS[transform](value))
    return str(entry["template"]).format(value=shown)


def contributions(booster: xgb.Booster, matrix: pd.DataFrame) -> np.ndarray:
    """Per-feature log-odds contributions, shape ``(n, n_features + 1)``.

    The final column is the bias. Each row sums to the model's margin, which
    ``check_additivity`` asserts.
    """
    dmatrix = xgb.DMatrix(matrix, enable_categorical=True)
    return np.asarray(booster.predict(dmatrix, pred_contribs=True))


def margins(booster: xgb.Booster, matrix: pd.DataFrame) -> np.ndarray:
    dmatrix = xgb.DMatrix(matrix, enable_categorical=True)
    return np.asarray(booster.predict(dmatrix, output_margin=True))


def check_additivity(
    booster: xgb.Booster, matrix: pd.DataFrame, tolerance: float = MARGIN_TOLERANCE
) -> float:
    """Assert that the explanation path matches the model (PLAN §7.10).

    Returns the largest absolute discrepancy so a caller can log it. If this ever fails,
    the reasons are describing a different model than the one making the decision, and
    showing them to an analyst would be worse than showing nothing.
    """
    difference = np.abs(contributions(booster, matrix).sum(axis=1) - margins(booster, matrix))
    worst = float(difference.max()) if len(difference) else 0.0
    if worst > tolerance:
        raise AssertionError(
            f"pred_contribs do not sum to the model margin: max |diff| = {worst:.3e} "
            f"exceeds {tolerance:.0e}. The reason codes would not be faithful (PLAN §7.10). "
            "If this is the categorical column, one-hot encode merchant_category and bump "
            "FEATURE_SPEC_VERSION."
        )
    return worst


def top_reasons(
    contribs: np.ndarray,
    matrix: pd.DataFrame,
    *,
    count: int | None = None,
) -> list[list[Reason]]:
    """The ``count`` largest **positive** contributions per row (PLAN §7.10).

    Rows whose every contribution is negative get an empty list rather than filler. That
    happens for low-risk rows, which are not alerts and are never explained anyway.
    """
    wanted = top_n() if count is None else count
    names = list(matrix.columns)
    # Drop the bias column: it is the base rate, identical for every row, and naming it
    # as a reason would tell an analyst nothing about this transaction.
    values = np.asarray(contribs)[:, : len(names)]

    order = np.argsort(values, axis=1)[:, ::-1][:, :wanted]
    reasons: list[list[Reason]] = []
    for row, columns in enumerate(order):
        picked: list[Reason] = []
        for column in columns:
            weight = float(values[row, column])
            if weight <= 0.0:
                break  # sorted descending, so everything after this is negative too
            feature = names[column]
            raw = matrix.iat[row, column]
            shown = str(raw) if feature in CATEGORICAL_FEATURES else float(raw)
            picked.append(
                Reason(
                    feature=feature,
                    value=shown,
                    contribution=weight,
                    text=render(feature, shown),
                )
            )
        reasons.append(picked)
    return reasons


def anomaly_reason(anomaly_pct: float, deviating: tuple[str, ...]) -> Reason | None:
    """The extra reason for rows the Isolation Forest finds strange (§7.5, §7.10).

    This is the only reason not derived from XGBoost, which is the point: it is how an
    alert raised by the unsupervised half explains itself.
    """
    config = anomaly_config()
    if anomaly_pct < float(config["threshold"]) or len(deviating) < 2:
        return None
    return Reason(
        feature="anomaly_pct",
        value=float(anomaly_pct),
        contribution=float(anomaly_pct),
        text=str(config["template"]).format(first=label(deviating[0]), second=label(deviating[1])),
    )


def explain(
    contribs: np.ndarray,
    matrix: pd.DataFrame,
    anomaly_pcts: np.ndarray,
    deviating: list[tuple[str, ...]],
    *,
    count: int | None = None,
) -> list[list[Reason]]:
    """Top-N model reasons per row, with the anomaly reason appended where it applies."""
    reasons = top_reasons(contribs, matrix, count=count)
    for row, (pct, features) in enumerate(zip(anomaly_pcts, deviating, strict=True)):
        extra = anomaly_reason(float(pct), features)
        if extra is not None:
            reasons[row].append(extra)
    return reasons
