"""Test-set discipline and the generated results file (PLAN §7.9, §7.11).

Nothing in this file reads the test split. That is the point: the guard, the audit log
and the "keep the first run" rule are all testable without ever spending the one read
they exist to protect, and a test suite that quietly consumed it would defeat the whole
mechanism.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from fraud.modeling import evaluate_test
from fraud.modeling.evaluate_test import EvaluationRun, append_run, previous_runs
from fraud.modeling.metrics import Evaluation
from fraud.modeling.splits import SplitLockedError, allow_test_enabled
from fraud.modeling.splits import load as load_split


def _run(version: str = "v1", pr_auc: float = 0.99) -> EvaluationRun:
    return EvaluationRun(
        timestamp="2026-09-20T12:00:00+00:00",
        model_version=version,
        git_commit="abc1234",
        rows=1000,
        pr_auc=pr_auc,
    )


# --- the guard (PLAN §7.11, L10) ---------------------------------------------------


def test_evaluate_refuses_without_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_TEST", raising=False)
    with pytest.raises(RuntimeError, match="ALLOW_TEST=1"):
        evaluate_test.evaluate()


def test_split_loader_refuses_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: the guard lives in the loader as well as the entrypoint."""
    monkeypatch.delenv("ALLOW_TEST", raising=False)
    assert allow_test_enabled() is False
    with pytest.raises(SplitLockedError, match="evaluate_test"):
        load_split("test")


def test_the_guard_names_the_only_sanctioned_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_TEST", raising=False)
    with pytest.raises(RuntimeError, match="make evaluate-test"):
        evaluate_test.evaluate()


# --- the audit log (PLAN §7.11) ----------------------------------------------------


def test_log_gets_a_header_once(tmp_path: Path) -> None:
    path = tmp_path / "test_runs.log"
    append_run(_run(), path)
    append_run(_run(pr_auc=0.98), path)

    lines = path.read_text().splitlines()
    assert lines[0].startswith("# timestamp")
    assert len([line for line in lines if line.startswith("#")]) == 1
    assert len(lines) == 3


def test_log_is_append_only(tmp_path: Path) -> None:
    """A rewritten log could hide a second look at the test set."""
    path = tmp_path / "test_runs.log"
    append_run(_run(pr_auc=0.90), path)
    first = path.read_text()
    append_run(_run(pr_auc=0.99), path)
    assert path.read_text().startswith(first)


def test_previous_runs_counts_per_version(tmp_path: Path) -> None:
    path = tmp_path / "test_runs.log"
    assert previous_runs("v1", path) == 0

    append_run(_run("v1"), path)
    append_run(_run("v1"), path)
    append_run(_run("v2"), path)
    assert previous_runs("v1", path) == 2
    assert previous_runs("v2", path) == 1
    assert previous_runs("v3", path) == 0


def test_log_line_carries_what_section_711_asks_for() -> None:
    line = _run().line()
    for field in ("2026-09-20T12:00:00+00:00", "v1", "abc1234", "rows=1000", "pr_auc=0.990000"):
        assert field in line


# --- metrics.test is written once (PLAN §7.11, §8) ---------------------------------


@dataclass
class _FakeBundle:
    directory: Path


@dataclass
class _FakeModel:
    model_version: str
    bundle: _FakeBundle


def _evaluation(pr_auc: float) -> Evaluation:
    return Evaluation(
        pr_auc=pr_auc,
        roc_auc=0.99,
        threshold=0.5,
        precision=0.9,
        recall=0.9,
        f1=0.9,
        false_positive_rate=0.001,
        alert_rate=0.01,
        alerts=100,
        value_detection_rate=0.9,
    )


@pytest.fixture
def artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeModel:
    (tmp_path / "metadata.json").write_text(
        json.dumps({"model_version": "v1", "metrics": {"valid": {"pr_auc": 1.0}, "test": None}})
    )
    monkeypatch.setattr(evaluate_test, "TEST_RUNS_LOG", tmp_path / "test_runs.log")
    return _FakeModel("v1", _FakeBundle(tmp_path))


def test_first_run_fills_metrics_test(artifact: _FakeModel) -> None:
    evaluate_test._record(artifact, _evaluation(0.95), rows=1000)
    metadata = json.loads((artifact.bundle.directory / "metadata.json").read_text())
    assert metadata["metrics"]["test"]["pr_auc"] == 0.95
    assert metadata["metrics"]["valid"] is not None


def test_a_second_run_does_not_overwrite_the_first(artifact: _FakeModel) -> None:
    """§7.11: the README quotes the FIRST test run of a model version."""
    evaluate_test._record(artifact, _evaluation(0.95), rows=1000)
    evaluate_test._record(artifact, _evaluation(0.42), rows=1000)

    metadata = json.loads((artifact.bundle.directory / "metadata.json").read_text())
    assert metadata["metrics"]["test"]["pr_auc"] == 0.95
    # Both attempts are still logged, so a second look cannot happen unrecorded.
    assert previous_runs("v1", artifact.bundle.directory / "test_runs.log") == 2


# --- the threshold comes from valid ------------------------------------------------


def test_threshold_is_chosen_on_valid_not_on_test() -> None:
    """The single most important line in this module (§7.11, L10).

    Test scores here are deliberately shifted, so a threshold re-derived on test would
    land somewhere else and report a flattering precision. It must not.
    """
    rng = np.random.default_rng(0)
    y_valid = (rng.random(4000) < 0.2).astype(int)
    scores_valid = np.clip(y_valid * 0.5 + rng.random(4000) * 0.5, 0, 1)

    y_test = (rng.random(4000) < 0.2).astype(int)
    scores_test = np.clip(y_test * 0.5 + rng.random(4000) * 0.5, 0, 1) * 0.5

    import pandas as pd

    frame = pd.DataFrame({"fraud_type": np.where(y_test == 1, "VELOCITY", "NONE")})
    evaluation = evaluate_test._at_valid_threshold(
        y_test, scores_valid, y_valid, scores_test, frame
    )

    from fraud.modeling.metrics import operating_point

    assert evaluation.threshold == operating_point(y_valid, scores_valid).threshold
    # Scores on test were halved, so at the valid threshold almost nothing fires. A
    # threshold fitted on test would have found a comfortable operating point instead.
    assert evaluation.alert_rate < 0.001


# --- the generated results file (PLAN §7.9, §0.4) ----------------------------------


def _export_results() -> Any:
    """Load scripts/export_results.py by path: scripts/ is not an installed package."""
    import importlib.util
    import sys

    from fraud.config import PROJECT_ROOT

    spec = importlib.util.spec_from_file_location(
        "export_results", PROJECT_ROOT / "scripts" / "export_results.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: @dataclass resolves its own module through sys.modules,
    # and a module that is not there fails with an opaque AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _experiment(experiment_id: str, *, rows: int = 66349, **metrics: Any) -> dict[str, Any]:
    base = {
        "pr_auc": 0.99,
        "precision": 0.9,
        "recall": 0.9,
        "f1": 0.9,
        "false_positive_rate": 0.001,
        "alert_rate": 0.013,
        "alerts": 909,
        "value_detection_rate": 0.9,
        "recall_by_pattern": {"VELOCITY": 0.9, "ATO": 1.0, "CARD_TESTING": 1.0, "RING": 1.0},
        "hold": {
            "enabled": False,
            "threshold": None,
            "precision": None,
            "alerts": 0,
            "source": "policy",
        },
    }
    base.update(metrics)
    return {
        "experiment_id": experiment_id,
        "git_commit": "abc1234",
        "rows": rows,
        "metrics": base,
        "details": {"model_version": "v1", "hold_enabled": False},
    }


@pytest.fixture
def rendered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    export = _export_results()

    monkeypatch.setattr(export, "EXPERIMENTS_DIR", tmp_path)
    for experiment_id in ("E1", "E2", "E3", "E3b", "E4"):
        (tmp_path / f"{experiment_id}.json").write_text(json.dumps(_experiment(experiment_id)))
    (tmp_path / "E5.json").write_text(
        json.dumps(
            {
                "best_weight": 0.5,
                "mean_recall_by_weight": {"1": 0.73, "0.5": 0.85},
                "rows": [
                    {
                        "pattern": "RING",
                        "pattern_recall": {"alone": 0.0, "blended": 0.0},
                        "overall_precision": {"alone": 0.93, "blended": 0.93},
                    }
                ],
            }
        )
    )
    return export.render(None)


def test_results_uses_the_planned_columns(rendered: str) -> None:
    assert "| Exp | PR-AUC | Precision | Recall | F1 | FPR | Alert rate | VDR |" in rendered
    assert "| VEL | ATO | CT | RING |" in rendered


def test_results_lists_every_experiment(rendered: str) -> None:
    for name in ("E1 · rules", "E2 · XGBoost, 30 hot", "E3 · XGBoost, 36", "E3b", "E4 · blend"):
        assert name in rendered


def test_results_says_the_test_split_is_unread(rendered: str) -> None:
    """The failure to avoid: validation numbers printed under a test heading."""
    assert "_Not evaluated yet._" in rendered
    assert "make evaluate-test V=v1" in rendered


def test_results_explains_the_disabled_hold_tier(rendered: str) -> None:
    assert "HOLD is disabled in this model version" in rendered
    assert "Every flagged transaction goes to REVIEW" in rendered


def test_results_reports_the_flat_weight_curve(rendered: str) -> None:
    """The caveat has to survive into the generated file, not just the commit message."""
    assert "about two transactions" in rendered
    assert "w* = 0.5" in rendered


def test_results_carries_the_synthetic_data_caveat(rendered: str) -> None:
    assert "no confidence intervals" in rendered


def test_results_says_it_is_generated(rendered: str) -> None:
    assert "Do not edit by hand" in rendered


# --- the test section, once the split has been read --------------------------------


@pytest.fixture
def rendered_with_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A render where the test split has been evaluated and broke the alert budget."""
    export = _export_results()
    monkeypatch.setattr(export, "EXPERIMENTS_DIR", tmp_path)
    monkeypatch.setattr(export, "PROJECT_ROOT", tmp_path)

    # Validation: inside the budget. Test: over it, purely because prevalence doubled.
    (tmp_path / "E4.json").write_text(
        json.dumps(_experiment("E4", alert_rate=0.0137, alerts=909, precision=0.96, recall=0.985))
    )
    (tmp_path / "E4_test.json").write_text(
        json.dumps(
            _experiment(
                "E4_test",
                rows=102987,
                alert_rate=0.0209,
                alerts=2151,
                precision=0.958,
                recall=0.993,
            )
        )
    )
    log = tmp_path / "reports" / "test_runs.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "# timestamp\tmodel_version\tgit_commit\trows\tpr_auc\n"
        "2026-09-21T01:59:13+00:00\tv1\t0d30e50\trows=102987\tpr_auc=0.993788\n"
    )
    return export.render(None)


def test_budget_overshoot_is_stated_not_buried(rendered_with_test: str) -> None:
    """A capacity promise that was broken has to be said out loud (§7.7, §7.8)."""
    assert "2.09%, above the 2% review budget" in rendered_with_test
    assert "deliberately not re-tuned on test" in rendered_with_test


def test_budget_overshoot_names_prevalence_as_the_cause(rendered_with_test: str) -> None:
    """The overshoot is explained, so nobody reads it as the model degrading."""
    assert "The cause is prevalence, not drift" in rendered_with_test
    assert "2.01% fraud against validation's" in rendered_with_test


def test_budget_note_is_silent_when_inside_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = _export_results()
    monkeypatch.setattr(export, "EXPERIMENTS_DIR", tmp_path)
    (tmp_path / "E4.json").write_text(json.dumps(_experiment("E4", alert_rate=0.0137)))
    (tmp_path / "E4_test.json").write_text(
        json.dumps(_experiment("E4_test", rows=102987, alert_rate=0.018))
    )

    note = export._budget_note("_test")
    assert "above the" not in note
    assert "inside the 2% review budget" in note


def test_test_section_says_hold_was_unchanged(rendered_with_test: str) -> None:
    """The question a reader will have: was the tier switched off after seeing test?"""
    assert "Unchanged from validation: HOLD is disabled" in rendered_with_test
    assert "before the test split had been read" in rendered_with_test
    assert "not rebuilt afterwards" in rendered_with_test


def test_test_section_carries_the_audit_line(rendered_with_test: str) -> None:
    assert "Evaluated once on 2026-09-21T01:59:13+00:00" in rendered_with_test
    assert "commit `0d30e50`" in rendered_with_test
    assert "_Not evaluated yet._" not in rendered_with_test


def test_repeated_runs_would_be_flagged_in_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second look at test must be visible in the report, not only in the log."""
    export = _export_results()
    monkeypatch.setattr(export, "PROJECT_ROOT", tmp_path)
    log = tmp_path / "reports" / "test_runs.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "# h\n"
        "2026-09-21T01:59:13+00:00\tv1\tabc\trows=1\tpr_auc=0.9\n"
        "2026-09-21T02:59:13+00:00\tv1\tdef\trows=1\tpr_auc=0.8\n"
    )
    note = export._test_provenance()
    assert "2 runs recorded; the first is reported" in note


def test_implied_positive_count_matches_both_derivations() -> None:
    """alerts*precision/recall and the FPR route must agree, or the note is wrong."""
    export = _export_results()
    metrics = {
        "alerts": 2151,
        "precision": 0.9576940957694096,
        "recall": 0.9927710843373494,
        "false_positive_rate": 0.0009017758046614872,
    }
    rows = 102987
    via_recall = export._positives(metrics)

    false_alerts = metrics["alerts"] * (1 - metrics["precision"])
    negatives = false_alerts / metrics["false_positive_rate"]
    via_fpr = round(rows - negatives)

    assert via_recall == 2075
    assert abs(via_recall - via_fpr) <= 1
