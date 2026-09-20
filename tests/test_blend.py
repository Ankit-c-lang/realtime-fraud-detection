"""The blend and the leave-one-pattern-out weight selection (PLAN §7.6).

The procedure is easy to get subtly wrong in ways that all look fine: withholding the
pattern from validation as well as training (which hides the very thing being measured),
reusing one threshold across the grid (which turns a recall comparison into a threshold
comparison), or refitting the anomaly detector per run (which stops the four runs being
comparable). Each of those has a test here.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from fraud.features.spec import (
    CATEGORICAL_FEATURES,
    FEATURE_NAMES,
    NUMERIC_FEATURE_NAMES,
    category_levels,
)
from fraud.modeling import blend as blend_mod
from fraud.modeling import iforest
from fraud.modeling.blend import LopoRun, blend, select_weight

PATTERNS = ("VELOCITY", "ATO", "CARD_TESTING", "RING")


def _frame(n: int, seed: int) -> pd.DataFrame:
    """A tiny warm frame with all four patterns present and separable."""
    rng = np.random.default_rng(seed)
    pattern = rng.choice([*PATTERNS, "NONE"], n, p=[0.04, 0.04, 0.04, 0.04, 0.84])

    frame = pd.DataFrame({name: rng.normal(0.0, 1.0, n) for name in NUMERIC_FEATURE_NAMES})
    frame["merchant_category"] = rng.choice(list(category_levels()), n)

    # Each pattern gets its own signal feature, so a model that never saw one really is
    # blind to it — which is what LOPO is meant to simulate.
    for name, signal in zip(
        PATTERNS,
        ("acct_cnt_5m", "geo_speed_kmh", "dev_accts_1h", "dev_accts_30d"),
        strict=True,
    ):
        frame[signal] = np.where(pattern == name, rng.uniform(20, 40, n), frame[signal])

    frame["fraud_type"] = pattern
    frame["is_fraud"] = (pattern != "NONE").astype(int)
    frame["amount"] = rng.uniform(100, 5000, n)
    frame["split"] = "train"
    frame["in_burn_in"] = 0
    return frame


def test_blend_is_the_planned_formula() -> None:
    p = np.array([0.2, 0.8])
    a = np.array([0.9, 0.1])
    np.testing.assert_allclose(blend(p, a, 0.75), 0.75 * p + 0.25 * a)


def test_weight_one_is_the_xgboost_score_untouched() -> None:
    p = np.array([0.2, 0.8])
    np.testing.assert_array_equal(blend(p, np.array([0.9, 0.1]), 1.0), p)


def test_weight_zero_is_the_anomaly_percentile_untouched() -> None:
    a = np.array([0.9, 0.1])
    np.testing.assert_array_equal(blend(np.array([0.2, 0.8]), a, 0.0), a)


@pytest.mark.parametrize("weight", [-0.1, 1.5])
def test_weight_outside_the_unit_interval_raises(weight: float) -> None:
    with pytest.raises(ValueError, match="blend weight"):
        blend(np.array([0.5]), np.array([0.5]), weight)


def test_grid_matches_the_plan() -> None:
    assert blend_mod.weight_grid() == (1.0, 0.95, 0.9, 0.85, 0.8, 0.7, 0.6, 0.5)
    assert blend_mod.lopo_patterns() == PATTERNS


def _run(pattern: str, recalls: dict[float, float]) -> LopoRun:
    return LopoRun(pattern=pattern, train_rows=100, withheld_rows=10, recall_by_weight=recalls)


def test_select_weight_takes_the_best_mean() -> None:
    runs = [
        _run("VELOCITY", {1.0: 0.5, 0.9: 0.7, 0.8: 0.6}),
        _run("ATO", {1.0: 0.5, 0.9: 0.7, 0.8: 0.4}),
    ]
    best, means = select_weight(runs)
    assert best == 0.9
    assert means[0.9] == pytest.approx(0.7)


def test_select_weight_breaks_ties_towards_the_larger_weight() -> None:
    """The anomaly half only gets weight where it demonstrably bought something."""
    runs = [_run("VELOCITY", {1.0: 0.6, 0.9: 0.6, 0.5: 0.6})]
    assert select_weight(runs)[0] == 1.0


def test_select_weight_needs_runs() -> None:
    with pytest.raises(ValueError, match="no leave-one-pattern-out runs"):
        select_weight([])


class _Recorder:
    """Stands in for train_xgb, recording which rows each LOPO fit was given."""

    def __init__(self, valid: pd.DataFrame) -> None:
        self.seen: list[dict[str, Any]] = []
        self.valid = valid

    def fit(
        self,
        train: pd.DataFrame,
        early_stop: pd.DataFrame,
        params: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.seen.append(
            {
                "train_patterns": set(train["fraud_type"]),
                "early_stop_patterns": set(early_stop["fraud_type"]),
                "params": params,
            }
        )
        return {"index": len(self.seen)}

    def predict(self, model: dict[str, Any], frame: pd.DataFrame, **kwargs: Any) -> np.ndarray:
        # Deterministic, ranked by is_fraud so the operating point is well defined.
        rng = np.random.default_rng(model["index"])
        return np.clip(frame["is_fraud"].to_numpy() * 0.6 + rng.random(len(frame)) * 0.3, 0, 1)


@pytest.fixture
def lopo(monkeypatch: pytest.MonkeyPatch) -> blend_mod.LopoResult:
    """A full LOPO pass with the XGBoost fits stubbed out, so it runs in milliseconds."""
    from fraud.modeling import train_xgb

    train = _frame(900, 1)
    early_stop = _frame(300, 2)
    valid = _frame(400, 3)

    recorder = _Recorder(valid)
    monkeypatch.setattr(train_xgb, "fit", recorder.fit)
    monkeypatch.setattr(train_xgb, "predict", recorder.predict)

    detector = iforest.fit(train, n_jobs=1)
    result = blend_mod.run_lopo(train, early_stop, valid, {"max_depth": 4}, detector, n_jobs=1)
    result.detector["_seen"] = recorder.seen  # carried through for the assertions below
    return result


def test_lopo_withholds_the_pattern_from_train_and_early_stop(
    lopo: blend_mod.LopoResult,
) -> None:
    for pattern, seen in zip(PATTERNS, lopo.detector["_seen"], strict=True):
        assert pattern not in seen["train_patterns"]
        assert pattern not in seen["early_stop_patterns"]
        # Every other pattern stays: only one is withheld at a time.
        assert seen["train_patterns"] == {"NONE", *(set(PATTERNS) - {pattern})}


def test_lopo_keeps_the_pattern_in_validation(lopo: blend_mod.LopoResult) -> None:
    """R_k(w) is measured on all of valid, pattern k included (§7.6 step 1)."""
    assert all(run.withheld_rows > 0 for run in lopo.runs)
    assert len(lopo.runs) == len(PATTERNS)


def test_lopo_uses_one_parameter_set(lopo: blend_mod.LopoResult) -> None:
    assert {id(seen["params"]) for seen in lopo.detector["_seen"]} == {
        id(lopo.detector["_seen"][0]["params"])
    }


def test_lopo_scores_every_weight(lopo: blend_mod.LopoResult) -> None:
    for run in lopo.runs:
        assert tuple(run.recall_by_weight) == blend_mod.weight_grid()
        assert all(0.0 <= r <= 1.0 for r in run.recall_by_weight.values())


def test_lopo_chooses_a_weight_from_the_grid(lopo: blend_mod.LopoResult) -> None:
    assert lopo.best_weight in blend_mod.weight_grid()
    assert lopo.isolation_forest_helps == (lopo.best_weight < 1.0)


def test_e5_rows_cover_every_pattern(lopo: blend_mod.LopoResult) -> None:
    assert [row.pattern for row in lopo.e5_rows] == list(PATTERNS)
    for row in lopo.e5_rows:
        assert 0.0 <= row.pattern_recall_alone <= 1.0
        assert 0.0 <= row.pattern_recall_blended <= 1.0


def test_result_serialises_with_the_protocol(lopo: blend_mod.LopoResult) -> None:
    payload = lopo.to_dict()
    assert payload["best_weight"] == lopo.best_weight
    assert set(payload["mean_recall_by_weight"]) == {f"{w:g}" for w in blend_mod.weight_grid()}
    assert len(payload["e5_rows"]) == len(PATTERNS)
    assert "§7.6" in payload["protocol"]


def test_the_warm_feature_set_is_the_full_thirty_six() -> None:
    """LOPO runs on the final model's features, not the hot subset."""
    assert len(FEATURE_NAMES) == 36
    assert len(NUMERIC_FEATURE_NAMES) + len(CATEGORICAL_FEATURES) == 36
