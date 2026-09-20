"""Account-to-account edges from shared devices and IPs (PLAN §6.1).

The original design linked accounts to devices, merchants and IPs directly. That graph is
bipartite, so it has no triangles and every clustering coefficient is 0 — the feature
would have been a column of zeros. Projecting onto accounts, where two accounts are
joined when they share an entity, gives a graph where clustering means something (§1).

Merchants are excluded. A popular merchant links thousands of unrelated shoppers, which
would fuse most of the population into one component.

**The fan-out caps are what make this safe.** An entity is used only when it links between
``min_accounts`` and its cap. Without them a card-testing device would join its unrelated
victims into a fake ring, and personalised PageRank would then push that risk onto those
victims' later, perfectly legitimate purchases. The caps come from measured train-period
distributions (reports/entity_fanout.md).

SQL builds the graph and NetworkX runs the algorithms, each doing what it is good at.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from fraud.config import load_yaml

logger = logging.getLogger(__name__)

EDGE_COLUMNS = ("u", "v", "weight")

# ORDER BY is not cosmetic: Louvain is order-sensitive, so unsorted edges make the
# communities differ between identical runs and the parity check in §6.6 fail.
_PROJECTION_SQL = """
WITH win AS (
  SELECT account_id, device_id, ip
  FROM events
  WHERE event_time >= $window_start AND event_time < $snapshot_ts
),
ad AS (SELECT DISTINCT account_id, device_id AS ent FROM win),
dev_ok AS (
  SELECT ent FROM ad GROUP BY ent
  HAVING COUNT(*) BETWEEN $min_accounts AND $device_cap
),
ai AS (SELECT DISTINCT account_id, ip AS ent FROM win),
ip_ok AS (
  SELECT ent FROM ai GROUP BY ent
  HAVING COUNT(*) BETWEEN $min_accounts AND $ip_cap
),
pairs AS (
  SELECT a.account_id AS u, b.account_id AS v
  FROM ad a JOIN ad b ON a.ent = b.ent JOIN dev_ok d ON d.ent = a.ent
  WHERE a.account_id < b.account_id
  UNION ALL
  SELECT a.account_id AS u, b.account_id AS v
  FROM ai a JOIN ai b ON a.ent = b.ent JOIN ip_ok i ON i.ent = a.ent
  WHERE a.account_id < b.account_id
)
SELECT u, v, COUNT(*) AS weight
FROM pairs
GROUP BY u, v
ORDER BY u, v
"""

_ENTITY_SQL = """
WITH win AS (
  SELECT DISTINCT account_id, {column} AS ent
  FROM events
  WHERE event_time >= $window_start AND event_time < $snapshot_ts
)
SELECT ent, COUNT(*) AS accounts
FROM win GROUP BY ent
HAVING COUNT(*) BETWEEN $min_accounts AND $cap
ORDER BY ent
"""


def graph_config() -> dict[str, Any]:
    return load_yaml("graph")


def project(
    events: pd.DataFrame,
    snapshot_ts: pd.Timestamp,
    config: dict[str, Any] | None = None,
    connection: duckdb.DuckDBPyConnection | None = None,
) -> pd.DataFrame:
    """Edges for one snapshot, sorted (PLAN §6.1).

    ``events`` must carry ``event_time``, ``account_id``, ``device_id`` and ``ip``. Only
    rows strictly before ``snapshot_ts`` are read, which is leakage rule L2: a snapshot
    may never see the future it is about to be joined to.
    """
    cfg = config or graph_config()
    window_start = snapshot_ts - pd.Timedelta(days=int(cfg["lookback_days"]))

    own = connection is None
    # In-memory and read-only over the frame: never a .duckdb file (§3.7, invariant 7).
    conn = connection or duckdb.connect(database=":memory:")
    try:
        conn.register("events", events)
        result = conn.execute(
            _PROJECTION_SQL,
            {
                "window_start": window_start.to_pydatetime(),
                "snapshot_ts": snapshot_ts.to_pydatetime(),
                "min_accounts": int(cfg["min_accounts"]),
                "device_cap": int(cfg["device_cap"]),
                "ip_cap": int(cfg["ip_cap"]),
            },
        ).fetch_df()
    finally:
        if own:
            conn.close()

    return result[list(EDGE_COLUMNS)]


def capped_entities(
    events: pd.DataFrame,
    snapshot_ts: pd.Timestamp,
    kind: str,
    config: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Entities of one kind that survive their cap, with their account counts.

    ``community_shared_devices`` counts these, so it has to use exactly the same rule
    the edges did.
    """
    cfg = config or graph_config()
    column = {"device": "device_id", "ip": "ip"}[kind]
    cap = int(cfg["device_cap"] if kind == "device" else cfg["ip_cap"])
    window_start = snapshot_ts - pd.Timedelta(days=int(cfg["lookback_days"]))

    conn = duckdb.connect(database=":memory:")
    try:
        conn.register("events", events)
        return conn.execute(
            _ENTITY_SQL.format(column=column),
            {
                "window_start": window_start.to_pydatetime(),
                "snapshot_ts": snapshot_ts.to_pydatetime(),
                "min_accounts": int(cfg["min_accounts"]),
                "cap": cap,
            },
        ).fetch_df()
    finally:
        conn.close()


def device_membership(
    events: pd.DataFrame, snapshot_ts: pd.Timestamp, config: dict[str, Any] | None = None
) -> pd.DataFrame:
    """(device_id, account_id) pairs for devices inside the cap."""
    cfg = config or graph_config()
    window_start = snapshot_ts - pd.Timedelta(days=int(cfg["lookback_days"]))
    kept = set(capped_entities(events, snapshot_ts, "device", cfg)["ent"])

    window = events[(events["event_time"] >= window_start) & (events["event_time"] < snapshot_ts)]
    pairs = window[window["device_id"].isin(kept)][["device_id", "account_id"]]
    return pairs.drop_duplicates().sort_values(["device_id", "account_id"], ignore_index=True)


def write_edges(edges: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    edges.to_parquet(path, compression="zstd", index=False)
    logger.info("wrote %s (%d edges)", path, len(edges))
