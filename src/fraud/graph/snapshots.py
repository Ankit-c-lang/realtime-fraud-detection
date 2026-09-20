"""Daily offline graph snapshots (PLAN §6.2, §6.3).

Run as ``python -m fraud.graph.snapshots`` (part of ``make graph``).

One snapshot per day at 00:00 IST. Each is built only from events strictly before its own
timestamp and seeded only from labels already available then, so the whole series is
replayable as production would have seen it (leakage rules L2 and L3).

Snapshots are written per directory, ``snapshot_ts=<T>/part.parquet``, holding rows only
for accounts that are actually in the graph. Most accounts never share a capped entity
with anyone, and writing a default row for all 22,000 of them 89 times over would be
almost entirely zeros. The join supplies the defaults instead (§6.4).
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from fraud.config import Settings, load_yaml
from fraud.graph.algorithms import SNAPSHOT_COLUMNS, seed_accounts, snapshot_features
from fraud.graph.projection import device_membership, project

logger = logging.getLogger(__name__)

CALENDAR_COLUMNS = ("snapshot_ts", "n_nodes", "n_edges", "n_communities", "n_seeds", "seconds")
PROJECTION_COLUMNS = ("txn_id", "event_time", "account_id", "device_id", "ip")


@dataclass(frozen=True, slots=True)
class SnapshotRun:
    calendar: pd.DataFrame
    total_seconds: float

    @property
    def slowest(self) -> float:
        return float(self.calendar["seconds"].max()) if len(self.calendar) else 0.0


def calendar_dates(config: dict[str, Any] | None = None) -> list[pd.Timestamp]:
    """Every snapshot time (PLAN §6.2). Events before the first one get defaults."""
    cfg = config or load_yaml("graph")
    return list(
        pd.date_range(
            pd.Timestamp(cfg["calendar_start"]),
            pd.Timestamp(cfg["calendar_end"]),
            freq=f"{int(cfg['cadence_days'])}D",
        )
    )


def build_snapshots(
    events: pd.DataFrame,
    labels: pd.DataFrame,
    account_created: pd.Series,
    out_dir: Path,
    config: dict[str, Any] | None = None,
    dates: list[pd.Timestamp] | None = None,
) -> SnapshotRun:
    """Build and write every snapshot, returning the calendar."""
    cfg = config or load_yaml("graph")
    schedule = dates if dates is not None else calendar_dates(cfg)

    started = time.perf_counter()
    rows: list[dict[str, Any]] = []

    for snapshot_ts in schedule:
        tick = time.perf_counter()
        edges = project(events, snapshot_ts, cfg)
        result = snapshot_features(
            edges,
            snapshot_ts,
            device_members=device_membership(events, snapshot_ts, cfg),
            account_created=account_created,
            seeds=seed_accounts(labels, snapshot_ts, events),
            config=cfg,
        )
        elapsed = time.perf_counter() - tick

        target = out_dir / f"snapshot_ts={snapshot_ts.strftime('%Y-%m-%d')}"
        target.mkdir(parents=True, exist_ok=True)
        frame = result.features
        if frame.empty:
            frame = pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))
        frame.to_parquet(target / "part.parquet", compression="zstd", index=False)

        rows.append(
            {
                "snapshot_ts": snapshot_ts,
                "n_nodes": result.n_nodes,
                "n_edges": result.n_edges,
                "n_communities": result.n_communities,
                "n_seeds": result.n_seeds,
                "seconds": round(elapsed, 3),
            }
        )
        if len(rows) % 10 == 0:
            logger.info(
                "%s: %d snapshots built, %d nodes at %s",
                f"{len(rows)}/{len(schedule)}",
                len(rows),
                result.n_nodes,
                snapshot_ts.date(),
            )

    calendar = pd.DataFrame(rows, columns=list(CALENDAR_COLUMNS))
    out_dir.mkdir(parents=True, exist_ok=True)
    calendar.to_parquet(out_dir / "calendar.parquet", compression="zstd", index=False)

    return SnapshotRun(calendar=calendar, total_seconds=time.perf_counter() - started)


def load_snapshots(out_dir: Path) -> pd.DataFrame:
    """Every snapshot's rows, concatenated. Small: thousands of nodes per snapshot."""
    parts = sorted(out_dir.glob("snapshot_ts=*/part.parquet"))
    if not parts:
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))

    frames = [pd.read_parquet(part) for part in parts]
    combined = pd.concat([frame for frame in frames if len(frame)], ignore_index=True)
    return combined if len(combined) else pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build daily graph snapshots (PLAN §6.2).")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None, help="build only the first N")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    out_dir = args.out or settings.graph_offline_dir

    events = pd.read_parquet(settings.raw_dir / "events.parquet", columns=list(PROJECTION_COLUMNS))
    labels = pd.read_parquet(
        settings.raw_dir / "labels.parquet", columns=["txn_id", "is_fraud", "label_available_at"]
    )
    created = pd.read_parquet(
        settings.raw_dir / "accounts.parquet", columns=["account_id", "created_at"]
    ).set_index("account_id")["created_at"]

    dates = calendar_dates()
    if args.limit:
        dates = dates[: args.limit]
    logger.info("building %d snapshots over %s events", len(dates), f"{len(events):,}")

    run = build_snapshots(events, labels, created, out_dir, dates=dates)
    logger.info(
        "%d snapshots in %.1fs (slowest %.2fs); nodes %d-%d, seeds %d-%d",
        len(run.calendar),
        run.total_seconds,
        run.slowest,
        int(run.calendar["n_nodes"].min()),
        int(run.calendar["n_nodes"].max()),
        int(run.calendar["n_seeds"].min()),
        int(run.calendar["n_seeds"].max()),
    )
    # PLAN §6.1: past ten minutes, switch to weekly snapshots and document it.
    if run.total_seconds > 600:
        logger.warning(
            "full run took %.0fs, over the 10 minute budget in §6.1. Consider weekly "
            "snapshots (cut list item 5) and document the change.",
            run.total_seconds,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
