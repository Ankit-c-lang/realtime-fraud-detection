"""Metrics and operating points, on toy examples (PLAN §7.7, §7.8, §13).

The budget constraint is the part that has to be right. Without it every threshold
search drifts towards alerting on everything, which scores well and is unusable.
"""

from __future__ import annotations

import numpy as np
import pytest

from fraud.modeling.metrics import (
    Evaluation,
    evaluate,
    hold_threshold,
    operating_point,
    pr_auc,
    review_budget,
    roc_auc,
)


def _labels(n: int, positives: list[int]) -> np.ndarray:
    y = np.zeros(n, dtype=int)
    y[positives] = 1
    return y


# --- threshold-free ---------------------------------------------------------


def test_pr_auc_is_one_for_a_perfect_ranking() -> None:
    y = np.array([0, 0, 1, 1])
    assert pr_auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)


def test_pr_auc_matches_prevalence_for_a_useless_score() -> None:
    """A constant score ranks nothing, so average precision falls to the base rate."""
    y = _labels(100, list(range(10)))
    assert pr_auc(y, np.full(100, 0.5)) == pytest.approx(0.1, abs=0.01)


def test_metrics_are_nan_when_a_split_has_one_class() -> None:
    """Better an explicit NaN than a number that looks like a result."""
    y = np.zeros(10, dtype=int)
    assert np.isnan(pr_auc(y, np.linspace(0, 1, 10)))
    assert np.isnan(roc_auc(y, np.linspace(0, 1, 10)))


def test_roc_auc_is_half_for_a_coin_flip() -> None:
    y = np.array([0, 1, 0, 1])
    assert roc_auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == pytest.approx(0.5)


# --- the alert budget (PLAN §7.7) -------------------------------------------


def test_the_operating_point_respects_the_budget() -> None:
    """100 rows, 10 fraud, a 2% budget: at most 2 alerts, whatever F1 would prefer."""
    y = _labels(100, list(range(10)))
    scores = np.linspace(1.0, 0.0, 100)  # perfectly ranked

    point = operating_point(y, scores, budget=0.02)
    assert point.alerts <= 2
    assert point.alert_rate <= 0.02


def test_a_bigger_budget_catches_more() -> None:
    """The trade-off the whole decision policy exists to express."""
    y = _labels(100, list(range(10)))
    scores = np.linspace(1.0, 0.0, 100)

    tight = operating_point(y, scores, budget=0.02)
    loose = operating_point(y, scores, budget=0.20)
    assert loose.recall > tight.recall
    assert loose.alerts > tight.alerts


def test_perfect_ranking_inside_the_budget_is_perfectly_precise() -> None:
    y = _labels(100, list(range(10)))
    scores = np.linspace(1.0, 0.0, 100)

    point = operating_point(y, scores, budget=0.10)
    assert point.precision == pytest.approx(1.0)
    assert point.recall == pytest.approx(1.0)
    assert point.f1 == pytest.approx(1.0)


def test_the_budget_default_comes_from_config() -> None:
    assert review_budget() == pytest.approx(0.02)


def test_an_impossible_budget_yields_no_alerts() -> None:
    y = _labels(10, [0])
    point = operating_point(y, np.linspace(1.0, 0.0, 10), budget=0.0)
    assert point.alerts == 0


# --- the HOLD tier (PLAN §7.7) ----------------------------------------------


def test_hold_requires_both_precision_and_volume() -> None:
    """HOLD freezes a real customer's card, so a handful of hits is not evidence."""
    y = _labels(1000, list(range(100)))
    scores = np.linspace(1.0, 0.0, 1000)

    threshold, precision, alerts = hold_threshold(y, scores, min_precision=0.95, min_alerts=50)
    assert threshold is not None
    assert precision >= 0.95
    assert alerts >= 50


def test_hold_is_disabled_when_nothing_is_precise_enough() -> None:
    """A real outcome, not an error: the README then has to say HOLD is off."""
    rng = np.random.default_rng(0)
    y = _labels(1000, list(range(50)))
    scores = rng.random(1000)  # no signal at all

    threshold, precision, alerts = hold_threshold(y, scores, min_precision=0.95, min_alerts=50)
    assert threshold is None
    assert precision is None
    assert alerts == 0


def test_hold_is_disabled_when_there_are_too_few_alerts() -> None:
    """Only 10 fraud rows exist, so no threshold can reach 50 precise alerts."""
    y = _labels(1000, list(range(10)))
    scores = np.linspace(1.0, 0.0, 1000)

    threshold, _, _ = hold_threshold(y, scores, min_precision=0.95, min_alerts=50)
    assert threshold is None


def test_hold_picks_the_lowest_qualifying_threshold() -> None:
    """Lowest, so HOLD covers as much as it can while staying precise."""
    y = _labels(1000, list(range(200)))
    scores = np.linspace(1.0, 0.0, 1000)

    strict, _, few = hold_threshold(y, scores, min_precision=0.99, min_alerts=50)
    relaxed, _, many = hold_threshold(y, scores, min_precision=0.95, min_alerts=50)
    assert relaxed <= strict
    assert many >= few


# --- the full evaluation (PLAN §7.8) ----------------------------------------


def test_evaluation_reports_every_required_metric() -> None:
    y = _labels(1000, list(range(20)))
    scores = np.linspace(1.0, 0.0, 1000)
    result = evaluate(y, scores)

    assert isinstance(result, Evaluation)
    payload = result.to_dict()
    for key in (
        "pr_auc",
        "roc_auc",
        "precision",
        "recall",
        "f1",
        "false_positive_rate",
        "alert_rate",
        "value_detection_rate",
        "recall_by_pattern",
        "hold",
    ):
        assert key in payload


def test_false_positive_rate_is_over_legitimate_transactions() -> None:
    """Not over all rows: at 1.7% prevalence the two differ enough to mislead."""
    y = _labels(100, list(range(10)))
    scores = np.zeros(100)
    scores[:10] = 1.0  # perfect
    result = evaluate(y, scores, budget=0.5)

    assert result.false_positive_rate == pytest.approx(0.0)
    assert result.precision == pytest.approx(1.0)


def test_value_detection_rate_weights_by_amount() -> None:
    """Catching one ₹100,000 fraud beats catching ten ₹100 ones."""
    y = np.array([1, 1, 0, 0])
    scores = np.array([0.9, 0.1, 0.2, 0.05])
    amounts = np.array([100_000.0, 100.0, 50.0, 50.0])

    result = evaluate(y, scores, amounts=amounts, budget=0.5)
    assert result.value_detection_rate == pytest.approx(100_000 / 100_100, abs=1e-4)


def test_recall_is_reported_per_pattern() -> None:
    """A blended recall can hide a pattern the model never catches (PLAN §7.9).

    The budget has to bite for that to show: at 0.25 only two of the eight rows can be
    alerted on, so the two RING rows fall below the threshold and their recall is 0 even
    though overall recall looks respectable.
    """
    y = np.array([1, 1, 1, 1, 0, 0, 0, 0])
    scores = np.array([0.9, 0.9, 0.01, 0.01, 0.0, 0.0, 0.0, 0.0])
    fraud_type = np.array(["ATO", "ATO", "RING", "RING", "NONE", "NONE", "NONE", "NONE"])

    result = evaluate(y, scores, fraud_type=fraud_type, budget=0.25)
    assert result.recall_by_pattern == {"ATO": pytest.approx(1.0), "RING": pytest.approx(0.0)}
    assert result.recall == pytest.approx(0.5)


def test_recall_by_pattern_excludes_legitimate_rows() -> None:
    y = np.array([1, 0, 0])
    scores = np.array([0.9, 0.1, 0.1])
    fraud_type = np.array(["VELOCITY", "NONE", "NONE"])

    result = evaluate(y, scores, fraud_type=fraud_type, budget=0.5)
    assert set(result.recall_by_pattern) == {"VELOCITY"}


def test_alert_rate_and_count_agree() -> None:
    y = _labels(1000, list(range(50)))
    scores = np.linspace(1.0, 0.0, 1000)
    result = evaluate(y, scores, budget=0.02)

    assert result.alerts == pytest.approx(result.alert_rate * 1000, abs=1)
    assert result.alert_rate <= 0.02
