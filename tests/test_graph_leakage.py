"""Graph leakage rules L2 and L3 (PLAN §7.1, §6.2, §13).

These are the two rules a graph layer is most likely to break, and breaking either is
invisible in the metrics: the model simply gets better for reasons that will not survive
production.

* **L2** — a snapshot at T is built only from events strictly before T.
* **L3** — label-based features use only labels whose `label_available_at` is before T.
  Chargebacks arrive 14 days late, so a label that becomes available at T or later is not
  knowable yet.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fraud.config import load_yaml
from fraud.graph.algorithms import seed_accounts, snapshot_features
from fraud.graph.projection import project

T = pd.Timestamp("2026-02-01")


def _events(rows: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "txn_id": f"T{index:04d}",
                "event_time": pd.Timestamp(when),
                "account_id": account,
                "device_id": device,
                "ip": ip,
            }
            for index, (account, device, ip, when) in enumerate(rows)
        ]
    )


def _labels(rows: list[tuple[str, int, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"txn_id": txn, "is_fraud": fraud, "label_available_at": pd.Timestamp(when)}
            for txn, fraud, when in rows
        ]
    )


# --- L2: no edge may come from the future -----------------------------------


def test_an_event_exactly_at_the_snapshot_is_excluded() -> None:
    """The window is [T-30d, T). An event at T belongs to the next snapshot."""
    frame = _events(
        [("A1", "D1", "IP1", "2026-02-01 00:00:00"), ("A2", "D1", "IP2", "2026-02-01 00:00:00")]
    )
    assert project(frame, T).empty


def test_an_event_one_second_before_the_snapshot_is_included() -> None:
    frame = _events(
        [("A1", "D1", "IP1", "2026-01-31 23:59:59"), ("A2", "D1", "IP2", "2026-01-31 23:59:59")]
    )
    assert len(project(frame, T)) == 1


def test_events_after_the_snapshot_never_create_edges() -> None:
    """The failure this prevents: a ring that forms next week showing up this week."""
    frame = _events([("A1", "D1", "IP1", "2026-02-05"), ("A2", "D1", "IP2", "2026-02-06")])
    assert project(frame, T).empty


def test_an_edge_appears_only_once_its_events_exist() -> None:
    """The same data, two snapshots: nothing at T, an edge a week later."""
    frame = _events([("A1", "D1", "IP1", "2026-02-03"), ("A2", "D1", "IP2", "2026-02-04")])
    assert project(frame, T).empty
    assert len(project(frame, T + pd.Timedelta(days=7))) == 1


def test_events_older_than_the_lookback_are_dropped() -> None:
    """A device two accounts shared a year ago is not a link today (§6.1)."""
    lookback = int(load_yaml("graph")["lookback_days"])
    stale = (T - pd.Timedelta(days=lookback + 1)).isoformat()
    frame = _events([("A1", "D1", "IP1", stale), ("A2", "D1", "IP2", stale)])

    assert project(frame, T).empty


def test_the_lookback_boundary_is_inclusive() -> None:
    lookback = int(load_yaml("graph")["lookback_days"])
    edge = (T - pd.Timedelta(days=lookback)).isoformat()
    frame = _events([("A1", "D1", "IP1", edge), ("A2", "D1", "IP2", edge)])

    assert len(project(frame, T)) == 1


# --- L3: no label may be used before it would have arrived ------------------


def test_a_label_available_one_second_after_the_snapshot_is_not_a_seed() -> None:
    """The sharpest version of L3, and the easiest to get wrong by one comparison."""
    labels = _labels([("T0000", 1, "2026-02-01 00:00:01")])
    events = pd.DataFrame({"txn_id": ["T0000"], "account_id": ["A1"]})

    assert seed_accounts(labels, T, events) == []


def test_a_label_available_exactly_at_the_snapshot_is_not_a_seed() -> None:
    """`label_available_at < T`, strictly, so T itself is still the future."""
    labels = _labels([("T0000", 1, "2026-02-01 00:00:00")])
    events = pd.DataFrame({"txn_id": ["T0000"], "account_id": ["A1"]})

    assert seed_accounts(labels, T, events) == []


def test_a_label_available_one_second_before_is_a_seed() -> None:
    labels = _labels([("T0000", 1, "2026-01-31 23:59:59")])
    events = pd.DataFrame({"txn_id": ["T0000"], "account_id": ["A1"]})

    assert seed_accounts(labels, T, events) == ["A1"]


def test_the_chargeback_delay_holds_a_recent_fraud_back() -> None:
    """A fraud two days ago is still unknown: the label arrives 14 days later (§4.6)."""
    delay = int(load_yaml("graph")["label_delay_days"])
    event_time = T - pd.Timedelta(days=2)
    labels = _labels([("T0000", 1, (event_time + pd.Timedelta(days=delay)).isoformat())])
    events = pd.DataFrame({"txn_id": ["T0000"], "account_id": ["A1"]})

    assert seed_accounts(labels, T, events) == []


def test_an_old_fraud_has_had_time_to_surface() -> None:
    delay = int(load_yaml("graph")["label_delay_days"])
    event_time = T - pd.Timedelta(days=delay + 5)
    labels = _labels([("T0000", 1, (event_time + pd.Timedelta(days=delay)).isoformat())])
    events = pd.DataFrame({"txn_id": ["T0000"], "account_id": ["A1"]})

    assert seed_accounts(labels, T, events) == ["A1"]


def test_seeding_changes_the_feature_so_the_rule_has_teeth() -> None:
    """If a withheld label made no difference, L3 would be untestable.

    Same graph twice: once with the label available, once not. ppr_risk must differ.
    """
    edges = pd.DataFrame([("A1", "A2", 1)], columns=["u", "v", "weight"])
    empty_members = pd.DataFrame(columns=["device_id", "account_id"])
    ages = pd.Series(dtype="datetime64[ns]")

    seeded = snapshot_features(
        edges, T, device_members=empty_members, account_created=ages, seeds=["A1"]
    )
    withheld = snapshot_features(
        edges, T, device_members=empty_members, account_created=ages, seeds=[]
    )

    assert seeded.n_seeds == 1
    assert withheld.n_seeds == 0
    assert seeded.features["ppr_risk"].max() > 0.5
    assert withheld.features["ppr_risk"].max() == pytest.approx(0.0)


def test_seeds_are_sorted_for_determinism() -> None:
    """Unordered seeds would make the personalisation vector build differently (§6.6)."""
    labels = _labels([("T0000", 1, "2026-01-05"), ("T0001", 1, "2026-01-06")])
    events = pd.DataFrame({"txn_id": ["T0000", "T0001"], "account_id": ["A9", "A1"]})

    assert seed_accounts(labels, T, events) == ["A1", "A9"]
