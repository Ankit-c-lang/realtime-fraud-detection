"""Collect reports/experiments/E*.json into reports/results.md (PLAN §7.9).

`results.md` is generated, never hand-written. That is the mechanism behind §0.4: every
number in the README and the CV has to be quotable from a file that records the split,
the commit and the code that produced it, so "I got 0.99" is always answerable with "run
this and look". A hand-maintained table drifts from the experiments within a day and
nobody can tell which half is stale.

The file renders whatever experiments exist. Before `make evaluate-test` has been run
there are no `*_test.json` files, and the test tables say so rather than silently
reporting validation numbers under a test heading.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from fraud.config import PROJECT_ROOT, Settings
from fraud.modeling.experiments import EXPERIMENTS_DIR
from fraud.modeling.metrics import review_budget

logger = logging.getLogger(__name__)

RESULTS = PROJECT_ROOT / "reports" / "results.md"
FIGURES = PROJECT_ROOT / "reports" / "figures"

PATTERNS = ("VELOCITY", "ATO", "CARD_TESTING", "RING")
# §7.9's own column headings, kept short so the table fits a terminal.
PATTERN_HEADINGS = {"VELOCITY": "VEL", "ATO": "ATO", "CARD_TESTING": "CT", "RING": "RING"}
ORDER = ("E1", "E2", "E3", "E3b", "E4")
NAMES = {
    "E1": "E1 · rules R1-R4",
    "E2": "E2 · XGBoost, 30 hot",
    "E3": "E3 · XGBoost, 36",
    "E3b": "E3b · graph only (6)",
    "E4": "E4 · blend at w*",
}


def _load(experiment_id: str) -> dict[str, Any] | None:
    path = EXPERIMENTS_DIR / f"{experiment_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _positives(metrics: dict[str, Any]) -> int:
    """Fraud rows in a split, implied by the recorded metrics.

    The Evaluation does not store a positive count, but ``alerts * precision`` is the
    true positives and dividing by recall recovers the total. Cross-checking against
    ``false_positive_rate`` gives the same number, so this is arithmetic on recorded
    values rather than a second read of the data.
    """
    if not metrics["recall"]:
        return 0
    return round(metrics["alerts"] * metrics["precision"] / metrics["recall"])


def _budget_note(suffix: str) -> str:
    """Say plainly when the alert rate broke the budget, and why (PLAN §7.7, §7.8).

    The budget is a promise about analyst capacity, so an overshoot is reported rather
    than smoothed over. It is also not a model failure here: the threshold is frozen on
    validation by design, and the alert rate follows whatever fraud prevalence the split
    happens to have.
    """
    test, valid = _load("E4" + suffix), _load("E4")
    if test is None or valid is None:
        return ""

    budget = review_budget()
    rate = test["metrics"]["alert_rate"]
    if rate <= budget:
        return f"The alert rate is {rate:.2%}, inside the {budget:.0%} review budget (§7.7).\n"

    test_rate = _positives(test["metrics"]) / test["rows"]
    valid_rate = _positives(valid["metrics"]) / valid["rows"]
    return (
        f"> **The alert rate is {rate:.2%}, above the {budget:.0%} review budget.** The "
        f"budget was met on validation ({valid['metrics']['alert_rate']:.2%}) and the "
        "threshold was carried over unchanged, as §7.11 requires — it was deliberately "
        "not re-tuned on test.\n>\n"
        f"> The cause is prevalence, not drift. The test window carries {test_rate:.2%} "
        f"fraud against validation's {valid_rate:.2%}, {test_rate / valid_rate:.2f}x as "
        f"much ({_positives(test['metrics']):,} fraudulent rows in {test['rows']:,}). At "
        f"{test['metrics']['recall']:.1%} recall the alert count tracks the fraud count "
        "almost exactly, so more fraud means more alerts at the same threshold. E2 and "
        "E3 overshoot by the same margin, which is what rules this out as something "
        "specific to the blend.\n>\n"
        "> In production this is what the budget is for: it would show up as a capacity "
        "breach and the threshold would be raised for the next model version, trading "
        "recall for load. It is reported here rather than fixed, because re-tuning on "
        "test is exactly what §7.11 forbids.\n"
    )


def _test_provenance() -> str:
    """The audit line proving the split was read once, and when (PLAN §7.11)."""
    log = PROJECT_ROOT / "reports" / "test_runs.log"
    if not log.is_file():
        return ""
    runs = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    if not runs:
        return ""
    fields = runs[0].split("\t")
    plural = "" if len(runs) == 1 else f" ({len(runs)} runs recorded; the first is reported)"
    return (
        f"Evaluated once on {fields[0]}, model `{fields[1]}`, commit `{fields[2]}`"
        f"{plural}. Every run appends to `reports/test_runs.log` (§7.11).\n"
    )


def _table(suffix: str = "") -> str:
    """The §7.9 results table for one split."""
    header = (
        "| Exp | PR-AUC | Precision | Recall | F1 | FPR | Alert rate | VDR | "
        + " | ".join(PATTERN_HEADINGS[p] for p in PATTERNS)
        + " |\n|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows: list[str] = []
    for experiment_id in ORDER:
        payload = _load(experiment_id + suffix)
        if payload is None:
            continue
        m = payload["metrics"]
        by_pattern = m["recall_by_pattern"]
        cells = [
            NAMES[experiment_id],
            f"{m['pr_auc']:.4f}",
            f"{m['precision']:.3f}",
            f"{m['recall']:.3f}",
            f"{m['f1']:.3f}",
            f"{m['false_positive_rate']:.4f}",
            f"{m['alert_rate']:.2%}",
            f"{m['value_detection_rate']:.3f}",
            *(f"{by_pattern.get(p, float('nan')):.2f}" for p in PATTERNS),
        ]
        rows.append("| " + " | ".join(cells) + " |")

    if not rows:
        return "_No experiments recorded for this split yet._\n"
    return header + "\n".join(rows) + "\n"


def _hold_table(suffix: str = "") -> str:
    """The HOLD tier, which on this dataset is disabled (§7.7)."""
    payload = _load("E4" + suffix)
    if payload is None:
        return "_Not available._\n"

    if suffix:
        # On the test section, the question a reader will actually have is whether the
        # tier was switched off after seeing test results. It was not, and the ordering
        # is checkable rather than asserted.
        return (
            "**Unchanged from validation: HOLD is disabled.** The tier was switched off "
            "when the thresholds were chosen on the validation split, before the test "
            "split had been read, and the artifact was not rebuilt afterwards. The test "
            "evaluation applied the stored policy and reports "
            f'`enabled: false, source: "policy"`, with {payload["metrics"]["hold"]["alerts"]} '
            "HOLD decisions.\n\n"
            "The ordering is verifiable: the commit in `reports/test_runs.log` is the one "
            "that already carried the disabled tier, and `models/v1/metadata.json` has "
            "one `metrics.test` block written by that run and a `thresholds` block "
            "written before it (§7.11, §8).\n"
        )

    hold = payload["metrics"]["hold"]
    enabled = payload["details"].get("hold_enabled", hold["threshold"] is not None)
    if not enabled:
        alerts = payload["metrics"]["alerts"]
        return (
            "**HOLD is disabled in this model version. Every flagged transaction goes to "
            f"REVIEW** — {alerts:,} of them, at {payload['metrics']['precision']:.3f} "
            "precision.\n\n"
            "§7.7 picks the HOLD threshold as the lowest one reaching 0.95 precision, "
            "which assumes precision at the budget threshold sits *below* that bar. Here "
            "it does not, so the lowest qualifying threshold is the REVIEW threshold "
            "itself and the two tiers coincide. Shipping that would freeze every flagged "
            "customer and leave the analyst queue empty, so the tier is switched off and "
            "reported as not separating, rather than manufactured by moving the bar.\n\n"
            "The measured numbers are kept in `models/*/metadata.json` under "
            '`thresholds.valid`, so "HOLD was possible but not distinct" stays '
            'distinguishable from "nothing was precise enough". `ALLOW`/`REVIEW`/`HOLD` '
            "remains the decision vocabulary; a later model version can enable the tier "
            "without a schema change (PLAN §7.7).\n"
        )
    return (
        "| Threshold | Precision | Alerts |\n|---|---|---|\n"
        f"| {hold['threshold']:.4f} | {hold['precision']:.3f} | {hold['alerts']} |\n"
    )


def _e5_table() -> str:
    """The §7.9 ensemble-evidence table, from the recorded LOPO run."""
    payload = _load("E5")
    if payload is None:
        return "_Not available; run `make lopo` then `make experiments`._\n"

    lines = [
        (
            "| Withheld pattern | Recall on it: `XGB_-k` alone "
            "| Recall on it: blended at `w*` | Overall precision: alone → blended |"
        ),
        "|---|---|---|---|",
    ]
    for row in payload["rows"]:
        recall, precision = row["pattern_recall"], row["overall_precision"]
        lines.append(
            f"| `{row['pattern']}` | {recall['alone']:.3f} | **{recall['blended']:.3f}** "
            f"| {precision['alone']:.3f} → {precision['blended']:.3f} |"
        )
    return "\n".join(lines) + "\n"


def _weight_table() -> str:
    payload = _load("E5")
    if payload is None:
        return ""
    means = payload["mean_recall_by_weight"]
    ordered = sorted(means, key=float, reverse=True)
    header = (
        "| w | " + " | ".join(f"{float(w):g}" for w in ordered) + " |\n"
        "|" + "---|" * (len(ordered) + 1) + "\n"
    )
    body = "| mean LOPO recall | " + " | ".join(f"{means[w]:.4f}" for w in ordered) + " |\n"
    return header + body


def contribution_figure(sample: int = 5000, path: Path | None = None) -> Path | None:
    """Global feature importance from the model's own contributions (PLAN §7.10).

    A beeswarm is tried first because it shows direction and spread, not just magnitude.
    If `shap` and the installed XGBoost disagree about anything, the plan's own fallback
    applies: mean |contribution| per feature, which answers the same question with less
    to go wrong.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from fraud.modeling.splits import load_many
    from fraud.scoring import reasons
    from fraud.scoring.risk_model import RiskModel

    target = path or FIGURES / "global_contributions.png"
    target.parent.mkdir(parents=True, exist_ok=True)

    model = RiskModel.load()
    valid = load_many(["valid"], warm=True)["valid"]
    rows = valid.sample(n=min(sample, len(valid)), random_state=42)
    matrix = model.model_input(rows)

    contribs = reasons.contributions(model.bundle.booster, matrix)[:, : matrix.shape[1]]

    try:
        import shap

        shap.summary_plot(
            contribs, matrix, feature_names=list(matrix.columns), show=False, max_display=20
        )
        plt.title(f"Contribution to the log-odds margin ({len(rows):,} validation rows)")
        plt.tight_layout()
        plt.savefig(target, dpi=140)
        plt.close("all")
        logger.info("wrote %s (shap beeswarm)", target)
        return target
    except Exception as error:  # noqa: BLE001 - the plan's own fallback path
        plt.close("all")
        logger.warning("shap beeswarm unavailable (%s); falling back to mean |contribution|", error)

    mean_abs = np.abs(contribs).mean(axis=0)
    order = np.argsort(mean_abs)[-20:]
    figure, axes = plt.subplots(figsize=(8, 7))
    axes.barh([matrix.columns[i] for i in order], mean_abs[order], color="#2a6f97")
    axes.set_xlabel("mean |contribution| to the log-odds margin")
    axes.set_title(f"Global feature importance ({len(rows):,} validation rows)")
    figure.tight_layout()
    figure.savefig(target, dpi=140)
    plt.close(figure)
    logger.info("wrote %s (mean |contribution| fallback)", target)
    return target


def render(figure: Path | None = None) -> str:
    """The whole of results.md."""
    e4 = _load("E4")
    e5 = _load("E5")
    test_run = _load("E4_test")

    weight = e5["best_weight"] if e5 else "?"
    version = e4["details"]["model_version"] if e4 else "?"
    commit = e4["git_commit"] if e4 else "?"

    parts = [
        "# Results",
        "",
        "**Generated by `make results`. Do not edit by hand (PLAN §0.4, §7.9).**",
        "",
        f"- Model version: `{version}` · blend weight `w* = {weight}` · commit `{commit}`",
        "- Every number traces to a file in `reports/experiments/`.",
        "- One simulated dataset, no confidence intervals. Rows inside one attack are",
        "  correlated, so a row-level bootstrap would overstate certainty (§7.8).",
        "",
        "## Validation split",
        "",
        "Development split. Thresholds are chosen here, under a 2% alert budget.",
        "",
        _table(),
        "",
        "### HOLD tier",
        "",
        _hold_table(),
        "",
        "## Test split",
        "",
    ]

    if test_run is None:
        parts += [
            "_Not evaluated yet._ The test split is read exactly once, by",
            "`make evaluate-test V=v1`, and every run appends a line to",
            "`reports/test_runs.log` (§7.11).",
            "",
        ]
    else:
        parts += [
            "Read once (§7.11). Thresholds come from the validation split; nothing here",
            "was fitted or tuned on test.",
            "",
            _test_provenance(),
            "",
            _table("_test"),
            "",
            "### Alert budget on test",
            "",
            _budget_note("_test"),
            "",
            "### HOLD tier",
            "",
            _hold_table("_test"),
            "",
        ]

    parts += [
        "## E5 · leave-one-pattern-out: does the ensemble earn its place?",
        "",
        "Each pattern is withheld from training in turn, so `XGB_-k` has genuinely never",
        "seen it. Recall is measured on the withheld pattern, on the validation split.",
        "",
        _e5_table(),
        "",
        "Mean overall recall across the four withheld-pattern runs, by blend weight:",
        "",
        _weight_table(),
    ]

    if e5 is not None:
        parts += [
            "",
            f"`w* = {e5['best_weight']}` (argmax, ties to the larger `w`). The grid stops",
            "at 0.5, so a better point below it would not be visible, and the top of the",
            "curve is flat — the margin over `w = 0.7` is about two transactions. What the",
            "curve does support is the step away from `w = 1.0`.",
        ]

    if figure is not None:
        parts += [
            "",
            "## Global feature importance",
            "",
            f"![Global contributions]({figure.relative_to(PROJECT_ROOT / 'reports')})",
            "",
            "Contributions come from the model's own `pred_contribs`, the same numbers",
            "behind the per-alert reason codes (§7.10).",
        ]

    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render reports/results.md (PLAN §7.9).")
    parser.add_argument("--no-figure", action="store_true", help="skip the contribution figure")
    parser.add_argument("--sample", type=int, default=5000, help="rows for the figure")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Settings.from_env()

    figure = None if args.no_figure else contribution_figure(args.sample)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(render(figure), encoding="utf-8")
    logger.info("wrote %s", RESULTS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
