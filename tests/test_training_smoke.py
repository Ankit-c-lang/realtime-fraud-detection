"""Training end to end on tiny data, and the leakage rules around it (PLAN §7.1, §13).

Marked slow because it fits real models. The point is not the metrics, which are
meaningless at this size, but that the wiring holds: the right splits reach the right
stage, the category levels come from config, and nothing is reweighted.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from fraud.features.spec import CATEGORICAL_FEATURES, HOT_FEATURE_NAMES, category_levels
from fraud.modeling import train_xgb
from fraud.modeling.metrics import evaluate
from fraud.modeling.splits import target

pytestmark = pytest.mark.slow


def _frame(n: int, seed: int) -> pd.DataFrame:
    """A small separable dataset, so a working pipeline scores well above chance."""
    rng = np.random.default_rng(seed)
    fraud = rng.random(n) < 0.15

    data: dict[str, Any] = dict.fromkeys(HOT_FEATURE_NAMES, 0.0)
    frame = pd.DataFrame({name: np.zeros(n) for name in data})
    frame["merchant_category"] = rng.choice(list(category_levels()), n)

    # Two honest signals plus noise, so the model has something real to find.
    frame["acct_cnt_5m"] = np.where(fraud, rng.integers(5, 20, n), rng.integers(0, 2, n))
    frame["geo_speed_kmh"] = np.where(fraud, rng.uniform(900, 5000, n), rng.uniform(0, 50, n))
    frame["log_amount"] = rng.normal(6, 1, n)

    frame["is_fraud"] = fraud.astype(int)
    frame["fraud_type"] = np.where(fraud, "VELOCITY", "NONE")
    frame["amount"] = rng.uniform(100, 5000, n)
    return frame


@pytest.fixture(scope="module")
def splits() -> dict[str, pd.DataFrame]:
    return {
        "train": _frame(1500, 0),
        "early_stop": _frame(500, 1),
        "valid": _frame(500, 2),
    }


def test_the_category_column_is_typed_from_config(splits: dict[str, pd.DataFrame]) -> None:
    """Leakage rule L7: codes come from config, never inferred from the split."""
    matrix = train_xgb.as_model_input(splits["train"])

    for column in CATEGORICAL_FEATURES:
        assert isinstance(matrix[column].dtype, pd.CategoricalDtype)
        assert list(matrix[column].cat.categories) == list(category_levels())


def test_the_model_input_is_spec_order_and_nothing_else(
    splits: dict[str, pd.DataFrame],
) -> None:
    matrix = train_xgb.as_model_input(splits["train"])
    assert tuple(matrix.columns) == HOT_FEATURE_NAMES
    assert not {"is_fraud", "fraud_type", "amount"} & set(matrix.columns)


def test_there_are_no_missing_values_to_impute(splits: dict[str, pd.DataFrame]) -> None:
    """PLAN §7.2: the engine's defaults mean no NaNs. Assert rather than impute."""
    assert not train_xgb.as_model_input(splits["train"]).isna().any().any()


def test_a_fitted_model_learns_something(splits: dict[str, pd.DataFrame]) -> None:
    model = train_xgb.fit(
        splits["train"], splits["early_stop"], {"max_depth": 4, "learning_rate": 0.1}, n_jobs=2
    )
    scores = train_xgb.predict(model, splits["valid"])
    result = evaluate(
        target(splits["valid"]).to_numpy(),
        scores,
        fraud_type=splits["valid"]["fraud_type"].to_numpy(),
        budget=0.2,
    )
    assert result.pr_auc > 0.5


def test_training_is_never_reweighted(splits: dict[str, pd.DataFrame]) -> None:
    """PLAN §7.2: reweighting distorts the probability the blend depends on."""
    model = train_xgb.fit(splits["train"], splits["early_stop"], {"max_depth": 3}, n_jobs=2)
    assert getattr(model, "scale_pos_weight", None) in (None, 1, 1.0)


def test_early_stopping_uses_the_early_stop_split(splits: dict[str, pd.DataFrame]) -> None:
    """Three splits, three jobs: fit, stop, select. Never one split for two of them."""
    model = train_xgb.fit(
        splits["train"], splits["early_stop"], {"max_depth": 4, "learning_rate": 0.2}, n_jobs=2
    )
    assert model.best_iteration is not None
    assert model.best_iteration < 2000


def test_the_search_is_reproducible() -> None:
    """Same seed, same draws, or the search stops being an experiment."""
    grid = {"max_depth": [4, 6, 8], "learning_rate": [0.03, 0.05, 0.1]}
    assert train_xgb.sample_grid(5, 42, grid) == train_xgb.sample_grid(5, 42, grid)
    assert train_xgb.sample_grid(5, 42, grid) != train_xgb.sample_grid(5, 7, grid)


def test_the_search_draws_are_distinct() -> None:
    grid = {"max_depth": [4, 6, 8], "learning_rate": [0.03, 0.05, 0.1], "reg_lambda": [1, 5]}
    draws = train_xgb.sample_grid(10, 42, grid)
    keys = {tuple(sorted(draw.items())) for draw in draws}
    assert len(keys) == len(draws)


def test_the_search_picks_the_best_valid_score(splits: dict[str, pd.DataFrame]) -> None:
    import fraud.modeling.train_xgb as module

    original = module.load_yaml
    module.load_yaml = lambda name: (
        {**original(name), "xgboost": {**original(name)["xgboost"], "search_trials": 3}}
        if name == "model"
        else original(name)
    )
    try:
        result = train_xgb.search(splits["train"], splits["early_stop"], splits["valid"], n_jobs=2)
    finally:
        module.load_yaml = original
    assert result.best.valid_pr_auc == max(trial.valid_pr_auc for trial in result.trials)
    assert result.best.params in [trial.params for trial in result.trials]
