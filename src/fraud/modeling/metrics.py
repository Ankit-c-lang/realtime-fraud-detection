"""Metrics and operating points (PLAN §7.7, §7.8).

Accuracy is not here on purpose. At ~1.7% prevalence a model that answers "legitimate"
every time is 98.3% accurate and catches nothing. What matters is how much fraud is
caught inside the analyst capacity, and how precise the HOLD tier is, because HOLD
freezes a real customer's card.

The headline number is PR-AUC (average precision). Everything else is reported at an
operating point chosen under the alert budget, because a threshold that alerts on 20% of
traffic is not a threshold anyone can staff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

from fraud.config import load_yaml

NO_FRAUD = "NONE"


class _Unset:
    """Marker for "no policy supplied, run the exploratory search"."""


UNSET: Final[Any] = _Unset()


@dataclass(frozen=True, slots=True)
class OperatingPoint:
    """A threshold and what it buys at that threshold."""

    threshold: float
    precision: float
    recall: float
    f1: float
    alert_rate: float
    alerts: int


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Everything §7.8 asks for, for one model on one split."""

    pr_auc: float
    roc_auc: float
    threshold: float
    precision: float
    recall: float
    f1: float
    false_positive_rate: float
    alert_rate: float
    alerts: int
    value_detection_rate: float
    recall_by_pattern: dict[str, float] = field(default_factory=dict)
    hold_threshold: float | None = None
    hold_precision: float | None = None
    hold_alerts: int = 0
    # Where the HOLD block came from. "search" is the exploratory question "could this
    # score support a HOLD tier at all"; "policy" is the tier a shipped artifact actually
    # applies. They differ, and a reader has to be able to tell which one they are
    # looking at — see the note on `evaluate_at`.
    hold_source: str = "search"

    @property
    def hold_enabled(self) -> bool:
        return self.hold_threshold is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_auc": self.pr_auc,
            "roc_auc": self.roc_auc,
            "threshold": self.threshold,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "false_positive_rate": self.false_positive_rate,
            "alert_rate": self.alert_rate,
            "alerts": self.alerts,
            "value_detection_rate": self.value_detection_rate,
            "recall_by_pattern": self.recall_by_pattern,
            "hold": {
                "enabled": self.hold_enabled,
                "threshold": self.hold_threshold,
                "precision": self.hold_precision,
                "alerts": self.hold_alerts,
                "source": self.hold_source,
            },
        }


def review_budget() -> float:
    return float(load_yaml("model")["review_budget"])


def pr_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Average precision: the headline metric (PLAN §7.8)."""
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(average_precision_score(y_true, scores))


def roc_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(roc_auc_score(y_true, scores))


def _candidate_thresholds(scores: np.ndarray, limit: int = 512) -> np.ndarray:
    """Distinct score values, thinned so the search stays cheap on 400k rows."""
    unique = np.unique(scores)
    if len(unique) <= limit:
        return unique[::-1]
    return np.quantile(unique, np.linspace(0.0, 1.0, limit))[::-1]


def operating_point(
    y_true: np.ndarray,
    scores: np.ndarray,
    budget: float | None = None,
) -> OperatingPoint:
    """The threshold that maximises F1 while staying inside the alert budget (§7.7).

    The budget is the constraint that makes this honest: without it F1 will happily pick
    a threshold that alerts on far more transactions than anyone could review.
    """
    y_true = np.asarray(y_true)
    scores = np.asarray(scores, dtype=float)
    limit = review_budget() if budget is None else budget
    total = len(scores)
    positives = int(y_true.sum())

    best = OperatingPoint(float("inf"), 0.0, 0.0, 0.0, 0.0, 0)
    for threshold in _candidate_thresholds(scores):
        flagged = scores >= threshold
        alerts = int(flagged.sum())
        if alerts == 0 or alerts / total > limit:
            continue

        hits = int(y_true[flagged].sum())
        precision = hits / alerts
        recall = hits / positives if positives else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        if f1 > best.f1:
            best = OperatingPoint(float(threshold), precision, recall, f1, alerts / total, alerts)

    return best


def hold_threshold(
    y_true: np.ndarray,
    scores: np.ndarray,
    *,
    min_precision: float | None = None,
    min_alerts: int | None = None,
) -> tuple[float | None, float | None, int]:
    """The lowest threshold precise enough to freeze a card on (PLAN §7.7).

    Returns ``(None, None, 0)`` when nothing qualifies, which is a real outcome: HOLD is
    then disabled and the README has to say so rather than quietly lowering the bar.
    """
    config = load_yaml("model")["hold"]
    floor = float(config["min_precision"]) if min_precision is None else min_precision
    minimum = int(config["min_alerts"]) if min_alerts is None else min_alerts

    y_true = np.asarray(y_true)
    scores = np.asarray(scores, dtype=float)

    best: tuple[float | None, float | None, int] = (None, None, 0)
    for threshold in _candidate_thresholds(scores):
        flagged = scores >= threshold
        alerts = int(flagged.sum())
        if alerts < minimum:
            continue
        precision = float(y_true[flagged].sum()) / alerts
        if precision >= floor:
            # Thresholds descend, so the last qualifying one is the lowest.
            best = (float(threshold), precision, alerts)
    return best


def evaluate(
    y_true: np.ndarray,
    scores: np.ndarray,
    *,
    amounts: np.ndarray | None = None,
    fraud_type: np.ndarray | None = None,
    budget: float | None = None,
    hold: float | None | _Unset = UNSET,
) -> Evaluation:
    """The full §7.8 picture, at the best threshold inside the alert budget."""
    point = operating_point(y_true, scores, budget)
    return evaluate_at(
        y_true, scores, point.threshold, amounts=amounts, fraud_type=fraud_type, hold=hold
    )


def evaluate_at(
    y_true: np.ndarray,
    scores: np.ndarray,
    threshold: float,
    *,
    amounts: np.ndarray | None = None,
    fraud_type: np.ndarray | None = None,
    hold: float | None | _Unset = UNSET,
) -> Evaluation:
    """The same picture at a threshold someone else chose.

    The rules baseline needs this: rules fire where they fire, and forcing them through
    a budget-constrained search would flatter them by hiding how much they over-alert.

    ``hold`` decides what the HOLD block means. Left unset, this searches for the best
    HOLD threshold the score could support, which is the right question for a candidate
    model nobody is shipping. Passed explicitly — including as ``None`` — it reports the
    tier the artifact's decision policy actually applies. A shipped model must use the
    second form: reporting a searched HOLD threshold beside a policy that disabled the
    tier tells a reader the system freezes cards when it does not (§7.7, §8).
    """
    y_true = np.asarray(y_true)
    scores = np.asarray(scores, dtype=float)
    flagged = scores >= threshold

    alerts = int(flagged.sum())
    hits = int(y_true[flagged].sum())
    positives = int(y_true.sum())
    precision = hits / alerts if alerts else 0.0
    recall = hits / positives if positives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    point = OperatingPoint(
        float(threshold),
        precision,
        recall,
        f1,
        alerts / len(scores) if len(scores) else 0.0,
        alerts,
    )

    negatives = int((y_true == 0).sum())
    false_alerts = int(((y_true == 0) & flagged).sum())

    value_rate = 0.0
    if amounts is not None:
        amounts = np.asarray(amounts, dtype=float)
        fraud_value = float(amounts[y_true == 1].sum())
        if fraud_value > 0:
            value_rate = float(amounts[(y_true == 1) & flagged].sum()) / fraud_value

    by_pattern: dict[str, float] = {}
    if fraud_type is not None:
        fraud_type = np.asarray(fraud_type)
        for pattern in sorted(set(fraud_type) - {NO_FRAUD}):
            rows = fraud_type == pattern
            if rows.any():
                by_pattern[str(pattern)] = float(flagged[rows].mean())

    if isinstance(hold, _Unset):
        hold_t, hold_p, hold_n = hold_threshold(y_true, scores)
        source = "search"
    elif hold is None:
        hold_t, hold_p, hold_n = None, None, 0
        source = "policy"
    else:
        flagged_hold = scores >= hold
        held = int(flagged_hold.sum())
        hold_t = float(hold)
        hold_p = float(y_true[flagged_hold].sum()) / held if held else None
        hold_n = held
        source = "policy"

    return Evaluation(
        pr_auc=pr_auc(y_true, scores),
        roc_auc=roc_auc(y_true, scores),
        threshold=point.threshold,
        precision=point.precision,
        recall=point.recall,
        f1=point.f1,
        false_positive_rate=false_alerts / negatives if negatives else 0.0,
        alert_rate=point.alert_rate,
        alerts=point.alerts,
        value_detection_rate=value_rate,
        recall_by_pattern=by_pattern,
        hold_threshold=hold_t,
        hold_precision=hold_p,
        hold_alerts=hold_n,
        hold_source=source,
    )
