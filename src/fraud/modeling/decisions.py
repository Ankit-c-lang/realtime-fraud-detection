"""REVIEW and HOLD thresholds and the decision policy (PLAN §7.7).

There is no BLOCK. The payment has already been authorised by the time this system sees
it, so the only honest verbs are: let it through, put it in front of an analyst, or
freeze the account for whatever comes next (§0.2).

The two thresholds answer two different questions and are chosen differently on purpose.

**REVIEW** is a capacity question. The best F1 on this data sits at an alert rate nobody
could staff, so the threshold is the best F1 *subject to* alerts staying inside
``review_budget``. Drop the budget and the number stops describing anything a team could
actually do.

**HOLD** is a harm question. It freezes a real customer's card, so it is the lowest
threshold whose precision clears 0.95 on at least 50 alerts. If nothing qualifies, HOLD
is disabled and the README says so — the bar does not move to make the tier exist.

**The tier can also collapse the other way, and it does here.** §7.7 quietly assumes
precision at the budget threshold sits *below* the HOLD bar, so that HOLD picks out a
stricter subset. On this data it does not: precision inside the 2% budget is already
above 0.95, so the lowest qualifying threshold is the REVIEW threshold itself and every
alert would become a HOLD, freezing every flagged customer and leaving no analyst queue.
That is not a two-tier policy, so it is treated as the same outcome as "nothing
qualifies": HOLD is disabled, the measured numbers are kept for the record, and the
README has to say that the tiers did not separate on this dataset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from fraud.config import load_yaml
from fraud.modeling.metrics import hold_threshold, operating_point, review_budget

logger = logging.getLogger(__name__)

ALLOW: Final[str] = "ALLOW"
REVIEW: Final[str] = "REVIEW"
HOLD: Final[str] = "HOLD"
DECISIONS: Final[tuple[str, ...]] = (ALLOW, REVIEW, HOLD)


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The operating point, with enough provenance to defend it later.

    ``hold`` is ``None`` when no threshold cleared the precision bar. That is a real
    outcome and is stored as one: a disabled tier is honest, a lowered bar is not.
    """

    review: float
    hold: float | None
    review_budget: float
    review_precision: float
    review_recall: float
    review_alerts: int
    review_alert_rate: float
    hold_precision: float | None = None
    hold_alerts: int = 0
    hold_degenerate: bool = False

    @property
    def hold_enabled(self) -> bool:
        return self.hold is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "review": self.review,
            "hold": self.hold,
            "hold_enabled": self.hold_enabled,
            "hold_degenerate": self.hold_degenerate,
            "review_budget": self.review_budget,
            "chosen_on": "valid",
            "valid": {
                "review_precision": self.review_precision,
                "review_recall": self.review_recall,
                "review_alerts": self.review_alerts,
                "review_alert_rate": self.review_alert_rate,
                "hold_precision": self.hold_precision,
                "hold_alerts": self.hold_alerts,
            },
        }


def choose(y_true: np.ndarray, risk: np.ndarray, *, budget: float | None = None) -> Thresholds:
    """Both thresholds, on the validation split (PLAN §7.7).

    Never call this with test data. The test split is read once, by ``evaluate_test.py``,
    and a threshold fitted there would make the final numbers self-fulfilling (L10).
    """
    y_true = np.asarray(y_true)
    risk = np.asarray(risk, dtype=float)
    limit = review_budget() if budget is None else budget

    point = operating_point(y_true, risk, limit)
    t_hold, hold_precision, hold_alerts = hold_threshold(y_true, risk)

    degenerate = False
    if t_hold is not None and t_hold <= point.threshold:
        # Every alert already clears the precision bar, so HOLD would swallow the whole
        # alert set and the REVIEW tier would be empty. Freezing every flagged card is a
        # worse answer than having no HOLD tier, so the tier is dropped and the fact is
        # recorded rather than the bar being moved to manufacture a separation.
        logger.warning(
            "HOLD is DISABLED: its threshold %.6f does not sit above REVIEW %.6f, so the "
            "two tiers do not separate. Precision inside the alert budget is %.3f, "
            "already above the %.2f HOLD bar. Reported, not tuned around (PLAN §7.7).",
            t_hold,
            point.threshold,
            point.precision,
            float(load_yaml("model")["hold"]["min_precision"]),
        )
        t_hold = None
        degenerate = True
    elif t_hold is None:
        logger.warning(
            "HOLD is DISABLED: no threshold reached the precision bar on at least the "
            "minimum alert count. The README must say so (PLAN §7.7)."
        )

    return Thresholds(
        review=point.threshold,
        hold=t_hold,
        review_budget=limit,
        review_precision=point.precision,
        review_recall=point.recall,
        review_alerts=point.alerts,
        review_alert_rate=point.alert_rate,
        # Kept even when the tier is disabled: "HOLD was possible but not distinct" and
        # "nothing was precise enough" are different findings and the metadata shows which.
        hold_precision=hold_precision,
        hold_alerts=hold_alerts,
        hold_degenerate=degenerate,
    )


def decide(risk: np.ndarray, thresholds: Thresholds) -> np.ndarray:
    """``HOLD`` if ``risk >= t_h``, else ``REVIEW`` if ``risk >= t_r``, else ``ALLOW``.

    Comparisons are ``>=`` on both tiers, matching how the thresholds were selected: a
    row exactly at the threshold was counted as an alert when the threshold was chosen,
    so it must be one here too.
    """
    risk = np.asarray(risk, dtype=float)
    decisions = np.full(len(risk), ALLOW, dtype=object)
    decisions[risk >= thresholds.review] = REVIEW
    if thresholds.hold is not None:
        decisions[risk >= thresholds.hold] = HOLD
    return decisions


def is_alert(decisions: np.ndarray) -> np.ndarray:
    """Alerts are REVIEW ∪ HOLD — what "at the operating point" means (§7.7)."""
    return np.asarray(decisions) != ALLOW
