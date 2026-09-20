"""Offline replay: build the training features and the state checkpoint (PLAN §5.5).

Run as ``python -m fraud.features.replay`` (or ``make features``).

Every event in the frozen dataset goes through the same ``FeatureEngine`` the live scorer
uses, in event-time order, against an ``InMemoryStore``. Replaying rather than computing
the features in bulk is the point: a vectorised second implementation would drift from
the online one, and the parity test in §5.4 exists because that drift is silent.

Just before the first event at or after ``test_start`` the whole store is dumped to
``data/state/checkpoint_<test_start>.json.gz``. That is what the backfill job loads into
Redis so the live replay of the test window starts with warm state instead of cold
accounts (§9.5). The checkpoint is taken BEFORE that event is processed, so it contains
only what the training period could legitimately know.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from fraud.config import PROJECT_ROOT, Settings, load_yaml
from fraud.features.accounts import AccountDirectory
from fraud.features.engine import FeatureEngine
from fraud.features.spec import FEATURE_SPEC_VERSION, HOT_FEATURE_NAMES
from fraud.features.state import AccountState
from fraud.features.store_memory import InMemoryStore
from fraud.schemas import EVENT_FIELDS, TransactionEvent

logger = logging.getLogger(__name__)

IDENTITY_COLUMNS = ("txn_id", "event_time", "account_id")
TAG_COLUMNS = ("split", "in_burn_in", "feature_spec_version")
OUTPUT_COLUMNS = IDENTITY_COLUMNS + TAG_COLUMNS + HOT_FEATURE_NAMES

CHECKPOINT_VERSION = 1
PROGRESS_EVERY = 100_000


@dataclass(frozen=True, slots=True)
class ReplayResult:
    features: pd.DataFrame
    checkpoint: dict[str, Any] | None
    checkpoint_at: pd.Timestamp | None
    elapsed_seconds: float


def split_boundaries() -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """Split intervals from configs/splits.yaml, the single source of truth (§4.7)."""
    splits = load_yaml("splits")["splits"]
    return [
        (name, pd.Timestamp(spec["start"]), pd.Timestamp(spec["end"]))
        for name, spec in splits.items()
    ]


def tag_splits(event_time: pd.Series) -> pd.DataFrame:
    """Label each event with its split. Intervals are half-open, so none overlaps."""
    split = pd.Series("none", index=event_time.index, dtype="string")
    for name, start, end in split_boundaries():
        split = split.mask((event_time >= start) & (event_time < end), name)

    return pd.DataFrame(
        {
            "split": split,
            # Burn-in rows exist only to warm the state up; they are excluded from
            # training and from every metric (PLAN §4.7).
            "in_burn_in": (split == "burn_in").astype("int8"),
            "feature_spec_version": FEATURE_SPEC_VERSION,
        }
    )


def dump_state(store: InMemoryStore) -> dict[str, Any]:
    """Serialise the whole store. This is what the backfill loads into Redis (§9.5)."""
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "feature_spec_version": FEATURE_SPEC_VERSION,
        "accounts": {
            account_id: state.to_dict() for account_id, state in store.account_states().items()
        },
        "entities": store.entity_last_seen(),
    }


def load_state(blob: dict[str, Any], store: InMemoryStore) -> None:
    """Restore a checkpoint, refusing one built for a different feature spec."""
    written = blob.get("feature_spec_version")
    if written != FEATURE_SPEC_VERSION:
        raise ValueError(
            f"checkpoint was built for feature spec {written!r}, this build is "
            f"{FEATURE_SPEC_VERSION!r}. Re-run `make features`."
        )

    store.restore(
        {account_id: AccountState.from_dict(raw) for account_id, raw in blob["accounts"].items()},
        blob["entities"],
    )


def replay(
    events: pd.DataFrame,
    accounts: AccountDirectory,
    checkpoint_at: pd.Timestamp | None = None,
) -> ReplayResult:
    """Run every event through the engine in order, dumping state at the boundary."""
    started = time.perf_counter()
    store = InMemoryStore()
    engine = FeatureEngine(store, accounts)

    checkpoint: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []

    for position, row in enumerate(events.itertuples(index=False, name=None)):
        record = dict(zip(events.columns, row, strict=True))
        moment = record["event_time"]

        # Taken before this event is processed, so the checkpoint holds only what the
        # training period knew. Processing first would leak one test event into it.
        if checkpoint is None and checkpoint_at is not None and moment >= checkpoint_at:
            checkpoint = dump_state(store)
            logger.info("checkpoint taken at %s after %d events", checkpoint_at, position)

        event = TransactionEvent.from_mapping(record)
        features = engine.process(event)
        rows.append(
            {
                "txn_id": event.txn_id,
                "event_time": event.event_time,
                "account_id": event.account_id,
                **features,
            }
        )

        if position and position % PROGRESS_EVERY == 0:
            logger.info("replayed %s events", f"{position:,}")

    if checkpoint is None and checkpoint_at is not None:
        # Every event predates the boundary; the checkpoint is still the final state.
        checkpoint = dump_state(store)

    frame = pd.DataFrame(rows)
    frame = pd.concat([frame, tag_splits(frame["event_time"])], axis=1)

    return ReplayResult(
        features=frame[list(OUTPUT_COLUMNS)],
        checkpoint=checkpoint,
        checkpoint_at=checkpoint_at,
        elapsed_seconds=round(time.perf_counter() - started, 2),
    )


def write_outputs(result: ReplayResult, settings: Settings, compression: str = "zstd") -> Path:
    settings.features_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.features_dir / "hot_features.parquet"
    result.features.to_parquet(destination, compression=compression, index=False)
    logger.info("wrote %s (%d rows)", destination, len(result.features))

    if result.checkpoint is not None and result.checkpoint_at is not None:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        stamp = result.checkpoint_at.strftime("%Y-%m-%d")
        path = settings.state_dir / f"checkpoint_{stamp}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(result.checkpoint, handle, sort_keys=True, separators=(",", ":"))
        logger.info("wrote %s (%d accounts)", path, len(result.checkpoint["accounts"]))

    return destination


def write_report(result: ReplayResult, path: Path) -> None:
    """Feature ranges, so a change in the data is visible rather than silent (§5.5)."""
    frame = result.features
    scored = frame[frame["in_burn_in"] == 0]
    numeric = [name for name in HOT_FEATURE_NAMES if pd.api.types.is_numeric_dtype(frame[name])]

    stats = frame[numeric].describe().T
    lines = [
        "| feature | min | p50 | mean | p99 | max | zeros |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in numeric:
        column = frame[name]
        lines.append(
            f"| `{name}` | {column.min():,.3f} | {stats.loc[name, '50%']:,.3f} | "
            f"{column.mean():,.3f} | {column.quantile(0.99):,.3f} | {column.max():,.3f} | "
            f"{(column == 0).mean():.1%} |"
        )

    counts = frame["split"].value_counts()
    split_rows = "\n".join(
        f"| {name} | {int(counts.get(name, 0)):,} |" for name, _, _ in split_boundaries()
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# Feature replay report

Generated by `python -m fraud.features.replay` (PLAN §5.5). Every number comes from that
command.

| | |
|---|---|
| Feature spec | `{FEATURE_SPEC_VERSION}` |
| Rows | {len(frame):,} |
| Hot features | {len(HOT_FEATURE_NAMES)} |
| Burn-in rows (excluded from training and metrics) | {int(frame["in_burn_in"].sum()):,} |
| Rows available to modelling | {len(scored):,} |
| Replay time | {result.elapsed_seconds} s |
| Checkpoint | {result.checkpoint_at.date() if result.checkpoint_at is not None else "none"} |

## Rows per split

| split | rows |
|---|---|
{split_rows}

## Feature ranges

{chr(10).join(lines)}

## Categorical

`merchant_category` levels present: {", ".join(f"`{v}`" for v in sorted(frame["merchant_category"].unique()))}
""",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline feature replay (PLAN §5.5).")
    parser.add_argument("--data", type=Path, default=None, help="directory holding data/raw")
    parser.add_argument("--limit", type=int, default=None, help="replay only the first N events")
    parser.add_argument(
        "--report", type=Path, default=PROJECT_ROOT / "reports" / "feature_report.md"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    raw = args.data or settings.raw_dir

    events = pd.read_parquet(raw / "events.parquet", columns=list(EVENT_FIELDS))
    if args.limit:
        events = events.head(args.limit)
    accounts = AccountDirectory.from_parquet(raw / "accounts.parquet")
    logger.info("replaying %s events for %s accounts", f"{len(events):,}", f"{len(accounts):,}")

    boundary = pd.Timestamp(load_yaml("splits")["checkpoint_at"])
    result = replay(events, accounts, checkpoint_at=boundary)

    write_outputs(result, settings)
    write_report(result, args.report)
    logger.info(
        "replayed %s events in %.1fs (%.0f events/s)",
        f"{len(result.features):,}",
        result.elapsed_seconds,
        len(result.features) / max(result.elapsed_seconds, 1e-9),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
