"""The single guarded test-set evaluation (PLAN §7.11).

Everything else in this project reads `valid`. This module is the only thing allowed to
open `test`, it only runs when `ALLOW_TEST=1`, and it writes a line to
`reports/test_runs.log` every time it does — because the number that makes the test split
worth having is the *first* one. A test set you looked at three times while adjusting
something is a validation set with a misleading name, and the log is what makes that
claim checkable by someone who does not trust me.

**No threshold is chosen here.** The operating point comes from the artifact, where it
was selected on `valid`. Re-deriving it on `test` would tune the decision to the very data
being used to judge it, which is the most flattering and least honest thing this file
could do (L10).

**Nothing is refitted on test.** E2, E3 and the four leave-one-pattern-out models are
refitted from `train` with the recorded parameters — deterministic, seeded, identical to
the versions evaluated on valid — and then scored on test. E4 comes from the artifact.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fraud.config import PROJECT_ROOT, Settings
from fraud.modeling.experiments import (
    Experiment,
    git_commit,
    read,
    run_e4,
    summarise,
    write,
)
from fraud.modeling.metrics import Evaluation, evaluate_at, operating_point
from fraud.modeling.splits import TEST_SPLIT, allow_test_enabled, load_many, target

logger = logging.getLogger(__name__)

TEST_RUNS_LOG = PROJECT_ROOT / "reports" / "test_runs.log"


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    """One appended line of the audit trail (PLAN §7.11).

    Not named ``TestRun``: pytest collects any class starting with ``Test`` and warns,
    the same trap ``SplitLockedError`` was renamed to avoid.
    """

    timestamp: str
    model_version: str
    git_commit: str
    rows: int
    pr_auc: float

    def line(self) -> str:
        return (
            f"{self.timestamp}\t{self.model_version}\t{self.git_commit}\t"
            f"rows={self.rows}\tpr_auc={self.pr_auc:.6f}"
        )


def append_run(run: EvaluationRun, path: Path | None = None) -> Path:
    """Append, never rewrite. The history is the point (§7.11)."""
    target_path = path or TEST_RUNS_LOG
    target_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "" if target_path.exists() else "# timestamp\tmodel_version\tgit_commit\trows\tpr_auc\n"
    )
    with target_path.open("a", encoding="utf-8") as handle:
        handle.write(header + run.line() + "\n")
    logger.info("appended to %s", target_path)
    return target_path


def previous_runs(version: str, path: Path | None = None) -> int:
    """How many times this model version has already been evaluated on test."""
    target_path = path or TEST_RUNS_LOG
    if not target_path.is_file():
        return 0
    return sum(
        1
        for line in target_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and line.split("\t")[1:2] == [version]
    )


def _at_valid_threshold(
    y_test: np.ndarray,
    scores_valid: np.ndarray,
    y_valid: np.ndarray,
    scores_test: np.ndarray,
    test: pd.DataFrame,
) -> Evaluation:
    """Pick the operating point on valid, then report it on test.

    This is the whole discipline in three lines: the threshold is a decision made before
    seeing the test data, and test only ever answers "what does that decision buy?".
    """
    threshold = operating_point(y_valid, scores_valid).threshold
    return evaluate_at(
        y_test,
        scores_test,
        threshold,
        amounts=test["amount"].to_numpy() if "amount" in test.columns else None,
        fraud_type=test["fraud_type"].to_numpy(),
    )


def evaluate(version: str | None = None, *, n_jobs: int = -1) -> dict[str, Experiment]:
    """E1-E4 on the test split, in one read (PLAN §7.9, §7.11)."""
    from fraud.modeling import train_xgb
    from fraud.modeling.rules import evaluate_rules, rule_thresholds
    from fraud.scoring.risk_model import RiskModel

    if not allow_test_enabled():
        raise RuntimeError(
            "refusing to read the test split without ALLOW_TEST=1 (PLAN §7.11). "
            "`make evaluate-test V=v1` sets it; nothing else should."
        )

    risk_model = RiskModel.load(version)
    model_version = risk_model.model_version
    seen = previous_runs(model_version)
    if seen:
        logger.warning(
            "model %s has already been evaluated on test %d time(s). The README reports "
            "the FIRST run; if anything changed, bump the model version (PLAN §7.11).",
            model_version,
            seen,
        )

    frames = load_many(["train", "early_stop", "valid", TEST_SPLIT], warm=True)
    valid, test = frames["valid"], frames[TEST_SPLIT]
    y_valid, y_test = target(valid).to_numpy(), target(test).to_numpy()
    logger.info("test split: %s rows, %s fraud", f"{len(test):,}", f"{int(y_test.sum()):,}")

    results: dict[str, Experiment] = {}

    # E1: rules fire where they fire; there is no threshold to carry over.
    evaluation, outcome = evaluate_rules(test)
    results["E1"] = Experiment(
        experiment_id="E1_test",
        name="Rules baseline R1-R4",
        split=TEST_SPLIT,
        evaluation=evaluation,
        rows=len(test),
        details={"thresholds": rule_thresholds(), "firing_rates": outcome.firing_rates()},
    )

    params = read("E2")["details"]["params"]
    for experiment_id, name, warm in (
        ("E2", "XGBoost, 30 hot features", False),
        ("E3", "XGBoost, 36 features (hot + graph)", True),
    ):
        model = train_xgb.fit(
            frames["train"], frames["early_stop"], params, warm=warm, n_jobs=n_jobs
        )
        results[experiment_id] = Experiment(
            experiment_id=f"{experiment_id}_test",
            name=name,
            split=TEST_SPLIT,
            evaluation=_at_valid_threshold(
                y_test,
                train_xgb.predict(model, valid, warm=warm),
                y_valid,
                train_xgb.predict(model, test, warm=warm),
                test,
            ),
            rows=len(test),
            details={
                "params": params,
                "threshold_source": "chosen on valid, applied to test (PLAN §7.11)",
                "refit": "deterministic refit of the recorded configuration; nothing is "
                "fitted on test",
            },
        )

    results["E4"] = run_e4(test, model=risk_model, split=TEST_SPLIT)

    for experiment in results.values():
        write(experiment)
        logger.info("%-8s %s", experiment.experiment_id, summarise(experiment.evaluation))

    _record(risk_model, results["E4"].evaluation, len(test))
    return results


def _record(risk_model: Any, evaluation: Evaluation, rows: int) -> None:
    """Fill ``metrics.test`` in the artifact and append the audit line (§7.11, §8)."""
    run = EvaluationRun(
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        model_version=risk_model.model_version,
        git_commit=git_commit(),
        rows=rows,
        pr_auc=evaluation.pr_auc,
    )
    append_run(run)

    path = risk_model.bundle.directory / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if metadata["metrics"].get("test") is not None:
        # Keep the first run. Overwriting it would erase exactly the number §7.11 asks
        # the README to quote.
        logger.warning("metrics.test is already set in %s; keeping the first run", path)
        return
    metadata["metrics"]["test"] = evaluation.to_dict()
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("filled metrics.test in %s", path)


def main(argv: list[str] | None = None) -> int:
    """``make evaluate-test V=v1`` — run once, at the end (PLAN §7.11)."""
    import argparse

    parser = argparse.ArgumentParser(description="The one test-set evaluation (PLAN §7.11).")
    parser.add_argument("--version", default=None, help="model version; default CURRENT")
    parser.add_argument("--jobs", type=int, default=-1, help="XGBoost n_jobs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    evaluate(args.version, n_jobs=args.jobs)

    settings = Settings.from_env()
    logger.info(
        "done. Results: %s and reports/experiments/*_test.json. Render the tables with "
        "`make results`.",
        (settings.model_dir / (args.version or "CURRENT")),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
