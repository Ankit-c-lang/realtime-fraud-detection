"""Reason codes and their faithfulness to the model (PLAN §7.10).

The headline test is additivity: XGBoost's contributions must sum to the margin the
model actually predicted. If they ever stop doing that, the reasons on an alert are
describing a different model than the one that raised it, which is worse than showing an
analyst nothing at all.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from fraud.config import load_yaml
from fraud.features.spec import CATEGORICAL_FEATURES, FEATURE_NAMES, category_levels
from fraud.scoring import reasons
from fraud.scoring.reasons import Reason


def _matrix(n: int = 300, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {name: rng.normal(0.0, 1.0, n) for name in FEATURE_NAMES if name != "merchant_category"}
    )
    frame["acct_cnt_5m"] = rng.integers(0, 12, n).astype(float)
    frame["geo_speed_kmh"] = rng.uniform(0, 4000, n)
    levels = pd.CategoricalDtype(categories=list(category_levels()))
    frame["merchant_category"] = pd.Series(rng.choice(list(category_levels()), n)).astype(levels)
    return frame[list(FEATURE_NAMES)]


@pytest.fixture(scope="module")
def fitted() -> tuple[xgb.Booster, pd.DataFrame]:
    matrix = _matrix()
    # Two honest signals, so the model has something real to attribute.
    y = ((matrix["acct_cnt_5m"] >= 6) & (matrix["geo_speed_kmh"] > 2000)).astype(int)
    model = xgb.XGBClassifier(
        n_estimators=40,
        max_depth=3,
        enable_categorical=True,
        tree_method="hist",
        random_state=42,
    )
    model.fit(matrix, y)
    return model.get_booster(), matrix


def test_contributions_sum_to_the_margin(fitted: tuple[xgb.Booster, pd.DataFrame]) -> None:
    """PLAN §7.10: within 1e-4. This is what "faithful" means here."""
    booster, matrix = fitted
    assert reasons.check_additivity(booster, matrix) <= 1e-4


def test_additivity_holds_with_the_categorical_column(
    fitted: tuple[xgb.Booster, pd.DataFrame],
) -> None:
    """§7.10's explicit check: if this fails, one-hot encode and bump the spec version."""
    booster, matrix = fitted
    assert matrix["merchant_category"].dtype.name == "category"
    contribs = reasons.contributions(booster, matrix)
    assert contribs.shape == (len(matrix), len(FEATURE_NAMES) + 1)
    np.testing.assert_allclose(contribs.sum(axis=1), reasons.margins(booster, matrix), atol=1e-4)


def test_additivity_failure_is_loud() -> None:
    """A silent mismatch would be the dangerous outcome, so it raises."""

    class _Wrong:
        def predict(self, _: object, **kwargs: object) -> np.ndarray:
            if kwargs.get("pred_contribs"):
                return np.zeros((2, len(FEATURE_NAMES) + 1))
            return np.array([5.0, 5.0])

    with pytest.raises(AssertionError, match="do not sum to the model margin"):
        reasons.check_additivity(_Wrong(), _matrix(2))  # type: ignore[arg-type]


def test_every_feature_has_a_template() -> None:
    """A feature with no text could still reach an alert and render as nothing."""
    assert set(reasons.templates()) == set(FEATURE_NAMES)
    assert len(FEATURE_NAMES) == 36


def test_every_template_renders() -> None:
    for feature in FEATURE_NAMES:
        value = "grocery" if feature in CATEGORICAL_FEATURES else 7.0
        text = reasons.render(feature, value)
        assert text and "{" not in text


def test_labels_are_human_readable() -> None:
    """Labels feed the anomaly reason, so they must not be raw column names."""
    for feature in FEATURE_NAMES:
        assert reasons.label(feature) != feature


def test_log_amount_is_rendered_in_rupees() -> None:
    """The model sees log1p(amount); an analyst must not."""
    assert reasons.render("log_amount", float(np.log1p(2500))) == "Payment of ₹2,500"


def test_only_positive_contributions_become_reasons(
    fitted: tuple[xgb.Booster, pd.DataFrame],
) -> None:
    """A feature arguing for innocence is not why the transaction was flagged."""
    booster, matrix = fitted
    contribs = reasons.contributions(booster, matrix)
    for row in reasons.top_reasons(contribs, matrix):
        assert all(reason.contribution > 0 for reason in row)


def test_reasons_are_ranked_by_contribution(
    fitted: tuple[xgb.Booster, pd.DataFrame],
) -> None:
    booster, matrix = fitted
    for row in reasons.top_reasons(reasons.contributions(booster, matrix), matrix):
        weights = [reason.contribution for reason in row]
        assert weights == sorted(weights, reverse=True)


def test_at_most_top_n_reasons(fitted: tuple[xgb.Booster, pd.DataFrame]) -> None:
    booster, matrix = fitted
    assert reasons.top_n() == 3
    assert all(
        len(row) <= 3 for row in reasons.top_reasons(reasons.contributions(booster, matrix), matrix)
    )


def test_the_bias_column_never_becomes_a_reason(
    fitted: tuple[xgb.Booster, pd.DataFrame],
) -> None:
    """The bias is the base rate: identical on every row and no help to an analyst."""
    booster, matrix = fitted
    named = {
        reason.feature
        for row in reasons.top_reasons(reasons.contributions(booster, matrix), matrix)
        for reason in row
    }
    assert named <= set(FEATURE_NAMES)


def test_reasons_name_the_features_the_model_used(
    fitted: tuple[xgb.Booster, pd.DataFrame],
) -> None:
    """The two planted signals should dominate the explanations."""
    booster, matrix = fitted
    rows = reasons.top_reasons(reasons.contributions(booster, matrix), matrix)
    named = [reason.feature for row in rows for reason in row]
    assert {"acct_cnt_5m", "geo_speed_kmh"} & set(named)


def test_anomaly_reason_only_above_the_threshold() -> None:
    threshold = float(load_yaml("reason_codes")["anomaly"]["threshold"])
    assert threshold == 0.99
    assert reasons.anomaly_reason(0.98, ("acct_cnt_5m", "geo_speed_kmh")) is None

    reason = reasons.anomaly_reason(0.995, ("acct_cnt_5m", "geo_speed_kmh"))
    assert reason is not None
    assert reason.text == (
        "Unusual combination: payments in 5 minutes and implied travel speed "
        "far outside typical values"
    )


def test_anomaly_reason_needs_two_features() -> None:
    assert reasons.anomaly_reason(1.0, ("acct_cnt_5m",)) is None


def test_explain_appends_the_anomaly_reason(
    fitted: tuple[xgb.Booster, pd.DataFrame],
) -> None:
    booster, matrix = fitted
    sample = matrix.iloc[:4]
    contribs = reasons.contributions(booster, sample)
    pcts = np.array([0.0, 1.0, 0.0, 1.0])
    deviating = [("acct_cnt_5m", "geo_speed_kmh")] * 4

    rows = reasons.explain(contribs, sample, pcts, deviating)
    flagged = [any(reason.feature == "anomaly_pct" for reason in row) for row in rows]
    assert flagged == [False, True, False, True]


def test_reason_serialises() -> None:
    payload = Reason("acct_cnt_5m", 7.0, 1.25, "7 payments in the previous 5 minutes").to_dict()
    assert payload == {
        "feature": "acct_cnt_5m",
        "value": 7.0,
        "contribution": 1.25,
        "text": "7 payments in the previous 5 minutes",
    }
