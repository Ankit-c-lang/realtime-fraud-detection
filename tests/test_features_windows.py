"""Window, deviation and novelty features, from hand-computed examples (PLAN §5.1-§5.2).

WRITTEN BEFORE THE IMPLEMENTATION (prompt P2.1). Every number here was worked out by
hand. The implementation must match them; they are not adjusted to match it.

The two rules almost everything below depends on (PLAN §5.1):

* windows are half-open ``[t - w, t)``, so an event never counts itself;
* features are computed from the state BEFORE the event, then the state is updated.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from feature_helpers import (
    CREATED_AT,
    DELHI,
    MUMBAI,
    at,
    directory,
    feature_rows,
    last_row,
    make_event,
)

THIRTY_DAYS_SECONDS = 30 * 86400


# --- half-open windows (PLAN §5.1 rule 2) -----------------------------------


def test_the_plan_worked_example_for_a_five_minute_window() -> None:
    """PLAN P2.1: events at 10:00:00, 10:04:59 and 10:05:00 give acct_cnt_5m = 2."""
    rows = feature_rows(
        [
            make_event("T0000000", at(10, 0, 0)),
            make_event("T0000001", at(10, 4, 59)),
            make_event("T0000002", at(10, 5, 0)),
        ]
    )
    assert [row["acct_cnt_5m"] for row in rows] == [0, 1, 2]


def test_an_event_never_counts_itself() -> None:
    """The first event of an account sees an empty window, not a window of one."""
    row = last_row([make_event("T0000000", at(10))])
    assert row["acct_cnt_5m"] == 0
    assert row["acct_cnt_1h"] == 0
    assert row["acct_cnt_24h"] == 0
    assert row["acct_history_cnt"] == 0


def test_the_lower_bound_is_inclusive_and_the_upper_exclusive() -> None:
    """[t-5m, t). An event exactly 5 minutes old counts; one a second older does not."""
    rows = feature_rows(
        [
            make_event("T0000000", at(9, 59, 59)),
            make_event("T0000001", at(10, 0, 0)),
            make_event("T0000002", at(10, 4, 59)),
            make_event("T0000003", at(10, 5, 0)),
        ]
    )
    # At 10:05:00 the window is [10:00:00, 10:05:00): 09:59:59 falls out, the other two in.
    assert rows[-1]["acct_cnt_5m"] == 2
    assert rows[-1]["acct_cnt_1h"] == 3


def test_longer_windows_accumulate_independently() -> None:
    events = [
        make_event("T0000000", at(10) - timedelta(hours=30)),  # outside 24 h
        make_event("T0000001", at(10) - timedelta(hours=5)),  # inside 24 h only
        make_event("T0000002", at(10) - timedelta(minutes=30)),  # inside 1 h
        make_event("T0000003", at(10) - timedelta(minutes=2)),  # inside 5 m
        make_event("T0000004", at(10)),
    ]
    row = feature_rows(events)[-1]
    assert row["acct_cnt_5m"] == 1
    assert row["acct_cnt_1h"] == 2
    assert row["acct_cnt_24h"] == 3
    assert row["acct_history_cnt"] == 4


def test_amount_sum_over_twenty_four_hours() -> None:
    events = [
        make_event("T0000000", at(10) - timedelta(hours=30), amount=1000.0),  # outside
        make_event("T0000001", at(10) - timedelta(hours=3), amount=250.0),
        make_event("T0000002", at(10) - timedelta(hours=1), amount=125.5),
        make_event("T0000003", at(10), amount=999.0),
    ]
    assert feature_rows(events)[-1]["acct_amt_24h"] == pytest.approx(375.5)


def test_distinct_merchants_in_the_last_hour() -> None:
    events = [
        make_event("T0000000", at(9, 10), merchant_id="M000001"),
        make_event("T0000001", at(9, 20), merchant_id="M000002"),
        make_event("T0000002", at(9, 30), merchant_id="M000001"),  # repeat
        make_event("T0000003", at(10, 0), merchant_id="M000003"),
    ]
    # Window [09:00, 10:00) holds M1, M2, M1 -> two distinct merchants.
    assert feature_rows(events)[-1]["acct_merchants_1h"] == 2


def test_declines_and_small_amounts_in_the_last_hour() -> None:
    """The card-testing signature: many tiny payments, many of them refused."""
    events = [
        make_event("T0000000", at(9, 10), amount=20.0, status="DECLINED"),
        make_event("T0000001", at(9, 20), amount=35.0, status="DECLINED"),
        make_event("T0000002", at(9, 30), amount=99.99, status="APPROVED"),
        make_event("T0000003", at(9, 40), amount=100.0, status="APPROVED"),  # not "small"
        make_event("T0000004", at(10, 0), amount=5000.0),
    ]
    row = feature_rows(events)[-1]
    assert row["acct_declines_1h"] == 2
    assert row["acct_small_1h"] == 3


# --- cold-start defaults (PLAN §5.1 rule 4) ---------------------------------


def test_cold_start_defaults_on_the_very_first_event() -> None:
    row = last_row([make_event("T0000000", at(10), amount=500.0)])

    assert row["amount_zscore"] == 0.0
    assert row["amount_to_mean"] == 1.0
    assert row["secs_since_last"] == THIRTY_DAYS_SECONDS
    assert row["geo_speed_kmh"] == 0.0
    assert row["acct_history_cnt"] == 0


def test_secs_since_last_is_capped_at_thirty_days() -> None:
    events = [
        make_event("T0000000", at(10) - timedelta(days=90)),
        make_event("T0000001", at(10)),
    ]
    assert feature_rows(events)[-1]["secs_since_last"] == THIRTY_DAYS_SECONDS


def test_secs_since_last_measures_the_real_gap() -> None:
    events = [make_event("T0000000", at(9, 30)), make_event("T0000001", at(10, 0))]
    assert feature_rows(events)[-1]["secs_since_last"] == 1800


# --- deviation features (PLAN §5.2 #15-#17) ---------------------------------


def test_amount_zscore_needs_three_prior_approved_transactions() -> None:
    """Fewer than three and the standard deviation means nothing, so the default is 0."""
    events = [
        make_event("T0000000", at(9, 0), amount=100.0),
        make_event("T0000001", at(9, 10), amount=200.0),
        make_event("T0000002", at(9, 20), amount=1_000_000.0),
    ]
    assert feature_rows(events)[-1]["amount_zscore"] == 0.0


def test_amount_zscore_worked_example() -> None:
    """Prior approved 100, 200, 300 -> mean 200, sample std 100.

    denominator = max(std=100, 0.25*mean=50, 50) = 100, so 500 scores (500-200)/100 = 3.
    """
    events = [
        make_event("T0000000", at(9, 0), amount=100.0),
        make_event("T0000001", at(9, 10), amount=200.0),
        make_event("T0000002", at(9, 20), amount=300.0),
        make_event("T0000003", at(9, 30), amount=500.0),
    ]
    row = feature_rows(events)[-1]
    assert row["amount_zscore"] == pytest.approx(3.0)
    assert row["amount_to_mean"] == pytest.approx(2.5)


def test_amount_zscore_uses_the_floor_when_spending_is_identical() -> None:
    """Four identical ₹100 payments give std 0. Without a floor the z-score explodes.

    denominator = max(0, 0.25*100=25, 50) = 50, so ₹150 scores (150-100)/50 = 1.
    """
    events = [make_event(f"T000000{i}", at(9, 10 * i), amount=100.0) for i in range(4)]
    events.append(make_event("T0000004", at(9, 50), amount=150.0))
    assert feature_rows(events)[-1]["amount_zscore"] == pytest.approx(1.0)


def test_amount_zscore_is_clipped() -> None:
    """Clipped to [-10, 50] (§5.2 #15), so one freak payment cannot dominate a split."""
    events = [make_event(f"T000000{i}", at(9, 10 * i), amount=100.0) for i in range(4)]
    events.append(make_event("T0000004", at(9, 50), amount=10_000_000.0))
    assert feature_rows(events)[-1]["amount_zscore"] == 50.0


def test_amount_to_mean_is_clipped_to_one_hundred() -> None:
    events = [make_event(f"T000000{i}", at(9, 10 * i), amount=100.0) for i in range(3)]
    events.append(make_event("T0000003", at(9, 40), amount=1_000_000.0))
    assert feature_rows(events)[-1]["amount_to_mean"] == 100.0


def test_declined_transactions_do_not_move_the_spending_statistics() -> None:
    """PLAN §5.1 rule 5: mean and std use prior APPROVED transactions only."""
    events = [
        make_event("T0000000", at(9, 0), amount=100.0),
        make_event("T0000001", at(9, 10), amount=200.0),
        make_event("T0000002", at(9, 20), amount=300.0),
        make_event("T0000003", at(9, 25), amount=999_999.0, status="DECLINED"),
        make_event("T0000004", at(9, 30), amount=500.0),
    ]
    row = feature_rows(events)[-1]
    assert row["amount_zscore"] == pytest.approx(3.0)
    assert row["amount_to_mean"] == pytest.approx(2.5)
    # Counts still include the declined one.
    assert row["acct_history_cnt"] == 4
    assert row["acct_declines_1h"] == 1


def test_hour_unusualness_smoothing_on_a_cold_account() -> None:
    """1 - (c_b + 1) / (n + 6) with c_b = 0 and n = 0 gives 5/6."""
    row = last_row([make_event("T0000000", at(10))])
    assert row["hour_unusualness"] == pytest.approx(0.8333333, abs=1e-6)


def test_hour_unusualness_falls_for_a_familiar_hour() -> None:
    """Three prior events in the same 4-hour bucket: 1 - 4/9."""
    events = [make_event(f"T000000{i}", at(9, 10 * i)) for i in range(3)]
    events.append(make_event("T0000003", at(10)))
    assert feature_rows(events)[-1]["hour_unusualness"] == pytest.approx(0.5555556, abs=1e-6)


def test_hour_unusualness_rises_for_an_unfamiliar_hour() -> None:
    """Five prior events, none in the 00:00-04:00 bucket: 1 - 1/11."""
    events = [make_event(f"T000000{i}", at(9, 10 * i)) for i in range(5)]
    events.append(make_event("T0000005", at(2)))
    assert feature_rows(events)[-1]["hour_unusualness"] == pytest.approx(0.9090909, abs=1e-6)


def test_account_age_in_days() -> None:
    """Created 2026-01-01, transacting on 2026-03-01 -> 59 days."""
    row = last_row([make_event("T0000000", at(10))])
    assert row["account_age_days"] == pytest.approx(59 + 10 / 24.0, abs=0.01)


# --- novelty (PLAN §5.2 #20-#23) --------------------------------------------


def test_a_first_device_is_new_and_the_second_use_is_not() -> None:
    events = [
        make_event("T0000000", at(9), device_id="D0000001"),
        make_event("T0000001", at(10), device_id="D0000001"),
        make_event("T0000002", at(11), device_id="D0000002"),
    ]
    assert [row["new_device"] for row in feature_rows(events)] == [1, 0, 1]


def test_distinct_devices_over_thirty_days() -> None:
    events = [
        make_event("T0000000", at(10) - timedelta(days=40), device_id="D0000001"),
        make_event("T0000001", at(10) - timedelta(days=5), device_id="D0000002"),
        make_event("T0000002", at(10) - timedelta(days=1), device_id="D0000003"),
        make_event("T0000003", at(10), device_id="D0000002"),
    ]
    # D1 last used 40 days ago, outside the window; D2 and D3 inside.
    assert feature_rows(events)[-1]["acct_devices_30d"] == 2


def test_a_first_merchant_is_new() -> None:
    events = [
        make_event("T0000000", at(9), merchant_id="M000001"),
        make_event("T0000001", at(10), merchant_id="M000001"),
        make_event("T0000002", at(11), merchant_id="M000002"),
    ]
    assert [row["new_merchant"] for row in feature_rows(events)] == [1, 0, 1]


def test_the_home_city_is_never_new() -> None:
    """PLAN §5.2 #23: the home city counts as seen from account creation.

    Otherwise every account's first transaction would look like it had travelled.
    """
    row = last_row([make_event("T0000000", at(10), city="Mumbai", location=MUMBAI)])
    assert row["new_city"] == 0


def test_another_city_is_new_once() -> None:
    events = [
        make_event("T0000000", at(9), city="Mumbai", location=MUMBAI),
        make_event("T0000001", at(10), city="Delhi", location=DELHI),
        make_event("T0000002", at(11), city="Delhi", location=DELHI),
    ]
    assert [row["new_city"] for row in feature_rows(events)] == [0, 1, 0]


# --- context features (PLAN §5.2 #1-#6) -------------------------------------


def test_stateless_context_features() -> None:
    import math

    row = last_row(
        [
            make_event(
                "T0000000",
                at(2, 30),
                amount=999.0,
                channel="ONLINE",
                country="AE",
                merchant_category="electronics",
            )
        ]
    )
    assert row["log_amount"] == pytest.approx(math.log1p(999.0))
    assert row["hour_of_day"] == 2
    assert row["is_night"] == 1
    assert row["is_online"] == 1
    assert row["is_international"] == 1
    assert row["merchant_category"] == "electronics"


def test_is_night_covers_midnight_to_five() -> None:
    hours = [0, 4, 5, 12, 23]
    rows = [last_row([make_event("T0000000", at(hour))]) for hour in hours]
    assert [row["is_night"] for row in rows] == [1, 1, 0, 0, 0]


# --- entity fan-out (PLAN §5.2 #26-#30) -------------------------------------


def test_device_fan_out_counts_distinct_accounts_in_the_window() -> None:
    """The card-testing signature: one device, many cards, minutes apart."""
    people = {f"A000000{i}": ("Mumbai", MUMBAI, CREATED_AT) for i in range(1, 5)}
    events = [
        make_event("T0000000", at(10, 0), account_id="A0000001", device_id="D0000009"),
        make_event("T0000001", at(10, 30), account_id="A0000002", device_id="D0000009"),
        make_event("T0000002", at(10, 45), account_id="A0000003", device_id="D0000009"),
    ]
    rows = feature_rows(events, accounts=directory(people))
    assert [row["dev_accts_1h"] for row in rows] == [0, 1, 2]


def test_fan_out_uses_last_use_per_account_not_a_visit_count() -> None:
    """PLAN §5.2: each entity stores the LAST time each account used it.

    So an account that used the device an hour ago drops out of the 1-hour window even
    though it is still on the device, while one that came back stays in.
    """
    people = {f"A000000{i}": ("Mumbai", MUMBAI, CREATED_AT) for i in range(1, 5)}
    events = [
        make_event("T0000000", at(10, 0), account_id="A0000001", device_id="D0000009"),
        make_event("T0000001", at(10, 30), account_id="A0000002", device_id="D0000009"),
        make_event("T0000002", at(11, 50), account_id="A0000001", device_id="D0000009"),
        make_event("T0000003", at(11, 55), account_id="A0000003", device_id="D0000009"),
    ]
    rows = feature_rows(events, accounts=directory(people))
    # At 11:55 the window is [10:55, 11:55): A1 came back at 11:50 and is in; A2's last
    # use was 10:30 and is out.
    assert rows[-1]["dev_accts_1h"] == 1
    assert rows[-1]["dev_accts_30d"] == 2


def test_ip_fan_out_behaves_like_device_fan_out() -> None:
    people = {f"A000000{i}": ("Mumbai", MUMBAI, CREATED_AT) for i in range(1, 5)}
    events = [
        make_event("T0000000", at(10, 0), account_id="A0000001", ip="203.0.113.7"),
        make_event("T0000001", at(10, 30), account_id="A0000002", ip="203.0.113.7"),
        make_event("T0000002", at(10, 45), account_id="A0000003", ip="203.0.113.7"),
    ]
    rows = feature_rows(events, accounts=directory(people))
    assert [row["ip_accts_1h"] for row in rows] == [0, 1, 2]


def test_merchant_fan_out_over_thirty_days() -> None:
    """Colluding ring merchants stay small, which is what this is for."""
    people = {f"A000000{i}": ("Mumbai", MUMBAI, CREATED_AT) for i in range(1, 5)}
    events = [
        make_event("T0000000", at(10) - timedelta(days=40), account_id="A0000001"),
        make_event("T0000001", at(10) - timedelta(days=2), account_id="A0000002"),
        make_event("T0000002", at(10), account_id="A0000003"),
    ]
    rows = feature_rows(events, accounts=directory(people))
    assert rows[-1]["mer_accts_30d"] == 1


def test_an_account_does_not_inflate_its_own_entity_fan_out() -> None:
    """Half-open windows again: the event being scored is not yet on the device."""
    events = [make_event("T0000000", at(10), device_id="D0000009")]
    assert feature_rows(events)[-1]["dev_accts_1h"] == 0


# --- engine contract (PLAN §5.4) --------------------------------------------


def test_features_are_computed_before_the_state_is_updated() -> None:
    """PLAN §5.1 rule 3. Two identical events must not produce identical features."""
    events = [make_event("T0000000", at(10)), make_event("T0000001", at(10, 1))]
    rows = feature_rows(events)
    assert rows[0]["acct_history_cnt"] == 0
    assert rows[1]["acct_history_cnt"] == 1


def test_reprocessing_an_event_returns_the_stored_record(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Idempotency (PLAN §5.4): a redelivered event gets the same answer, no state change."""
    from fraud.features.engine import FeatureEngine
    from fraud.features.store_memory import InMemoryStore

    engine = FeatureEngine(InMemoryStore(), directory())
    event = make_event("T0000000", at(10))
    later = make_event("T0000001", at(10, 5))

    first = engine.process(event)
    repeat = engine.process(event)
    assert repeat == first

    # The duplicate must not have advanced the account's history.
    assert engine.process(later)["acct_history_cnt"] == 1


def test_feature_names_cover_every_row_key() -> None:
    """PLAN §5.1 rule 6: spec.FEATURE_NAMES is the only definition of order."""
    from fraud.features.spec import FEATURE_SPEC_VERSION, HOT_FEATURE_NAMES

    row = last_row([make_event("T0000000", at(10))])
    assert set(HOT_FEATURE_NAMES) <= set(row)
    assert len(HOT_FEATURE_NAMES) == 30
    assert FEATURE_SPEC_VERSION == "fs1"


def test_the_account_id_is_never_a_feature() -> None:
    """Leakage rule L8: no identifier-like column may reach the model (PLAN §7.1)."""
    from fraud.features.spec import HOT_FEATURE_NAMES

    banned = {"account_id", "txn_id", "merchant_id", "device_id", "ip", "attack_id", "ring_id"}
    assert not banned & set(HOT_FEATURE_NAMES)
