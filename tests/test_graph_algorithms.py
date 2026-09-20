"""Graph algorithms on toy graphs (PLAN §6.3, §13).

The headline check is the one from §1: clustering is 0 on the bipartite account-to-entity
graph and non-zero on the projected account-to-account graph. That single fact is why the
projection exists at all.
"""

from __future__ import annotations

from typing import Any

import networkx as nx
import pandas as pd
import pytest

from fraud.config import load_yaml
from fraud.graph.algorithms import (
    GRAPH_FEATURE_NAMES,
    SNAPSHOT_COLUMNS,
    build_graph,
    defaults,
    seed_accounts,
    snapshot_features,
)

T = pd.Timestamp("2026-02-01")


def edges(rows: list[tuple[str, str, int]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["u", "v", "weight"])


def members(pairs: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(pairs, columns=["device_id", "account_id"])


def created(ages: dict[str, int]) -> pd.Series:
    return pd.Series({account: T - pd.Timedelta(days=days) for account, days in ages.items()})


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return load_yaml("graph")


# --- the reason the projection exists (PLAN §1) -----------------------------


def test_clustering_is_zero_on_the_bipartite_graph() -> None:
    """Accounts linked to entities: no triangles, so clustering is 0 everywhere.

    This is the defect the projection fixes. Left as-is, graph_clustering would have
    been a column of zeros.
    """
    bipartite = nx.Graph()
    bipartite.add_edges_from([("A1", "D1"), ("A2", "D1"), ("A3", "D1")])

    assert set(nx.clustering(bipartite).values()) == {0.0}


def test_clustering_is_positive_on_the_projected_graph() -> None:
    """The same household, projected onto accounts, is a triangle."""
    graph = build_graph(edges([("A1", "A2", 1), ("A1", "A3", 1), ("A2", "A3", 1)]))
    assert all(value == pytest.approx(1.0) for value in nx.clustering(graph).values())


# --- structural features ----------------------------------------------------


def test_degree_is_weighted() -> None:
    result = snapshot_features(
        edges([("A1", "A2", 3), ("A1", "A3", 1)]),
        T,
        device_members=members([]),
        account_created=created({}),
        seeds=[],
    )
    row = result.features.set_index("account_id")
    assert row.loc["A1", "graph_degree"] == pytest.approx(4.0)
    assert row.loc["A2", "graph_degree"] == pytest.approx(3.0)


def test_output_has_the_documented_columns() -> None:
    result = snapshot_features(
        edges([("A1", "A2", 1)]),
        T,
        device_members=members([]),
        account_created=created({}),
        seeds=[],
    )
    assert tuple(result.features.columns) == SNAPSHOT_COLUMNS
    assert len(GRAPH_FEATURE_NAMES) == 6


def test_an_empty_graph_produces_no_rows() -> None:
    result = snapshot_features(
        edges([]), T, device_members=members([]), account_created=created({}), seeds=[]
    )
    assert result.features.empty
    assert result.n_nodes == 0


def test_defaults_exist_for_accounts_outside_the_graph(config: dict[str, Any]) -> None:
    """Most accounts are in no graph at all; §6.3 fixes what they get."""
    values = defaults(config)
    assert set(values) == set(GRAPH_FEATURE_NAMES)
    assert values["community_size"] == 1
    assert values["graph_degree"] == 0
    assert values["ppr_risk"] == 0


# --- community features -----------------------------------------------------


def test_community_size_counts_the_whole_component() -> None:
    result = snapshot_features(
        edges([("A1", "A2", 1), ("A2", "A3", 1), ("B1", "B2", 1)]),
        T,
        device_members=members([]),
        account_created=created({}),
        seeds=[],
    )
    sizes = result.features.set_index("account_id")["community_size"]
    assert sizes["A1"] == 3
    assert sizes["B1"] == 2


def test_shared_devices_counts_devices_linking_two_members() -> None:
    result = snapshot_features(
        edges([("A1", "A2", 1), ("A2", "A3", 1)]),
        T,
        device_members=members([("D1", "A1"), ("D1", "A2"), ("D2", "A3"), ("D2", "Z9")]),
        account_created=created({}),
        seeds=[],
    )
    row = result.features.set_index("account_id")
    # D1 links two members of the community; D2 links only one of them plus an outsider.
    assert row.loc["A1", "community_shared_devices"] == 1


def test_young_share_uses_the_configured_window(config: dict[str, Any]) -> None:
    young = int(config["young_days"])
    # A clique, so Louvain cannot split it: a path of four would come back as two
    # communities and the share would be measured over the wrong members.
    clique = edges(
        [
            ("A1", "A2", 1),
            ("A1", "A3", 1),
            ("A1", "A4", 1),
            ("A2", "A3", 1),
            ("A2", "A4", 1),
            ("A3", "A4", 1),
        ]
    )
    result = snapshot_features(
        clique,
        T,
        device_members=members([]),
        account_created=created({"A1": 1, "A2": 5, "A3": young + 10, "A4": young + 50}),
        seeds=[],
        config=config,
    )
    assert (result.features["community_size"] == 4).all()
    assert result.features["community_young_share"].iloc[0] == pytest.approx(0.5)


def test_community_ids_are_never_a_feature() -> None:
    """A Louvain label is arbitrary and changes between runs (§6.3)."""
    result = snapshot_features(
        edges([("A1", "A2", 1)]),
        T,
        device_members=members([]),
        account_created=created({}),
        seeds=[],
    )
    assert not {"community", "community_id", "louvain"} & set(result.features.columns)


# --- personalised PageRank --------------------------------------------------


def test_ppr_is_zero_without_seeds() -> None:
    """No fraud label is available yet, so there is nothing to propagate."""
    result = snapshot_features(
        edges([("A1", "A2", 1)]),
        T,
        device_members=members([]),
        account_created=created({}),
        seeds=[],
    )
    assert (result.features["ppr_risk"] == 0.0).all()


def test_ppr_reaches_only_the_seeded_component() -> None:
    """The point of seeding: risk flows along shared devices, not along degree.

    Rank need not be highest at the seed itself. A1's neighbour A2 is the hub here and
    collects mass from both sides. What matters is that risk stays inside the component
    the seed sits in and decays with distance from it.
    """
    result = snapshot_features(
        edges([("A1", "A2", 1), ("A2", "A3", 1), ("A3", "A4", 1), ("Z1", "Z2", 1)]),
        T,
        device_members=members([]),
        account_created=created({}),
        seeds=["A1"],
    )
    row = result.features.set_index("account_id")["ppr_risk"]

    # Not exactly zero: power iteration starts from a uniform vector and leaves a
    # residue on unreachable nodes at the default tolerance. It is five orders of
    # magnitude below the seeded component, so it cannot influence a split.
    assert row["Z1"] < 1e-3
    assert row["Z2"] < 1e-3
    assert min(row["A1"], row["A2"], row["A3"]) > 1000 * max(row["Z1"], row["Z2"])
    # Decay with distance from the seed.
    assert row["A2"] > row["A3"] > row["A4"]


def test_a_seed_outside_the_graph_is_ignored() -> None:
    result = snapshot_features(
        edges([("A1", "A2", 1)]),
        T,
        device_members=members([]),
        account_created=created({}),
        seeds=["GHOST"],
    )
    assert result.n_seeds == 0
    assert (result.features["ppr_risk"] == 0.0).all()


# --- determinism (PLAN §6.6) ------------------------------------------------


def test_snapshots_are_reproducible() -> None:
    """Louvain is seeded; without that the communities differ between runs."""
    data = edges([("A1", "A2", 1), ("A2", "A3", 2), ("A3", "A4", 1), ("A4", "A1", 1)])
    first = snapshot_features(
        data, T, device_members=members([]), account_created=created({}), seeds=["A1"]
    )
    second = snapshot_features(
        data, T, device_members=members([]), account_created=created({}), seeds=["A1"]
    )
    pd.testing.assert_frame_equal(first.features, second.features)


# --- seeds and the label delay (PLAN §6.2) ----------------------------------


def _labels(rows: list[tuple[str, int, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"txn_id": txn, "is_fraud": is_fraud, "label_available_at": pd.Timestamp(when)}
            for txn, is_fraud, when in rows
        ]
    )


def test_seeds_use_only_labels_already_available() -> None:
    labels = _labels([("T1", 1, "2026-01-20"), ("T2", 1, "2026-03-01")])
    events = pd.DataFrame({"txn_id": ["T1", "T2"], "account_id": ["A1", "A2"]})

    assert seed_accounts(labels, T, events) == ["A1"]


def test_legitimate_rows_are_never_seeds() -> None:
    labels = _labels([("T1", 0, "2026-01-20")])
    events = pd.DataFrame({"txn_id": ["T1"], "account_id": ["A1"]})

    assert seed_accounts(labels, T, events) == []
