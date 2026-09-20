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
