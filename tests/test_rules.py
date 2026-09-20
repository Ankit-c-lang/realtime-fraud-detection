"""The R1-R4 rules baseline, on crafted rows (PLAN §7.3, §13).

Each rule gets a row that should fire it and a row just short of firing, so a threshold
that drifts by one is caught. The baseline has to be honest: if it were accidentally
weakened, every model after it would look better than it is.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fraud.features.spec import HOT_FEATURE_NAMES
from fraud.modeling.rules import RULE_NAMES, apply_rules, evaluate_rules, rule_thresholds


def row(**overrides: float) -> dict[str, float]:
    """A quiet transaction: nothing fires on it."""
    base = dict.fromkeys(HOT_FEATURE_NAMES, 0.0)
    base["merchant_category"] = "grocery"
    base["account_age_days"] = 500.0  # old account, so R4 cannot fire
    base["amount_to_mean"] = 1.0
    base.update(overrides)
    return base


def frame(*rows: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


@pytest.fixture(scope="module")
def thresholds() -> dict[str, dict[str, float]]:
    return rule_thresholds()


def test_a_quiet_transaction_fires_nothing() -> None:
    outcome = apply_rules(frame(row()))
    assert not outcome.flag[0]
    assert outcome.score[0] == 0.0


def test_every_rule_has_a_column() -> None:
    outcome = apply_rules(frame(row()))
    assert tuple(outcome.fired.columns) == RULE_NAMES


# --- R1 velocity ------------------------------------------------------------


def test_r1_fires_on_a_burst(thresholds: dict[str, dict[str, float]]) -> None:
    limit = int(thresholds["r1_velocity"]["acct_cnt_5m"])
    outcome = apply_rules(frame(row(acct_cnt_5m=limit), row(acct_cnt_5m=limit - 1)))

    assert outcome.fired["r1_velocity"].tolist() == [True, False]


# --- R2 ATO -----------------------------------------------------------------


def test_r2_needs_both_impossible_travel_and_a_new_device(
    thresholds: dict[str, dict[str, float]],
) -> None:
    """Either alone is ordinary: people travel, and people buy new phones."""
    speed = float(thresholds["r2_ato"]["geo_speed_kmh"])
    outcome = apply_rules(
        frame(
            row(geo_speed_kmh=speed, new_device=1),
            row(geo_speed_kmh=speed, new_device=0),
            row(geo_speed_kmh=speed - 1, new_device=1),
        )
    )
    assert outcome.fired["r2_ato"].tolist() == [True, False, False]


def test_r2_misses_the_rest_of_an_attack() -> None:
    """The documented weakness of the baseline, and why E2 has room to improve.

    Only the FIRST transaction of a takeover jumps location and uses an unseen device.
    The ones after it come from the same place on the same device, so R2 sees nothing,
    even though the amounts and the city are still wrong.
    """
    outcome = apply_rules(
        frame(
            row(geo_speed_kmh=5000.0, new_device=1),  # first event
            row(geo_speed_kmh=0.0, new_device=0, amount_to_mean=8.0, is_international=1),
            row(geo_speed_kmh=0.0, new_device=0, amount_to_mean=9.0, is_international=1),
        )
    )
    assert outcome.fired["r2_ato"].tolist() == [True, False, False]


# --- R3 card testing --------------------------------------------------------


def test_r3_fires_on_device_fan_out(thresholds: dict[str, dict[str, float]]) -> None:
    limit = int(thresholds["r3_card_testing"]["dev_accts_1h"])
    outcome = apply_rules(frame(row(dev_accts_1h=limit), row(dev_accts_1h=limit - 1)))

    assert outcome.fired["r3_card_testing"].tolist() == [True, False]


def test_r3_also_fires_on_small_declines(thresholds: dict[str, dict[str, float]]) -> None:
    """The other half of the rule: a run of tiny payments being refused."""
    spec = thresholds["r3_card_testing"]
    small = int(spec["acct_small_1h"])
    declines = int(spec["acct_declines_1h"])

    outcome = apply_rules(
        frame(
            row(acct_small_1h=small, acct_declines_1h=declines),
            row(acct_small_1h=small, acct_declines_1h=declines - 1),
            row(acct_small_1h=small - 1, acct_declines_1h=declines),
        )
    )
    assert outcome.fired["r3_card_testing"].tolist() == [True, False, False]


def test_r3_leaves_an_honest_micro_burst_alone(
    thresholds: dict[str, dict[str, float]],
) -> None:
    """The §4.4 hard negative: many small payments, but approved and on one device."""
    spec = thresholds["r3_card_testing"]
    outcome = apply_rules(
        frame(row(acct_small_1h=int(spec["acct_small_1h"]) + 5, acct_declines_1h=0, dev_accts_1h=1))
    )
    assert not outcome.fired["r3_card_testing"][0]


# --- R4 ring ----------------------------------------------------------------


def test_r4_needs_a_young_account_and_shared_devices(
    thresholds: dict[str, dict[str, float]],
) -> None:
    spec = thresholds["r4_ring"]
    devices = int(spec["dev_accts_30d"])
    age = float(spec["account_age_days"])

    outcome = apply_rules(
        frame(
            row(dev_accts_30d=devices, account_age_days=age - 1),
            row(dev_accts_30d=devices, account_age_days=age),  # old enough
            row(dev_accts_30d=devices - 1, account_age_days=age - 1),
        )
    )
    assert outcome.fired["r4_ring"].tolist() == [True, False, False]


def test_r4_leaves_an_established_shared_device_alone(
    thresholds: dict[str, dict[str, float]],
) -> None:
    """A family device on an old account is the §4.4 hard negative for rings."""
    spec = thresholds["r4_ring"]
    outcome = apply_rules(
        frame(row(dev_accts_30d=int(spec["dev_accts_30d"]) + 2, account_age_days=800.0))
    )
    assert not outcome.fired["r4_ring"][0]


# --- combination and scoring ------------------------------------------------


def test_any_rule_flags_the_transaction() -> None:
    outcome = apply_rules(frame(row(acct_cnt_5m=99), row()))
    assert outcome.flag.tolist() == [True, False]


def test_the_score_counts_how_many_rules_fired() -> None:
    """PR-AUC needs something to rank by; the count is the stand-in, not a probability."""
    outcome = apply_rules(frame(row(acct_cnt_5m=99, dev_accts_1h=99), row(acct_cnt_5m=99), row()))
    assert outcome.score.tolist() == [2.0, 1.0, 0.0]


def test_firing_rates_are_reported_per_rule() -> None:
    outcome = apply_rules(frame(row(acct_cnt_5m=99), row(), row(), row()))
    assert outcome.firing_rates()["r1_velocity"] == pytest.approx(0.25)


# --- evaluation -------------------------------------------------------------


def test_evaluate_rules_reports_at_where_the_rules_fire() -> None:
    """Not at a budget-constrained threshold: that would hide over-alerting."""
    rows = [row(acct_cnt_5m=99) for _ in range(5)] + [row() for _ in range(95)]
    data = frame(*rows)
    data["is_fraud"] = [1] * 5 + [0] * 95
    data["fraud_type"] = ["VELOCITY"] * 5 + ["NONE"] * 95
    data["amount"] = 100.0

    evaluation, outcome = evaluate_rules(data)

    assert evaluation.alert_rate == pytest.approx(0.05)
    assert evaluation.precision == pytest.approx(1.0)
    assert evaluation.recall == pytest.approx(1.0)
    assert evaluation.recall_by_pattern == {"VELOCITY": pytest.approx(1.0)}
    assert outcome.flag.sum() == 5


def test_rules_can_exceed_the_alert_budget() -> None:
    """Rules have no threshold to turn down, so over-alerting must be visible."""
    data = frame(*[row(acct_cnt_5m=99) for _ in range(100)])
    data["is_fraud"] = [1] + [0] * 99
    data["fraud_type"] = ["ATO"] + ["NONE"] * 99
    data["amount"] = 100.0

    evaluation, _ = evaluate_rules(data)
    assert evaluation.alert_rate == pytest.approx(1.0)
    assert evaluation.precision == pytest.approx(0.01)


def test_value_detection_rate_uses_the_amount_column() -> None:
    data = frame(row(acct_cnt_5m=99), row())
    data["is_fraud"] = [1, 1]
    data["fraud_type"] = ["ATO", "ATO"]
    data["amount"] = [9_000.0, 1_000.0]

    evaluation, _ = evaluate_rules(data)
    assert evaluation.value_detection_rate == pytest.approx(0.9)


def test_thresholds_come_from_config(thresholds: dict[str, dict[str, float]]) -> None:
    assert set(thresholds) == set(RULE_NAMES)
    custom = {
        "r1_velocity": {"acct_cnt_5m": 2},
        "r2_ato": {"geo_speed_kmh": 900.0, "new_device": 1},
        "r3_card_testing": {"dev_accts_1h": 5, "acct_small_1h": 3, "acct_declines_1h": 2},
        "r4_ring": {"dev_accts_30d": 3, "account_age_days": 30},
    }
    outcome = apply_rules(frame(row(acct_cnt_5m=2)), custom)
    assert outcome.fired["r1_velocity"][0]


def test_rules_never_look_at_a_label() -> None:
    """The baseline reads features only; a label reaching it would be meaningless."""
    data = frame(row(acct_cnt_5m=99))
    with_label = data.copy()
    with_label["is_fraud"] = 1

    assert np.array_equal(apply_rules(data).flag, apply_rules(with_label).flag)
