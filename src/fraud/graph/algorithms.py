"""Graph features from an account-to-account snapshot (PLAN §6.3).

Six features per account. Two are structural, three describe the community an account
sits in, and one carries label information forward.

Three choices from §1 and §6.3 are worth keeping in view:

* **Clustering is computed on the projected graph**, where it is non-zero. On the
  original account-to-entity design it would have been 0 everywhere, because a bipartite
  graph has no triangles.
* **Community IDs are never features.** A Louvain label is arbitrary and changes between
  runs, so a model that learned "community 7 is risky" would be learning noise.
  Community *aggregates* are stable in a way the label is not.
* **PageRank is personalised and seeded only from labels already available at T.**
  Chargebacks arrive 14 days late (§4.6), so a fresher label is not knowable yet. Plain
  PageRank on an undirected graph mostly repeats degree, which `graph_degree` already has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

import networkx as nx
import pandas as pd

from fraud.config import load_yaml

logger = logging.getLogger(__name__)

GRAPH_FEATURE_NAMES: Final[tuple[str, ...]] = (
    "graph_degree",
    "graph_clustering",
    "community_size",
    "community_shared_devices",
    "community_young_share",
    "ppr_risk",
)

SNAPSHOT_COLUMNS: Final[tuple[str, ...]] = ("snapshot_ts", "account_id", *GRAPH_FEATURE_NAMES)


def defaults(config: dict[str, Any] | None = None) -> dict[str, float]:
    """Values for an account with no edges at all (PLAN §6.3)."""
    return dict((config or load_yaml("graph"))["defaults"])


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_ts: pd.Timestamp
    features: pd.DataFrame
    n_nodes: int
    n_edges: int
    n_communities: int
    n_seeds: int


def build_graph(edges: pd.DataFrame) -> nx.Graph:
    """An undirected weighted graph from sorted edge rows."""
    graph = nx.Graph()
    graph.add_weighted_edges_from(edges[["u", "v", "weight"]].itertuples(index=False, name=None))
    return graph


def seed_accounts(
    labels: pd.DataFrame, snapshot_ts: pd.Timestamp, events: pd.DataFrame
) -> list[str]:
    """Accounts with a fraud label already available at T (PLAN §6.2, leakage rule L3).

    ``label_available_at`` is the event time plus the chargeback delay. Using a label
    that becomes available at T or later would seed the graph with knowledge production
    could not have had, and the ring features would then be scoring the answer.
    """
    known = labels[(labels["is_fraud"] == 1) & (labels["label_available_at"] < snapshot_ts)]
    if known.empty:
        return []

    joined = known.merge(events[["txn_id", "account_id"]], on="txn_id", how="inner")
    return sorted(set(joined["account_id"]))


def community_features(
    graph: nx.Graph,
    communities: list[set[str]],
    device_members: pd.DataFrame,
    account_ages: pd.Series,
    young_days: int,
    snapshot_ts: pd.Timestamp,
) -> pd.DataFrame:
    """Size, shared devices and young share, per account's community."""
    by_device = device_members.groupby("device_id")["account_id"].apply(set)
    cutoff = snapshot_ts - pd.Timedelta(days=young_days)

    rows: list[dict[str, Any]] = []
    for members in communities:
        # Devices linking at least two members of THIS community, using the same capped
        # device set the edges came from.
        shared = sum(1 for accounts in by_device if len(accounts & members) >= 2)

        ages = account_ages.reindex(sorted(members))
        known_ages = ages.dropna()
        young_share = float((known_ages > cutoff).mean()) if len(known_ages) else 0.0

        for account in members:
            rows.append(
                {
                    "account_id": account,
                    "community_size": len(members),
                    "community_shared_devices": shared,
                    "community_young_share": young_share,
                }
            )
    return pd.DataFrame(rows, columns=["account_id", *list(rows[0])[1:]] if rows else None)


def snapshot_features(
    edges: pd.DataFrame,
    snapshot_ts: pd.Timestamp,
    *,
    device_members: pd.DataFrame,
    account_created: pd.Series,
    seeds: list[str],
    config: dict[str, Any] | None = None,
) -> Snapshot:
    """All six graph features for every account in the snapshot graph (PLAN §6.3)."""
    cfg = config or load_yaml("graph")
    graph = build_graph(edges)

    if graph.number_of_nodes() == 0:
        empty = pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))
        return Snapshot(snapshot_ts, empty, 0, 0, 0, 0)

    degree = dict(graph.degree(weight="weight"))
    clustering = nx.clustering(graph)
    communities = nx.community.louvain_communities(
        graph, weight="weight", seed=int(cfg["louvain_seed"])
    )

    present = [account for account in seeds if account in graph]
    ppr: dict[str, float] = {}
    if present:
        ppr = nx.pagerank(
            graph,
            alpha=float(cfg["pagerank_alpha"]),
            personalization=dict.fromkeys(present, 1.0),
            weight="weight",
        )

    # Accounts in a component the seeds cannot reach keep a tiny residue rather than a
    # clean zero: power iteration starts from a uniform vector and stops at the default
    # tolerance. It lands around 1e-5 against seeded values in the ones, so it cannot
    # move a tree split, and forcing it to zero would cost iterations for nothing.
    #
    # Scaled by node count so the value does not shrink as the graph grows (§6.3).
    scale = graph.number_of_nodes()
    frame = pd.DataFrame(
        {
            "account_id": sorted(graph.nodes),
        }
    )
    frame["graph_degree"] = frame["account_id"].map(degree).astype(float)
    frame["graph_clustering"] = frame["account_id"].map(clustering).astype(float)
    frame["ppr_risk"] = frame["account_id"].map(lambda node: ppr.get(node, 0.0) * scale)

    community = community_features(
        graph,
        communities,
        device_members,
        account_created,
        int(cfg["young_days"]),
        snapshot_ts,
    )
    frame = frame.merge(community, on="account_id", how="left")
    frame["snapshot_ts"] = snapshot_ts

    return Snapshot(
        snapshot_ts=snapshot_ts,
        features=frame[list(SNAPSHOT_COLUMNS)].sort_values("account_id", ignore_index=True),
        n_nodes=graph.number_of_nodes(),
        n_edges=graph.number_of_edges(),
        n_communities=len(communities),
        n_seeds=len(present),
    )
