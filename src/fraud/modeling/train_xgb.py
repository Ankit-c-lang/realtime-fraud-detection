"""XGBoost training and the light random search (PLAN §7.4, experiments E2 and E3).

Three decisions here are load-bearing.

**No reweighting.** ``scale_pos_weight`` stays at 1 even though fraud is under 2% of the
data. The output probability is later blended with an anomaly percentile, and reweighting
distorts that probability so the blend weight stops meaning anything (§3.8, §7.2). The
imbalance is handled where it belongs, by choosing a threshold under the alert budget.

**Three splits, three jobs.** ``train`` fits, ``early_stop`` decides when to stop, and
``valid`` picks the winner. Using one split for two of those jobs is how a model ends up
tuned on the data it is then judged by.

**One parameter set for every comparison.** The search runs once, here, on the 30 hot
features. Phase 4 trains the 36-feature model with the same parameters, so the graph
ablation measures the graph rather than a luckier hyperparameter draw.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

from fraud.config import load_yaml
from fraud.features.spec import CATEGORICAL_FEATURES, category_levels
from fraud.modeling.metrics import pr_auc
from fraud.modeling.splits import feature_matrix, target

logger = logging.getLogger(__name__)


def as_model_input(frame: pd.DataFrame, *, warm: bool = False) -> pd.DataFrame:
    """Features in spec order, with the category column typed from config.

    The levels come from ``categories.yaml``, not from the data. Inferring them would
    make the codes depend on which rows happened to appear in a split, so a model
    trained on one split would read a different meaning from the same integer (L7).
    """
    matrix = feature_matrix(frame, warm=warm).copy()
    levels = pd.CategoricalDtype(categories=list(category_levels()))
    for column in CATEGORICAL_FEATURES:
        if column in matrix.columns:
            matrix[column] = matrix[column].astype(levels)
    return matrix


@dataclass(frozen=True, slots=True)
class Trial:
    params: dict[str, Any]
    valid_pr_auc: float
    best_iteration: int
    seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "params": self.params,
            "valid_pr_auc": self.valid_pr_auc,
            "best_iteration": self.best_iteration,
            "seconds": round(self.seconds, 1),
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    best: Trial
    trials: list[Trial] = field(default_factory=list)
    total_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "best": self.best.to_dict(),
            "trials": [trial.to_dict() for trial in self.trials],
            "total_seconds": round(self.total_seconds, 1),
            "n_trials": len(self.trials),
        }


def sample_grid(n_trials: int, seed: int, grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Distinct parameter draws, seeded so the search can be repeated exactly."""
    rng = np.random.default_rng(seed)
    seen: set[tuple[Any, ...]] = set()
    sampled: list[dict[str, Any]] = []

    # The grid has 216 points, so 20 distinct draws are easy to find; the cap stops an
    # unlucky run from spinning if a future grid is ever smaller than the trial count.
    for _ in range(n_trials * 50):
        if len(sampled) >= n_trials:
            break
        params = {key: values[int(rng.integers(len(values)))] for key, values in grid.items()}
        key = tuple(params[name] for name in sorted(params))
        if key not in seen:
            seen.add(key)
            sampled.append(params)
    return sampled


def fit(
    train: pd.DataFrame,
    early_stop: pd.DataFrame,
    params: dict[str, Any],
    *,
    warm: bool = False,
    n_jobs: int = -1,
) -> XGBClassifier:
    """Fit on train, stopping when early_stop stops improving."""
    fixed = dict(load_yaml("model")["xgboost"]["fixed"])
    model = XGBClassifier(**fixed, **params, n_jobs=n_jobs)
    model.fit(
        as_model_input(train, warm=warm),
        target(train),
        eval_set=[(as_model_input(early_stop, warm=warm), target(early_stop))],
        verbose=False,
    )
    return model


def predict(model: XGBClassifier, frame: pd.DataFrame, *, warm: bool = False) -> np.ndarray:
    return model.predict_proba(as_model_input(frame, warm=warm))[:, 1]


def search(
    train: pd.DataFrame,
    early_stop: pd.DataFrame,
    valid: pd.DataFrame,
    *,
    warm: bool = False,
    n_jobs: int = -1,
) -> SearchResult:
    """Light random search, selected on valid PR-AUC (PLAN §7.4)."""
    config = load_yaml("model")["xgboost"]
    draws = sample_grid(int(config["search_trials"]), int(config["search_seed"]), config["grid"])

    started = time.perf_counter()
    trials: list[Trial] = []
    y_valid = target(valid).to_numpy()

    for index, params in enumerate(draws, start=1):
        trial_started = time.perf_counter()
        model = fit(train, early_stop, params, warm=warm, n_jobs=n_jobs)
        score = pr_auc(y_valid, predict(model, valid, warm=warm))
        trial = Trial(
            params=params,
            valid_pr_auc=score,
            best_iteration=int(getattr(model, "best_iteration", 0) or 0),
            seconds=time.perf_counter() - trial_started,
        )
        trials.append(trial)
        logger.info(
            "trial %2d/%d  PR-AUC %.4f  trees %4d  %5.1fs  %s",
            index,
            len(draws),
            score,
            trial.best_iteration,
            trial.seconds,
            params,
        )

    # Highest PR-AUC; ties broken towards fewer trees, which is the simpler model (§7.4).
    best = max(trials, key=lambda trial: (trial.valid_pr_auc, -trial.best_iteration))
    return SearchResult(best=best, trials=trials, total_seconds=time.perf_counter() - started)


def write_search(result: SearchResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("wrote %s", path)
