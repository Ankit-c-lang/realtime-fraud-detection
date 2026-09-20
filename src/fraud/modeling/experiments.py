"""Experiment runner: writes reports/experiments/E*.json (PLAN §7.9).

Every published number traces back to one of these files, which is what makes the
honesty rule in §0.4 enforceable: the README quotes a file, and the file records the
command, the split, the data and the code that produced it.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from fraud.config import PROJECT_ROOT
from fraud.features.spec import FEATURE_SPEC_VERSION
from fraud.modeling.metrics import Evaluation

logger = logging.getLogger(__name__)

EXPERIMENTS_DIR = PROJECT_ROOT / "reports" / "experiments"


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=PROJECT_ROOT,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        return "unknown"


@dataclass(frozen=True, slots=True)
class Experiment:
    """One result, pinned to what produced it."""

    experiment_id: str
    name: str
    split: str
    evaluation: Evaluation
    rows: int
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "split": self.split,
            "rows": self.rows,
            "feature_spec_version": FEATURE_SPEC_VERSION,
            "git_commit": git_commit(),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "metrics": self.evaluation.to_dict(),
            "details": self.details,
        }


def write(experiment: Experiment, directory: Path | None = None) -> Path:
    target = directory or EXPERIMENTS_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{experiment.experiment_id}.json"
    path.write_text(
        json.dumps(experiment.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    logger.info("wrote %s", path)
    return path


def read(experiment_id: str, directory: Path | None = None) -> dict[str, Any]:
    path = (directory or EXPERIMENTS_DIR) / f"{experiment_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def summarise(evaluation: Evaluation) -> str:
    """One line for a log, in the order §7.9's results table uses."""
    patterns = " ".join(
        f"{name[:4]}={value:.2f}" for name, value in sorted(evaluation.recall_by_pattern.items())
    )
    return (
        f"PR-AUC {evaluation.pr_auc:.4f}  P {evaluation.precision:.3f}  "
        f"R {evaluation.recall:.3f}  F1 {evaluation.f1:.3f}  "
        f"FPR {evaluation.false_positive_rate:.4f}  alerts {evaluation.alert_rate:.2%}  "
        f"VDR {evaluation.value_detection_rate:.3f}  {patterns}"
    )


def run_e1(frame: pd.DataFrame) -> Experiment:
    """E1: the rules baseline (PLAN §7.3)."""
    from fraud.modeling.rules import evaluate_rules, rule_thresholds

    evaluation, outcome = evaluate_rules(frame)
    return Experiment(
        experiment_id="E1",
        name="Rules baseline R1-R4",
        split="valid",
        evaluation=evaluation,
        rows=len(frame),
        details={
            "thresholds": rule_thresholds(),
            "firing_rates": outcome.firing_rates(),
            "note": (
                "Rules give a binary decision. PR-AUC ranks by how many rules fired, "
                "which is a stand-in, not a probability. Metrics are reported where the "
                "rules fire, not at a budget-constrained threshold."
            ),
        },
    )


def run_e2(
    train: pd.DataFrame,
    early_stop: pd.DataFrame,
    valid: pd.DataFrame,
    *,
    n_jobs: int = -1,
) -> tuple[Experiment, dict[str, Any]]:
    """E2: XGBoost on the 30 hot features (PLAN §7.4)."""
    from fraud.modeling import train_xgb
    from fraud.modeling.metrics import evaluate
    from fraud.modeling.splits import target

    result = train_xgb.search(train, early_stop, valid, n_jobs=n_jobs)
    train_xgb.write_search(result, EXPERIMENTS_DIR / "xgb_search.json")

    model = train_xgb.fit(train, early_stop, result.best.params, n_jobs=n_jobs)
    scores = train_xgb.predict(model, valid)
    evaluation = evaluate(
        target(valid).to_numpy(),
        scores,
        amounts=valid["amount"].to_numpy() if "amount" in valid.columns else None,
        fraud_type=valid["fraud_type"].to_numpy(),
    )

    experiment = Experiment(
        experiment_id="E2",
        name="XGBoost, 30 hot features",
        split="valid",
        evaluation=evaluation,
        rows=len(valid),
        details={
            "params": result.best.params,
            "best_iteration": result.best.best_iteration,
            "n_trials": len(result.trials),
            "search_seconds": round(result.total_seconds, 1),
            "train_rows": len(train),
            "early_stop_rows": len(early_stop),
            "scale_pos_weight": 1,
            "note": (
                "Fitted on train, stopped on early_stop, selected on valid. No "
                "reweighting: the probability is blended with an anomaly percentile "
                "later (PLAN §7.2)."
            ),
        },
    )
    return experiment, result.best.params


def main(argv: list[str] | None = None) -> int:
    """Run the development experiments on valid (PLAN §7.9)."""
    import argparse

    from fraud.modeling.splits import load_many

    parser = argparse.ArgumentParser(description="Run experiments E1-E2 (PLAN §7.9).")
    parser.add_argument("--only", nargs="*", default=None, help="subset, e.g. --only E1")
    parser.add_argument("--jobs", type=int, default=-1, help="XGBoost n_jobs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    wanted = set(args.only) if args.only else {"E1", "E2"}

    frames = load_many(["train", "early_stop", "valid"])
    logger.info(
        "train %s / early_stop %s / valid %s rows",
        f"{len(frames['train']):,}",
        f"{len(frames['early_stop']):,}",
        f"{len(frames['valid']):,}",
    )

    results: dict[str, Experiment] = {}
    if "E1" in wanted:
        results["E1"] = run_e1(frames["valid"])
    if "E2" in wanted:
        results["E2"], _ = run_e2(
            frames["train"], frames["early_stop"], frames["valid"], n_jobs=args.jobs
        )

    for experiment in results.values():
        write(experiment)
        logger.info("%s %s", experiment.experiment_id, summarise(experiment.evaluation))

    if {"E1", "E2"} <= results.keys():
        _compare(results["E1"], results["E2"])
    return 0


def _compare(baseline: Experiment, model: Experiment) -> None:
    """PLAN §17: E2 must beat E1, and a suspiciously perfect score is a warning."""
    gain = model.evaluation.pr_auc - baseline.evaluation.pr_auc
    logger.info(
        "E2 - E1: PR-AUC %+.4f, recall %+.4f, alert rate %+.4f",
        gain,
        model.evaluation.recall - baseline.evaluation.recall,
        model.evaluation.alert_rate - baseline.evaluation.alert_rate,
    )
    if gain <= 0:
        logger.warning("E2 does NOT beat E1 on PR-AUC. Investigate before continuing (§17).")
    if model.evaluation.pr_auc > 0.995:
        logger.warning(
            "valid PR-AUC %.4f exceeds 0.995. PLAN §4.8 allows ONE simulator revision "
            "(sim-v2) in this case. Report it; do not act unilaterally.",
            model.evaluation.pr_auc,
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
