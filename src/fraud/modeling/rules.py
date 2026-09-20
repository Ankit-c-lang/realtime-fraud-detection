"""The R1-R4 rules baseline, experiment E1 (PLAN §7.3).

This is the reference point every model is measured against. It exists because "XGBoost
beats nothing" is not a result: an analyst team without a model would be running rules
like these, so the honest question is how much the model adds on top of them.

Each rule targets one pattern, using features a rules engine could realistically compute:

* **R1** velocity — many transactions on one account within minutes
* **R2** ATO — impossible travel from a device the account has never used
* **R3** card testing — one device touching many accounts, or a run of small declines
* **R4** ring — a young account sharing devices with others

A transaction is flagged if ANY rule fires. Rules have no threshold to tune at serving
time, so E1 is reported where the rules actually fire rather than at a budget-constrained
operating point: hiding how much they over-alert would flatter the baseline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from fraud.config import load_yaml
from fraud.modeling.metrics import Evaluation, evaluate_at

logger = logging.getLogger(__name__)

RULE_NAMES = ("r1_velocity", "r2_ato", "r3_card_testing", "r4_ring")


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """Which rules fired, per transaction."""

    fired: pd.DataFrame  # one boolean column per rule
    flag: np.ndarray  # any rule fired
    score: np.ndarray  # how many fired, used only to rank for PR-AUC

    def firing_rates(self) -> dict[str, float]:
        return {name: float(self.fired[name].mean()) for name in self.fired.columns}


def rule_thresholds() -> dict[str, dict[str, Any]]:
    return load_yaml("model")["rules"]


def apply_rules(
    features: pd.DataFrame, thresholds: dict[str, dict[str, Any]] | None = None
) -> RuleOutcome:
    """Evaluate R1-R4 over a feature frame (PLAN §7.3)."""
    config = thresholds or rule_thresholds()

    r1 = config["r1_velocity"]
    r2 = config["r2_ato"]
    r3 = config["r3_card_testing"]
    r4 = config["r4_ring"]

    fired = pd.DataFrame(
        {
            # Velocity abuse: a card drained fast.
            "r1_velocity": features["acct_cnt_5m"] >= int(r1["acct_cnt_5m"]),
            # ATO: a jump no journey explains, from a device never seen before.
            "r2_ato": (features["geo_speed_kmh"] >= float(r2["geo_speed_kmh"]))
            & (features["new_device"] == int(r2["new_device"])),
            # Card testing: one device across many cards, or a run of small declines.
            "r3_card_testing": (features["dev_accts_1h"] >= int(r3["dev_accts_1h"]))
            | (
                (features["acct_small_1h"] >= int(r3["acct_small_1h"]))
                & (features["acct_declines_1h"] >= int(r3["acct_declines_1h"]))
            ),
            # Ring: a young account sharing devices with others.
            "r4_ring": (features["dev_accts_30d"] >= int(r4["dev_accts_30d"]))
            & (features["account_age_days"] < float(r4["account_age_days"])),
        },
        index=features.index,
    )

    return RuleOutcome(
        fired=fired,
        flag=fired.any(axis=1).to_numpy(),
        # Rules give a binary decision, so PR-AUC needs something to rank by. The count
        # of rules firing is the honest stand-in: it is not a probability and the report
        # says so.
        score=fired.sum(axis=1).to_numpy().astype(float),
    )


def evaluate_rules(frame: pd.DataFrame) -> tuple[Evaluation, RuleOutcome]:
    """Run E1 over a split, reporting where the rules actually fire."""
    outcome = apply_rules(frame)
    evaluation = evaluate_at(
        frame["is_fraud"].to_numpy(),
        outcome.score,
        threshold=1.0,  # at least one rule fired
        amounts=frame["amount"].to_numpy() if "amount" in frame.columns else None,
        fraud_type=frame["fraud_type"].to_numpy() if "fraud_type" in frame.columns else None,
    )
    return evaluation, outcome


def main(argv: list[str] | None = None) -> int:
    """Run E1 on a split and write reports/experiments/E1.json."""
    import argparse

    from fraud.modeling import experiments
    from fraud.modeling.splits import load

    parser = argparse.ArgumentParser(description="Rules baseline, experiment E1 (PLAN §7.3).")
    parser.add_argument("--split", default="valid", help="evaluate on this split")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    frame = load(args.split)
    evaluation, outcome = evaluate_rules(frame)

    for name, rate in outcome.firing_rates().items():
        logger.info("%-16s fires on %.3f%% of transactions", name, 100 * rate)
    logger.info("E1 on %s: %s", args.split, experiments.summarise(evaluation))

    experiments.write(
        experiments.Experiment(
            experiment_id="E1",
            name="Rules baseline R1-R4",
            split=args.split,
            evaluation=evaluation,
            rows=len(frame),
            details={
                "thresholds": rule_thresholds(),
                "firing_rates": outcome.firing_rates(),
                "note": (
                    "Rules give a binary decision. PR-AUC ranks by how many rules fired, "
                    "which is a stand-in, not a probability. Metrics are reported where "
                    "the rules fire, not at a budget-constrained threshold."
                ),
            },
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
