"""One-shot backfill of Redis state, graph keys and the consumer group (PLAN §9.5).

The live replay starts at ``test_start``, which is day 73 of the simulated data. Without
this job every account in Redis would be brand new: ``account_age_days`` near zero,
``acct_history_cnt`` zero, no known devices, no graph position. Every transaction would
look like a first transaction, the model would see a distribution it was never trained
on, and the live numbers would disagree with the offline ones for a reason that has
nothing to do with streaming. So the offline replay's checkpoint — the exact state the
engine held just before the first test-window event — is loaded in first.

**Every step is idempotent.** A partial run is simply overwritten: account blobs are set
by key, entity scores go in with ``ZADD GT`` so they can only move forward, graph hashes
are rewritten with the same values, and the consumer group creation tolerates
``BUSYGROUP``. That matters because the failure mode of a half-finished backfill is not
an error — it is a system that starts and scores against partial history.

**The group is created before the replayer runs, at id 0.** Created at ``$`` instead, or
created after the first events arrive, it would silently skip everything already in the
stream and those events would never be scored.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pandas as pd
from redis import Redis

from fraud.config import Settings, load_yaml
from fraud.features.spec import FEATURE_SPEC_VERSION
from fraud.features.store_redis import (
    DEVICE_KEY,
    IP_KEY,
    MERCHANT_KEY,
    STATE_KEY,
    STATE_VERSION_KEY,
)
from fraud.graph.algorithms import defaults as graph_defaults
from fraud.graph.refresh_live import PUBLISHED_KEY, snapshot_key, to_epoch_seconds
from fraud.stream.recovery import ensure_group

logger = logging.getLogger(__name__)

DONE_KEY: Final[str] = "backfill:done:{spec}:{checkpoint}"
MILLIS_PER_SECOND: Final[int] = 1000

ENTITY_KEYS: Final[dict[str, str]] = {"dev": DEVICE_KEY, "ip": IP_KEY, "mer": MERCHANT_KEY}


@dataclass(frozen=True, slots=True)
class BackfillResult:
    skipped: bool = False
    accounts: int = 0
    entity_members: int = 0
    graph_accounts: int = 0
    snapshot_ts: int | None = None
    group_created: bool = False

    def summary(self) -> str:
        if self.skipped:
            return "skipped: already done for this feature spec and checkpoint"
        return (
            f"{self.accounts:,} account blobs, {self.entity_members:,} entity members, "
            f"{self.graph_accounts:,} graph rows at snapshot {self.snapshot_ts}"
        )


def checkpoint_date() -> str:
    """The boundary the checkpoint was taken at (PLAN §5.5, §9.5).

    ``splits.yaml`` requires ``checkpoint_at`` to equal ``splits.test.start``; reading it
    from there rather than from ``splits.test`` keeps the two honest about being the same
    date instead of assuming it.
    """
    return str(load_yaml("splits")["checkpoint_at"])


def done_key(checkpoint: str | None = None) -> str:
    return DONE_KEY.format(spec=FEATURE_SPEC_VERSION, checkpoint=checkpoint or checkpoint_date())


def run(
    client: Redis,
    *,
    checkpoint_path: Path | None = None,
    snapshot_dir: Path | None = None,
    force: bool = False,
) -> BackfillResult:
    """The whole §9.5 sequence. Safe to run repeatedly."""
    settings = Settings.from_env()
    checkpoint = checkpoint_date()
    marker = done_key(checkpoint)

    if not force and client.exists(marker):
        logger.info("backfill already done (%s); nothing to do", marker)
        return BackfillResult(skipped=True)

    raw = _load_checkpoint(
        checkpoint_path or settings.state_dir / f"checkpoint_{checkpoint}.json.gz"
    )
    accounts = _load_accounts(client, raw["accounts"])
    members = _load_entities(client, raw["entities"])

    snapshot_ts, graph_rows = _publish_snapshot(
        client, checkpoint, snapshot_dir or settings.graph_offline_dir
    )

    stream_cfg = load_yaml("stream")["stream"]
    created = ensure_group(client, str(stream_cfg["name"]), str(stream_cfg["group"]))

    # Markers last. Written earlier, a crash midway would leave the marker claiming a
    # backfill that never finished, and the next start would skip it.
    client.set(STATE_VERSION_KEY, FEATURE_SPEC_VERSION)
    client.set(marker, datetime.now().isoformat(timespec="seconds"))  # noqa: DTZ005 - naive IST

    result = BackfillResult(
        accounts=accounts,
        entity_members=members,
        graph_accounts=graph_rows,
        snapshot_ts=snapshot_ts,
        group_created=created,
    )
    logger.info("backfill complete: %s", result.summary())
    return result


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f"no checkpoint at {path}. Run `make features`, which writes it at the "
            "test boundary (PLAN §5.5)."
        )
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        blob: dict[str, Any] = json.load(handle)

    written = blob.get("feature_spec_version")
    if written != FEATURE_SPEC_VERSION:
        raise ValueError(
            f"checkpoint was built for feature spec {written!r}, this build is "
            f"{FEATURE_SPEC_VERSION!r}. Re-run `make features` (PLAN §5.5)."
        )
    return blob


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _chunk_size() -> int:
    return int(load_yaml("stream")["backfill"]["chunk_size"])


def _load_accounts(client: Redis, accounts: dict[str, Any]) -> int:
    """Account blobs, chunked. Written exactly as ``AccountState.to_json`` would."""
    size = _chunk_size()
    entries = sorted(accounts.items())
    for chunk in _chunks(entries, size):
        client.mset(
            {
                STATE_KEY.format(account_id): json.dumps(
                    state, sort_keys=True, separators=(",", ":")
                )
                for account_id, state in chunk
            }
        )
    logger.info("loaded %s account blobs", f"{len(entries):,}")
    return len(entries)


def _load_entities(client: Redis, entities: dict[str, dict[str, dict[str, int]]]) -> int:
    """Entity last-seen sets, trimmed to the retention window on the way in.

    The checkpoint carries every entity the training period ever saw. Anything older
    than the longest window can never be counted again, so loading it would cost memory
    for rows no query can reach.
    """
    from fraud.features.spec import FeatureConfig

    cfg = FeatureConfig.load()
    size = _chunk_size()
    newest = max(
        (ts for kind in entities.values() for last in kind.values() for ts in last.values()),
        default=0,
    )
    cutoff = newest - cfg.redis_entity_retention_seconds * MILLIS_PER_SECOND

    total = 0
    for kind, template in ENTITY_KEYS.items():
        for chunk in _chunks(sorted(entities.get(kind, {}).items()), size):
            pipe = client.pipeline(transaction=False)
            for entity_id, last_seen in chunk:
                fresh = {account_id: ts for account_id, ts in last_seen.items() if ts >= cutoff}
                if not fresh:
                    continue
                # GT for the same reason the scorer uses it: re-running the backfill
                # must never drag a timestamp the live run has already advanced.
                pipe.zadd(template.format(entity_id), fresh, gt=True)
                total += len(fresh)
            pipe.execute()

    logger.info("loaded %s entity members across %s", f"{total:,}", ", ".join(ENTITY_KEYS))
    return total


def _publish_snapshot(client: Redis, checkpoint: str, snapshot_dir: Path) -> tuple[int | None, int]:
    """Publish the offline snapshot at ``test_start`` so the first events have a graph.

    This snapshot is built from events *before* the boundary, so publishing it hands the
    live system only what the offline join would have used at the same instant (§6.4).
    """
    path = snapshot_dir / f"snapshot_ts={checkpoint}" / "part.parquet"
    if not path.is_file():
        logger.warning("no offline snapshot at %s; the first events will use defaults", path)
        return None, 0

    frame = pd.read_parquet(path)
    snapshot_ts = to_epoch_seconds(datetime.fromisoformat(checkpoint))
    ttl = int(load_yaml("graph")["live"]["key_ttl_seconds"])
    names = list(graph_defaults())

    size = _chunk_size()
    rows = frame.to_dict("records")
    for chunk in _chunks(rows, size):
        pipe = client.pipeline(transaction=False)
        for row in chunk:
            key = snapshot_key(snapshot_ts, str(row["account_id"]))
            pipe.hset(key, mapping={name: str(row[name]) for name in names})
            pipe.expire(key, ttl)
        pipe.execute()

    client.zadd(PUBLISHED_KEY, {str(snapshot_ts): snapshot_ts})
    logger.info(
        "published snapshot %s (%s) with %s accounts", snapshot_ts, checkpoint, f"{len(rows):,}"
    )
    return snapshot_ts, len(rows)


def main(argv: list[str] | None = None) -> int:
    """``python -m fraud.stream.backfill`` (PLAN §9.5)."""
    import argparse

    parser = argparse.ArgumentParser(description="Backfill Redis before the replay (§9.5).")
    parser.add_argument("--force", action="store_true", help="ignore the done marker")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    client = Redis.from_url(settings.redis_url, decode_responses=True)

    result = run(client, force=args.force)
    logger.info("%s", result.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
