"""Assert the stack actually scored what was replayed (PLAN §12.2, `make smoke`).

`make smoke` is the one command that proves the whole Compose stack works together:
Redis up, backfill complete, the scorer consuming, the API serving. It fails loudly
rather than reporting a green container that scored nothing.

The check that matters is **unique** rows, not row count. §9.3 deliberately allows a
duplicate row when a crash lands between the flush and the ack, so counting raw rows
would be flaky by design; counting distinct `txn_id` through the deduplicating view is
the property that must actually hold.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from fraud.storage import duck

logger = logging.getLogger(__name__)

DEFAULT_API = "http://localhost:8000"


def scored_rows(root: Path | None = None) -> tuple[int, int]:
    """(unique txn_ids, raw rows). They differ only when a duplicate was written."""
    if not duck.has_scored_data(root):
        return 0, 0

    connection = duck.connect(root)
    try:
        unique = connection.execute("SELECT count(*) FROM scored").fetchone()[0]
        raw = connection.execute(
            f"SELECT count(*) FROM read_parquet('{duck.scored_glob(root)}', "
            "hive_partitioning = true, union_by_name = true)"
        ).fetchone()[0]
    finally:
        connection.close()
    return int(unique), int(raw)


def wait_for_rows(expect: int, timeout: float, root: Path | None = None) -> tuple[int, int]:
    """Poll until the scorer catches up, or give up.

    The replayer exits as soon as the last XADD returns; the scorer is still working.
    Without a wait this check would race the thing it is meant to verify.
    """
    deadline = time.monotonic() + timeout
    unique, raw = scored_rows(root)
    while unique < expect and time.monotonic() < deadline:
        time.sleep(2.0)
        unique, raw = scored_rows(root)
        logger.info("waiting: %s/%s unique rows scored", f"{unique:,}", f"{expect:,}")
    return unique, raw


def api_health(url: str) -> dict[str, Any]:
    import json

    with urllib.request.urlopen(f"{url}/health", timeout=10) as response:
        return dict(json.load(response))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-check the Compose stack (§12.2).")
    parser.add_argument("--expect", type=int, default=2000, help="events replayed")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds to wait")
    parser.add_argument("--api", default=DEFAULT_API)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    failures: list[str] = []

    unique, raw = wait_for_rows(args.expect, args.timeout)
    if unique != args.expect:
        failures.append(f"expected {args.expect:,} unique scored rows, found {unique:,}")
    else:
        logger.info("scored rows      OK  %s unique (%s raw)", f"{unique:,}", f"{raw:,}")

    try:
        health = api_health(args.api)
        if health.get("status") != "ok":
            failures.append(f"/health reports {health.get('status')!r}: {health}")
        else:
            logger.info(
                "api /health     OK  model %s, spec %s, redis %s",
                health.get("model_version"),
                health.get("feature_spec_version"),
                health.get("redis"),
            )
    except Exception as error:  # noqa: BLE001 - any failure to reach the API is a failure
        failures.append(f"/health unreachable at {args.api}: {error}")

    if failures:
        for failure in failures:
            logger.error("SMOKE FAILED: %s", failure)
        return 1

    logger.info("smoke passed: %s events replayed and scored end to end", f"{args.expect:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
