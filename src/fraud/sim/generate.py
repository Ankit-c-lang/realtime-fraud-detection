"""Assemble the simulation and write data/raw/ (PLAN §4.2, §4.8).

Run as ``python -m fraud.sim.generate`` (or ``make data``).

The order matters. Population first, then the legitimate stream, then the fraud patterns
on top of it, because account takeover has to anchor on a real card-present purchase and
card testing needs real victims. Everything is then stable-sorted by ``event_time`` and
``txn_id`` is assigned in that order, so the identifier itself carries the sequence.

``events.parquet`` holds the §3.5 fields and nothing else. The labels live in a separate
file keyed by ``txn_id`` (invariant 4): only evaluation code, the graph job's seeds and
the dashboard scorecard may read them, and never the scorer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from fraud.config import CONFIG_DIR, Settings, load_yaml
from fraud.sim.legit import EVENT_COLUMNS, generate_legit
from fraud.sim.patterns import inject_patterns
from fraud.sim.population import build_population, spawn_streams

logger = logging.getLogger(__name__)

ACCOUNT_COLUMNS = ("account_id", "created_at", "home_city", "home_lat", "home_lon", "spend_level")
MERCHANT_COLUMNS = (
    "merchant_id",
    "name",
    "category",
    "city",
    "country",
    "lat",
    "lon",
    "is_online",
    "created_at",
)
LABEL_FILE_COLUMNS = (
    "txn_id",
    "is_fraud",
    "fraud_type",
    "attack_id",
    "ring_id",
    "label_available_at",
)

TXN_ID_COLUMNS = ("txn_id", *EVENT_COLUMNS)
NO_FRAUD = "NONE"


@dataclass(frozen=True, slots=True)
class Dataset:
    """The four tables of PLAN §4.2 plus the run's manifest."""

    events: pd.DataFrame
    accounts: pd.DataFrame
    merchants: pd.DataFrame
    labels: pd.DataFrame
    manifest: dict[str, Any]


def config_digest(name: str) -> str:
    """sha256 of the config file, recorded in the manifest and frozen at sim-v1 (§4.8)."""
    return hashlib.sha256((CONFIG_DIR / f"{name}.yaml").read_bytes()).hexdigest()


def build_dataset(config_name: str = "sim") -> Dataset:
    """Run the whole simulation in memory."""
    config = load_yaml(config_name)
    categories = load_yaml("categories")["categories"]
    rngs = spawn_streams(int(config["seed"]))

    started = time.perf_counter()
    population = build_population(config, categories)
    legit = generate_legit(config, categories, population, rngs["legit"])
    attacks = inject_patterns(config, categories, population, legit, rngs)
    logger.info("generated %d legit and %d fraud events", len(legit), len(attacks.events))

    events, labels = _merge_streams(config, legit, attacks.events)
    accounts = pd.concat([population.accounts, attacks.accounts], ignore_index=True)
    merchants = population.merchants

    manifest = {
        "config": config_name,
        "seed": int(config["seed"]),
        "generator_version": int(config["generator_version"]),
        "config_sha256": config_digest(config_name),
        "rows": {
            "events": len(events),
            "accounts": len(accounts),
            "merchants": len(merchants),
            "labels": len(labels),
        },
        "fraud": {
            pattern: int((labels["fraud_type"] == pattern).sum())
            for pattern in sorted(set(labels["fraud_type"]) - {NO_FRAUD})
        },
        "fraud_rate": float((labels["is_fraud"] == 1).mean()),
        "attacks": int(labels.loc[labels["is_fraud"] == 1, "attack_id"].nunique()),
        "rings": int(labels["ring_id"].nunique()),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }

    return Dataset(
        events=_normalise_times(events),
        accounts=_normalise_times(accounts[list(ACCOUNT_COLUMNS)]),
        merchants=_normalise_times(merchants[list(MERCHANT_COLUMNS)]),
        labels=_normalise_times(labels),
        manifest=manifest,
    )


def _normalise_times(frame: pd.DataFrame) -> pd.DataFrame:
    """Pin the dtypes so memory and Parquet agree.

    Timestamps go to microseconds: the generators produce a mix of second and nanosecond
    resolution and Parquet stores neither unchanged, so a written table would not compare
    equal to the one in memory. Text columns go to the nullable string dtype for the same
    reason, since a column holding both strings and None reads back as object either way.
    """
    frame = frame.copy()
    for column in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            frame[column] = frame[column].astype("datetime64[us]")
        elif frame[column].dtype == object:
            frame[column] = frame[column].astype("string")
    return frame


def _merge_streams(
    config: dict[str, Any], legit: pd.DataFrame, fraud: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Interleave both streams, assign txn_id in time order, then split off the labels."""
    tagged = legit.copy()
    tagged["fraud_type"] = NO_FRAUD
    for column in ("attack_id", "ring_id"):
        tagged[column] = None

    combined = pd.concat([tagged, fraud], ignore_index=True)
    combined = combined.sort_values("event_time", kind="stable", ignore_index=True)

    fmt = str(config["output"]["txn_id_format"])
    combined["txn_id"] = [fmt.format(index) for index in range(len(combined))]

    delay = pd.Timedelta(days=int(config["labels"]["delay_days"]))
    labels = pd.DataFrame(
        {
            "txn_id": combined["txn_id"],
            "is_fraud": (combined["fraud_type"] != NO_FRAUD).astype("int8"),
            "fraud_type": combined["fraud_type"],
            "attack_id": combined["attack_id"],
            "ring_id": combined["ring_id"],
            # Chargebacks arrive late, so a label is not usable the moment it exists (§4.6).
            "label_available_at": combined["event_time"] + delay,
        }
    )

    return combined[list(TXN_ID_COLUMNS)], labels[list(LABEL_FILE_COLUMNS)]


def write_dataset(dataset: Dataset, out_dir: Path, compression: str = "zstd") -> None:
    """Write the four tables plus manifest.json. This directory has one writer (§3.7)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in ("events", "accounts", "merchants", "labels"):
        frame: pd.DataFrame = getattr(dataset, name)
        frame.to_parquet(out_dir / f"{name}.parquet", compression=compression, index=False)
        logger.info("wrote %s.parquet (%d rows)", name, len(frame))

    (out_dir / "manifest.json").write_text(
        json.dumps(dataset.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the simulated transaction dataset.")
    parser.add_argument("--config", default="sim", help="config name in configs/ (default: sim)")
    parser.add_argument("--out", type=Path, default=None, help="output directory")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    dataset = build_dataset(args.config)
    destination = args.out or Settings.from_env().raw_dir
    write_dataset(dataset, destination, str(load_yaml(args.config)["output"]["compression"]))

    manifest = dataset.manifest
    logger.info(
        "%s events, %.3f%% fraud, %d attacks, %d rings in %.1fs -> %s",
        f"{manifest['rows']['events']:,}",
        100.0 * manifest["fraud_rate"],
        manifest["attacks"],
        manifest["rings"],
        manifest["elapsed_seconds"],
        destination,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
