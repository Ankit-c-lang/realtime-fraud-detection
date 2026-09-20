"""Point-in-time join of graph snapshots onto events (PLAN §6.4).

**Why this is two steps and not one ASOF JOIN.** The obvious implementation —
`ASOF JOIN graph_features ON account_id, event_time >= snapshot_ts` — is wrong, and
wrong in a way that looks fine. It returns the most recent snapshot *in which that
account appeared*. An account whose shared device aged out of the 30-day lookback simply
stops appearing, and the join then hands back its values from weeks ago instead of the
defaults. The model learns from a graph position the account no longer has, and nothing
about the output looks unusual.

So: step one asks which snapshot applies to each event, using the calendar alone. Step two
looks that account up in *that* snapshot, and takes the §6.3 defaults when it is absent.
An account that has left the graph gets defaults, which is the truth.

Snapshot T is built from events strictly before T, so an event at exactly T may use
snapshot T. The online path applies the same rule (§6.5).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from fraud.config import Settings, load_yaml
from fraud.features.spec import WARM_FEATURE_NAMES
from fraud.graph.algorithms import defaults
from fraud.graph.snapshots import load_snapshots

logger = logging.getLogger(__name__)

LABEL_COLUMNS = ("is_fraud", "fraud_type", "attack_id", "ring_id", "label_available_at")

# Step 1 alone. The calendar has no account in it, which is the whole point: which
# snapshot applies depends on the clock, never on whether the account happened to appear.
_SNAPSHOT_FOR_EVENT = """
SELECT f.txn_id, s.snapshot_ts
FROM features f
ASOF LEFT JOIN calendar s
  ON f.event_time >= s.snapshot_ts
"""


def attach_snapshots(
    features: pd.DataFrame,
    graph_features: pd.DataFrame,
    calendar: pd.DataFrame,
    config: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Attach each event's applicable snapshot values, or defaults (PLAN §6.4)."""
    cfg = config or load_yaml("graph")
    fallback = defaults(cfg)

    calendar_only = calendar[["snapshot_ts"]].sort_values("snapshot_ts", ignore_index=True)
    graph = (
        graph_features
        if len(graph_features)
        else pd.DataFrame(columns=["snapshot_ts", "account_id", *WARM_FEATURE_NAMES])
    )

    coalesced = ",\n       ".join(
        f"COALESCE(g.{name}, {fallback[name]!r}) AS {name}" for name in WARM_FEATURE_NAMES
    )

    conn = duckdb.connect(database=":memory:")
    try:
        conn.register("features", features)
        conn.register("calendar", calendar_only)
        conn.register("graph_features", graph)
        conn.execute(f"CREATE TEMP TABLE ev_snap AS {_SNAPSHOT_FOR_EVENT}")

        return conn.execute(
            f"""
            SELECT f.*,
                   e.snapshot_ts AS graph_snapshot_ts,
                   {coalesced}
            FROM features f
            JOIN ev_snap e USING (txn_id)
            LEFT JOIN graph_features g
                   ON g.account_id = f.account_id
                  AND g.snapshot_ts = e.snapshot_ts
            ORDER BY f.event_time, f.txn_id
            """
        ).fetch_df()
    finally:
        conn.close()


def build_training_table(
    features: pd.DataFrame,
    graph_features: pd.DataFrame,
    calendar: pd.DataFrame,
    labels: pd.DataFrame,
    config: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """The 36-feature table, with labels joined in last (PLAN §6.4)."""
    joined = attach_snapshots(features, graph_features, calendar, config)
    return joined.merge(labels, on="txn_id", how="left", validate="one_to_one")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Point-in-time graph join (PLAN §6.4).")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()

    features = pd.read_parquet(settings.features_dir / "hot_features.parquet")
    graph_features = load_snapshots(settings.graph_offline_dir)
    calendar = pd.read_parquet(settings.graph_offline_dir / "calendar.parquet")
    labels = pd.read_parquet(
        settings.raw_dir / "labels.parquet", columns=["txn_id", *LABEL_COLUMNS]
    )
    logger.info(
        "joining %s events against %s snapshot rows over %d snapshots",
        f"{len(features):,}",
        f"{len(graph_features):,}",
        len(calendar),
    )

    table = build_training_table(features, graph_features, calendar, labels)
    destination = args.out or settings.features_dir / "training_table.parquet"
    table.to_parquet(destination, compression="zstd", index=False)

    attached = (table["graph_degree"] > 0).mean()
    logger.info(
        "wrote %s (%d rows); %.1f%% of events have a non-default graph position",
        destination,
        len(table),
        100 * attached,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
