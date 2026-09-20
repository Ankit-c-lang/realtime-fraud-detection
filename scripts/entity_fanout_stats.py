"""Accounts-per-device and accounts-per-IP over 30-day windows (PLAN §6.1, prompt P4.1).

Measures the distribution the graph fan-out caps have to be set from, and **does not set
them**. The caps have to drop carrier NAT, which links hundreds of unrelated accounts,
while keeping the legitimate mid-size clusters: offices, households, and the widely
shared devices sim-v2 added.

Windows match how a snapshot is built: for each day boundary T in the train period, count
the distinct accounts that touched an entity in ``[T - 30d, T)``. The pooled distribution
over all (entity, T) pairs is what a cap applies to. The lookback reaches back into
burn-in on purpose, because a graph at time T legitimately knows every earlier event;
burn-in exclusion is about which rows train a model, not which edges exist.

**Entity-type labels are simulator ground truth**, rebuilt from the frozen config. They
are context for a human setting the caps and must never reach the pipeline: in production
nobody knows which IP is carrier NAT. The caps themselves are read off the distribution.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from fraud.config import PROJECT_ROOT, Settings, load_yaml
from fraud.sim.population import build_population

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
PERCENTILES = (0.50, 0.90, 0.99, 0.995, 0.999)
CAP_CANDIDATES = (3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 50, 100)


def fanout_by_window(
    events: pd.DataFrame, entity: str, boundaries: list[pd.Timestamp]
) -> pd.DataFrame:
    """Distinct accounts per entity in [T-30d, T), for every boundary T."""
    ordered = events.sort_values("event_time", kind="stable")
    times = ordered["event_time"].to_numpy()
    window = pd.Timedelta(days=WINDOW_DAYS)

    frames = []
    for boundary in boundaries:
        low = np.searchsorted(times, np.datetime64(boundary - window))
        high = np.searchsorted(times, np.datetime64(boundary))
        if high <= low:
            continue
        chunk = ordered.iloc[low:high]
        counts = chunk.groupby(entity)["account_id"].nunique()
        frames.append(pd.DataFrame({"entity": counts.index, "accounts": counts.to_numpy()}))

    return pd.concat(frames, ignore_index=True)


def describe(counts: pd.Series) -> dict[str, float]:
    return {
        "n": len(counts),
        "mean": float(counts.mean()),
        **{f"p{int(q * 1000) / 10:g}": float(counts.quantile(q)) for q in PERCENTILES},
        "max": int(counts.max()),
    }


def _table(rows: list[dict[str, object]], columns: list[str]) -> str:
    header = "| " + " | ".join(columns) + " |"
    divider = "|" + "|".join(["---"] * len(columns)) + "|"
    body = [
        "| " + " | ".join(f"{row.get(column, '')}" for column in columns) + " |" for row in rows
    ]
    return "\n".join([header, divider, *body])


def summarise(pooled: pd.DataFrame, kinds: pd.Series, fraud_entities: set[str], label: str) -> str:
    """Percentiles overall, then per entity type, then what each cap would drop."""
    pooled = pooled.copy()
    pooled["kind"] = pooled["entity"].map(kinds).fillna("unknown")
    pooled["fraud_linked"] = pooled["entity"].isin(fraud_entities)

    overall = describe(pooled["accounts"])
    lines = [
        f"### {label}: pooled over {overall['n']:,} (entity, window) pairs",
        "",
        _table(
            [{k: (f"{v:,.2f}" if isinstance(v, float) else f"{v:,}") for k, v in overall.items()}],
            list(overall),
        ),
        "",
        f"#### {label} by entity type (simulator ground truth — context only)",
        "",
    ]

    rows = []
    for kind, group in pooled.groupby("kind"):
        stats = describe(group["accounts"])
        rows.append(
            {
                "type": kind,
                "entities": f"{group['entity'].nunique():,}",
                "pairs": f"{stats['n']:,}",
                "p50": f"{stats['p50']:.0f}",
                "p90": f"{stats['p90']:.0f}",
                "p99": f"{stats['p99']:.0f}",
                "p99.5": f"{stats['p99.5']:.0f}",
                "p99.9": f"{stats['p99.9']:.0f}",
                "max": f"{stats['max']:,}",
            }
        )
    rows.sort(key=lambda row: float(row["p99"].replace(",", "")), reverse=True)
    lines += [
        _table(rows, ["type", "entities", "pairs", "p50", "p90", "p99", "p99.5", "p99.9", "max"]),
        "",
        f"#### What each candidate cap would drop ({label})",
        "",
        "An entity above the cap contributes no edges at that snapshot (§6.1).",
        "",
    ]

    legit = pooled[~pooled["fraud_linked"]]
    fraud = pooled[pooled["fraud_linked"]]
    cap_rows = []
    for cap in CAP_CANDIDATES:
        dropped_legit = legit[legit["accounts"] > cap]
        dropped_fraud = fraud[fraud["accounts"] > cap]
        cap_rows.append(
            {
                "cap": cap,
                "legit pairs dropped": f"{len(dropped_legit):,} ({len(dropped_legit) / max(len(legit), 1):.2%})",
                "legit entities dropped": f"{dropped_legit['entity'].nunique():,}",
                "types dropped": ", ".join(sorted(set(dropped_legit["kind"]))[:4]) or "-",
                "fraud pairs dropped": f"{len(dropped_fraud):,} ({len(dropped_fraud) / max(len(fraud), 1):.2%})",
            }
        )
    lines.append(
        _table(
            cap_rows,
            [
                "cap",
                "legit pairs dropped",
                "legit entities dropped",
                "types dropped",
                "fraud pairs dropped",
            ],
        )
    )
    return "\n".join(lines)


def _attack_actors(population) -> dict[str, pd.Series]:
    """Rebuild the attack actors so their entity types can be named.

    Deterministic from the frozen config, so this reproduces exactly the devices and
    IPs the committed dataset was generated with.
    """
    from fraud.sim.legit import generate_legit
    from fraud.sim.patterns import inject_patterns
    from fraud.sim.population import spawn_streams

    config = load_yaml("sim")
    categories = load_yaml("categories")["categories"]
    rngs = spawn_streams(int(config["seed"]))
    legit = generate_legit(config, categories, population, rngs["legit"])
    attacks = inject_patterns(config, categories, population, legit, rngs)

    return {
        "devices": attacks.devices.set_index("device_id")["device_type"],
        "ips": attacks.ips.set_index("ip")["ip_type"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Entity fan-out distributions (PLAN §6.1).")
    parser.add_argument(
        "--report", type=Path, default=PROJECT_ROOT / "reports" / "entity_fanout.md"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    splits = load_yaml("splits")["splits"]
    train_start = pd.Timestamp(splits["train"]["start"])
    train_end = pd.Timestamp(splits["train"]["end"])

    events = pd.read_parquet(
        settings.raw_dir / "events.parquet",
        columns=["txn_id", "event_time", "account_id", "device_id", "ip"],
    )
    labels = pd.read_parquet(settings.raw_dir / "labels.parquet", columns=["txn_id", "is_fraud"])
    events = events.merge(labels, on="txn_id", validate="one_to_one")

    # Only events that a snapshot inside the train period could see.
    events = events[events["event_time"] < train_end]
    boundaries = list(pd.date_range(train_start, train_end, freq="D", inclusive="left"))
    logger.info(
        "%s events, %d daily snapshots from %s to %s",
        f"{len(events):,}",
        len(boundaries),
        train_start.date(),
        train_end.date(),
    )

    population = build_population(load_yaml("sim"), load_yaml("categories")["categories"])
    # Every device, including the spare handsets, is in the registry now that
    # next_device registers what it allocates.
    device_kind = population.devices.set_index("device_id")["device_type"]
    ip_kind = population.ips.set_index("ip")["ip_type"]
    assert device_kind.index.is_unique, "duplicate device id in the registry"

    # Attack actors too, so a ring device can be told from a card-testing one. Ring
    # fan-out is the number the cap has to stay above, or the graph loses the pattern
    # it exists to find.
    attacks = _attack_actors(population)
    device_kind = pd.concat([device_kind, attacks["devices"]])
    ip_kind = pd.concat([ip_kind, attacks["ips"]])

    fraud_devices = set(events.loc[events["is_fraud"] == 1, "device_id"])
    fraud_ips = set(events.loc[events["is_fraud"] == 1, "ip"])

    sections = []
    for entity, kinds, fraud_entities, label in (
        ("device_id", device_kind, fraud_devices, "Accounts per DEVICE"),
        ("ip", ip_kind, fraud_ips, "Accounts per IP"),
    ):
        pooled = fanout_by_window(events, entity, boundaries)
        logger.info("%s: %s (entity, window) pairs", label, f"{len(pooled):,}")
        sections.append(summarise(pooled, kinds, fraud_entities, label))

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        "# Entity fan-out, train period (PLAN §6.1)\n\n"
        f"Generated by `python scripts/entity_fanout_stats.py`. Windows are `[T - {WINDOW_DAYS}d, T)` "
        f"at each of the {len(boundaries)} day boundaries in train "
        f"({train_start.date()} to {train_end.date()}), pooled over all (entity, window) pairs.\n\n"
        "Entity types are simulator ground truth, shown so the caps can be set deliberately. "
        "They are **not** available to the pipeline: production does not know which IP is "
        "carrier NAT. The caps are read off the distribution.\n\n"
        "**No cap is set here.** That is a deliberate, separate decision.\n\n"
        + "\n\n".join(sections)
        + "\n",
        encoding="utf-8",
    )
    logger.info("wrote %s", args.report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
