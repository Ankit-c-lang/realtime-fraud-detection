"""The two-step point-in-time join (PLAN §6.4, §13).

§6.4 names three edge cases, and the third is the reason the join has two steps at all:
an account present in an older snapshot but absent from the one that applies must get
defaults, not its stale values from weeks ago.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fraud.features.spec import WARM_FEATURE_NAMES
from fraud.graph.algorithms import defaults
from fraud.graph.join import attach_snapshots, build_training_table

CALENDAR = pd.DataFrame({"snapshot_ts": pd.to_datetime(["2026-02-01", "2026-02-02", "2026-02-03"])})


def features(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    """(txn_id, account_id, event_time)."""
    return pd.DataFrame(
        [
            {"txn_id": txn, "account_id": account, "event_time": pd.Timestamp(when)}
            for txn, account, when in rows
        ]
    )


def graph_rows(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """(snapshot_ts, account_id, degree), other features filled in."""
    return pd.DataFrame(
        [
            {
                "snapshot_ts": pd.Timestamp(when),
                "account_id": account,
                "graph_degree": degree,
                "graph_clustering": 0.5,
                "community_size": 4,
                "community_shared_devices": 2,
                "community_young_share": 0.25,
                "ppr_risk": 1.5,
            }
            for when, account, degree in rows
        ]
    )


def test_every_warm_feature_is_attached() -> None:
    joined = attach_snapshots(
        features([("T1", "A1", "2026-02-02 12:00")]),
        graph_rows([("2026-02-02", "A1", 3.0)]),
        CALENDAR,
    )
    assert set(WARM_FEATURE_NAMES) <= set(joined.columns)
    assert "graph_snapshot_ts" in joined.columns


def test_an_event_uses_the_most_recent_snapshot_at_or_before_it() -> None:
    joined = attach_snapshots(
        features([("T1", "A1", "2026-02-02 12:00")]),
        graph_rows([("2026-02-01", "A1", 1.0), ("2026-02-02", "A1", 9.0)]),
        CALENDAR,
    )
    assert joined["graph_snapshot_ts"].iloc[0] == pd.Timestamp("2026-02-02")
    assert joined["graph_degree"].iloc[0] == pytest.approx(9.0)


# --- §6.4 edge case 1: an event exactly at T --------------------------------


def test_an_event_exactly_at_a_snapshot_uses_that_snapshot() -> None:
    """Snapshot T holds only events strictly before T, so an event at T may use it."""
    joined = attach_snapshots(
        features([("T1", "A1", "2026-02-02 00:00:00")]),
        graph_rows([("2026-02-01", "A1", 1.0), ("2026-02-02", "A1", 9.0)]),
        CALENDAR,
    )
    assert joined["graph_snapshot_ts"].iloc[0] == pd.Timestamp("2026-02-02")
    assert joined["graph_degree"].iloc[0] == pytest.approx(9.0)


def test_an_event_one_second_earlier_uses_the_previous_snapshot() -> None:
    joined = attach_snapshots(
        features([("T1", "A1", "2026-02-01 23:59:59")]),
        graph_rows([("2026-02-01", "A1", 1.0), ("2026-02-02", "A1", 9.0)]),
        CALENDAR,
    )
    assert joined["graph_snapshot_ts"].iloc[0] == pd.Timestamp("2026-02-01")
    assert joined["graph_degree"].iloc[0] == pytest.approx(1.0)


# --- §6.4 edge case 2: an event before the first snapshot -------------------


def test_an_event_before_the_first_snapshot_gets_defaults() -> None:
    """There is no graph yet, so there is nothing to attach."""
    joined = attach_snapshots(
        features([("T1", "A1", "2026-01-15 09:00")]),
        graph_rows([("2026-02-01", "A1", 7.0)]),
        CALENDAR,
    )
    fallback = defaults()

    assert pd.isna(joined["graph_snapshot_ts"].iloc[0])
    for name in WARM_FEATURE_NAMES:
        assert joined[name].iloc[0] == pytest.approx(fallback[name])


# --- §6.4 edge case 3: the stale-value trap ---------------------------------


def test_an_account_absent_from_the_applicable_snapshot_gets_defaults() -> None:
    """The reason this join has two steps.

    A1 was in the graph on 01 Feb and has since dropped out: its shared device aged past
    the 30-day lookback. An ASOF keyed on the account would return the 01 Feb row, and
    the model would learn from a graph position the account no longer has. Nothing about
    the output would look wrong.
    """
    joined = attach_snapshots(
        features([("T1", "A1", "2026-02-03 08:00")]),
        # Present on the 1st, absent on the 2nd and 3rd.
        graph_rows([("2026-02-01", "A1", 42.0)]),
        CALENDAR,
    )
    fallback = defaults()

    assert joined["graph_snapshot_ts"].iloc[0] == pd.Timestamp("2026-02-03")
    assert joined["graph_degree"].iloc[0] == pytest.approx(fallback["graph_degree"])
    assert joined["community_size"].iloc[0] == fallback["community_size"]
    assert joined["ppr_risk"].iloc[0] == pytest.approx(fallback["ppr_risk"])


def test_another_account_in_the_snapshot_does_not_leak_across() -> None:
    joined = attach_snapshots(
        features([("T1", "A1", "2026-02-02 08:00")]),
        graph_rows([("2026-02-02", "A2", 99.0)]),
        CALENDAR,
    )
    assert joined["graph_degree"].iloc[0] == pytest.approx(defaults()["graph_degree"])


def test_an_account_rejoining_the_graph_picks_the_values_back_up() -> None:
    joined = attach_snapshots(
        features([("T1", "A1", "2026-02-02 08:00"), ("T2", "A1", "2026-02-03 08:00")]),
        graph_rows([("2026-02-01", "A1", 42.0), ("2026-02-03", "A1", 5.0)]),
        CALENDAR,
    )
    values = joined.set_index("txn_id")["graph_degree"]
    assert values["T1"] == pytest.approx(defaults()["graph_degree"])  # gone on the 2nd
    assert values["T2"] == pytest.approx(5.0)  # back on the 3rd


# --- shape and labels -------------------------------------------------------


def test_the_join_preserves_every_event_exactly_once() -> None:
    rows = features(
        [
            ("T1", "A1", "2026-02-02 08:00"),
            ("T2", "A2", "2026-02-02 09:00"),
            ("T3", "A1", "2026-01-01 09:00"),
        ]
    )
    joined = attach_snapshots(rows, graph_rows([("2026-02-02", "A1", 3.0)]), CALENDAR)

    assert len(joined) == 3
    assert joined["txn_id"].is_unique


def test_an_empty_graph_still_joins() -> None:
    """No snapshot has any account yet; every event takes defaults."""
    joined = attach_snapshots(
        features([("T1", "A1", "2026-02-02 08:00")]),
        pd.DataFrame(columns=["snapshot_ts", "account_id", *WARM_FEATURE_NAMES]),
        CALENDAR,
    )
    assert joined["graph_degree"].iloc[0] == pytest.approx(defaults()["graph_degree"])


def test_the_training_table_carries_labels() -> None:
    labels = pd.DataFrame(
        {
            "txn_id": ["T1"],
            "is_fraud": [1],
            "fraud_type": ["RING"],
            "attack_id": ["RING0001"],
            "ring_id": ["RING0001"],
            "label_available_at": [pd.Timestamp("2026-02-16")],
        }
    )
    table = build_training_table(
        features([("T1", "A1", "2026-02-02 08:00")]),
        graph_rows([("2026-02-02", "A1", 3.0)]),
        CALENDAR,
        labels,
    )
    assert table["is_fraud"].iloc[0] == 1
    assert table["graph_degree"].iloc[0] == pytest.approx(3.0)


def test_rows_come_back_in_event_order() -> None:
    rows = features([("T2", "A1", "2026-02-02 09:00"), ("T1", "A1", "2026-02-02 08:00")])
    joined = attach_snapshots(rows, graph_rows([("2026-02-02", "A1", 3.0)]), CALENDAR)
    assert joined["event_time"].is_monotonic_increasing
