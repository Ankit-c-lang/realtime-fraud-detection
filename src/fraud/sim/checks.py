"""Validate a generated dataset and write reports/sim_report.md (PLAN §4.8).

Run as ``python -m fraud.sim.checks`` (``make data`` runs it straight after the
generator). Any failing check exits non-zero, so a bad dataset cannot pass unnoticed.

These read back what was actually written to ``data/raw/`` rather than re-deriving it in
memory, so a fault in the writing path is caught too. This is evaluation code, which is
one of the few places allowed to read labels (§4.6).

The report this writes is the evidence for the freeze: once §4.8's freeze rule applies,
the sha256 recorded here pins the config that every model's metadata refers back to.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fraud.config import PROJECT_ROOT, Settings, load_yaml
from fraud.sim.generate import (
    ACCOUNT_COLUMNS,
    LABEL_FILE_COLUMNS,
    MERCHANT_COLUMNS,
    NO_FRAUD,
    TXN_ID_COLUMNS,
    build_dataset,
    write_dataset,
)

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0
IMPOSSIBLE_SPEED_KMH = 900.0
ATO_SPEED_SHARE = 0.90
CARD_TESTING_WINDOW = pd.Timedelta(minutes=60)
PREVALENCE_BAND = (0.010, 0.020)
PATTERN_TOLERANCE = 0.30


@dataclass(frozen=True, slots=True)
class Check:
    """One validation, with the number behind it so the report is auditable."""

    name: str
    section: str
    passed: bool
    detail: str

    @property
    def mark(self) -> str:
        return "PASS" if self.passed else "**FAIL**"


@dataclass(frozen=True, slots=True)
class Tables:
    events: pd.DataFrame
    accounts: pd.DataFrame
    merchants: pd.DataFrame
    labels: pd.DataFrame
    manifest: dict[str, Any]

    @property
    def joined(self) -> pd.DataFrame:
        """Events with their labels attached. Evaluation only (PLAN §4.6)."""
        return self.events.merge(self.labels, on="txn_id", validate="one_to_one")


def load_tables(directory: Path) -> Tables:
    read = {
        name: pd.read_parquet(directory / f"{name}.parquet")
        for name in ("events", "accounts", "merchants", "labels")
    }
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return Tables(**read, manifest=manifest)


def haversine_km(
    lat1: np.ndarray | float,
    lon1: np.ndarray | float,
    lat2: np.ndarray | float,
    lon2: np.ndarray | float,
) -> np.ndarray:
    lat1, lon1, lat2, lon2 = (
        np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2)
    )
    inner = (
        np.sin((lat2 - lat1) / 2.0) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2.0) ** 2
    )
    return EARTH_RADIUS_KM * 2.0 * np.arcsin(np.sqrt(np.clip(inner, 0.0, 1.0)))


# --- schema and integrity ---------------------------------------------------


def _schema_checks(tables: Tables) -> list[Check]:
    events, labels = tables.events, tables.labels
    accounts = tables.accounts.set_index("account_id")["created_at"]
    early = (
        events["event_time"].to_numpy() < accounts.reindex(events["account_id"]).to_numpy()
    ).sum()

    return [
        Check(
            "events.parquet has exactly the §4.2 columns",
            "§4.2",
            tuple(events.columns) == TXN_ID_COLUMNS,
            f"`{', '.join(events.columns)}`",
        ),
        Check(
            "no label column leaks into events",
            "§4.2, invariant 4",
            not set(events.columns) & set(LABEL_FILE_COLUMNS) - {"txn_id"},
            "labels live only in labels.parquet, keyed by txn_id",
        ),
        Check(
            "accounts and merchants match §4.2",
            "§4.2",
            tuple(tables.accounts.columns) == ACCOUNT_COLUMNS
            and tuple(tables.merchants.columns) == MERCHANT_COLUMNS,
            f"{len(tables.accounts):,} accounts, {len(tables.merchants):,} merchants",
        ),
        Check(
            "txn_id is unique", "§4.8", bool(events["txn_id"].is_unique), f"{len(events):,} events"
        ),
        Check(
            "events are sorted by event_time",
            "§4.8",
            bool(events["event_time"].is_monotonic_increasing),
            f"{events['event_time'].min()} to {events['event_time'].max()}",
        ),
        Check(
            "no event precedes its account's creation",
            "§4.8",
            early == 0,
            f"{early} violations",
        ),
        Check(
            "one label row per event",
            "§4.6",
            len(labels) == len(events) and bool(labels["txn_id"].is_unique),
            f"{len(labels):,} label rows",
        ),
    ]


# --- volume -----------------------------------------------------------------


def _volume_checks(tables: Tables, config: dict[str, Any]) -> list[Check]:
    labels = tables.labels
    rate = float(labels["is_fraud"].mean())
    low, high = PREVALENCE_BAND

    checks = [
        Check(
            "fraud prevalence within [1.0%, 2.0%]",
            "§4.8",
            low <= rate <= high,
            f"{rate:.3%}",
        ),
        Check(
            "total volume within tolerance of target",
            "§4.3",
            abs(len(tables.events) / int(config["target_transactions"]) - 1.0)
            <= float(config["target_tolerance"]),
            f"{len(tables.events):,} vs target {int(config['target_transactions']):,} "
            f"({len(tables.events) / int(config['target_transactions']) - 1:+.2%})",
        ),
    ]

    for pattern, spec in config["patterns"].items():
        target = int(spec["target_transactions"])
        produced = int((labels["fraud_type"] == pattern.upper()).sum())
        drift = produced / target - 1.0
        checks.append(
            Check(
                f"{pattern.upper()} within ±30% of target",
                "§4.8",
                abs(drift) <= PATTERN_TOLERANCE,
                f"{produced:,} vs {target:,} ({drift:+.1%})",
            )
        )

    return checks


# --- pattern signatures -----------------------------------------------------


def _card_testing_check(tables: Tables, config: dict[str, Any]) -> Check:
    """Every attack device must touch >= 20 distinct accounts within 60 minutes."""
    spec = config["patterns"]["card_testing"]
    joined = tables.joined
    probes = joined[
        (joined["fraud_type"] == "CARD_TESTING") & (joined["amount"] <= float(spec["amount_max"]))
    ]

    worst = None
    for _, attack in probes.groupby("attack_id"):
        start = attack["event_time"].min()
        inside = attack[attack["event_time"] < start + CARD_TESTING_WINDOW]
        reach = inside["account_id"].nunique()
        worst = reach if worst is None else min(worst, reach)

    minimum = int(spec["victims_min"])
    return Check(
        f"every card-testing device sees >= {minimum} accounts in 60 min",
        "§4.8",
        worst is not None and worst >= minimum,
        f"worst attack reaches {worst} accounts across {probes['attack_id'].nunique()} attacks",
    )


def _ato_speed_check(tables: Tables) -> tuple[Check, list[float]]:
    """>= 90% of takeovers must exceed 900 km/h at the first fraud transaction."""
    joined = tables.joined
    fraud = joined[joined["fraud_type"] == "ATO"]
    honest = joined[(joined["is_fraud"] == 0) & (joined["channel"] == "POS")]
    by_account = {account: group for account, group in honest.groupby("account_id")}

    speeds: list[float] = []
    for _, attack in fraud.groupby("attack_id"):
        first = attack.loc[attack["event_time"].idxmin()]
        history = by_account.get(first["account_id"])
        if history is None:
            continue
        earlier = history[history["event_time"] < first["event_time"]]
        if earlier.empty:
            continue

        previous = earlier.loc[earlier["event_time"].idxmax()]
        km = float(haversine_km(previous["lat"], previous["lon"], first["lat"], first["lon"]))
        hours = max(
            (first["event_time"] - previous["event_time"]).total_seconds() / 3600.0, 1.0 / 60.0
        )
        speeds.append(km / hours)

    share = float(np.mean(np.array(speeds) > IMPOSSIBLE_SPEED_KMH)) if speeds else 0.0
    return (
        Check(
            "ATO: >= 90% of attacks exceed 900 km/h at the first fraud event",
            "§4.8",
            share >= ATO_SPEED_SHARE,
            f"{share:.1%} of {len(speeds)} attacks; median {np.median(speeds):,.0f} km/h"
            if speeds
            else "no measurable attacks",
        ),
        speeds,
    )


def _ring_checks(tables: Tables, config: dict[str, Any]) -> list[Check]:
    """The §4.5 constraints, re-verified on the written data rather than trusted."""
    spec = config["patterns"]["ring"]
    splits = load_yaml("splits")["splits"]
    joined = tables.joined
    rings = joined[joined["fraud_type"] == "RING"]

    if rings.empty:
        return [Check("rings exist", "§4.5", False, "no ring events")]

    spans = rings.groupby("ring_id")["event_time"].agg(["min", "max"])
    test_start = pd.Timestamp(splits["test"]["start"])
    test_end = pd.Timestamp(splits["test"]["end"])
    train_start = pd.Timestamp(splits["train"]["start"])
    train_end = pd.Timestamp(splits["train"]["end"])

    in_test = spans[(spans["min"] >= test_start) & (spans["min"] < test_end)]
    in_train = spans[(spans["min"] >= train_start) & (spans["max"] < train_end)]

    devices = rings.groupby("ring_id")["device_id"].unique()
    seen: dict[str, str] = {}
    reusers: list[str] = []
    for ring_id in spans.sort_values("min").index:
        if any(device in seen for device in devices[ring_id]):
            reusers.append(str(ring_id))
        for device in devices[ring_id]:
            seen.setdefault(device, str(ring_id))

    test_reusers = len(set(reusers) & set(in_test.index.astype(str)))
    sizes = rings.groupby("ring_id")["account_id"].nunique()

    return [
        Check(
            f">= {spec['min_rings_in_test']} rings start inside the test window",
            "§4.5",
            len(in_test) >= int(spec["min_rings_in_test"]),
            f"{len(in_test)} of {len(spans)} rings",
        ),
        Check(
            f">= {spec['min_rings_in_test_with_device_reuse']} test rings reuse a device",
            "§4.5",
            test_reusers >= int(spec["min_rings_in_test_with_device_reuse"]),
            f"{test_reusers} test rings; {len(reusers)} rings reuse overall "
            f"({len(reusers) / len(spans):.0%}, configured {float(spec['device_reuse_share']):.0%})",
        ),
        Check(
            f">= {spec['min_rings_fully_in_train']} rings start and finish inside training",
            "§4.5",
            len(in_train) >= int(spec["min_rings_fully_in_train"]),
            f"{len(in_train)} rings",
        ),
        Check(
            "ring sizes stay within the configured range",
            "§4.5",
            int(sizes.min()) >= int(spec["accounts_min"])
            and int(sizes.max()) <= int(spec["accounts_max"]),
            f"{int(sizes.min())}-{int(sizes.max())} accounts per ring "
            f"(configured {spec['accounts_min']}-{spec['accounts_max']})",
        ),
        Check(
            "ring approval rate is indistinguishable from ordinary traffic",
            "§4.5",
            abs(
                float((rings["status"] == "DECLINED").mean())
                - float(config["legit"]["decline_rate"])
            )
            <= 0.02,
            f"{(rings['status'] == 'DECLINED').mean():.2%} declined vs "
            f"{float(config['legit']['decline_rate']):.2%} for legitimate traffic",
        ),
    ]


# --- hard negatives ---------------------------------------------------------


def _hard_negative_checks(tables: Tables, config: dict[str, Any]) -> list[Check]:
    """Thresholds derive from the config, so shrinking the population stays valid (§4.8)."""
    joined = tables.joined
    honest = joined[joined["is_fraud"] == 0]
    accounts = tables.accounts
    n_accounts = len(accounts)

    vpn_spec = config["ip_pools"]["vpn"]
    abroad = honest[(honest["channel"] == "ONLINE") & (honest["country"] != "IN")]
    vpn_expected = n_accounts * float(vpn_spec["account_share"])

    family = config["devices"]
    shared = honest.groupby("device_id")["account_id"].nunique()
    shared_devices = int((shared > 1).sum())
    family_expected = (
        n_accounts
        * float(family["family_share"])
        / ((int(family["family_size_min"]) + int(family["family_size_max"])) / 2.0)
    )

    start = pd.Timestamp(config["start"])
    newcomers = accounts[accounts["created_at"] >= start]
    active_newcomers = honest[honest["account_id"].isin(newcomers["account_id"])][
        "account_id"
    ].nunique()
    newcomer_expected = n_accounts * (1.0 - float(config["accounts"]["existing_share"]))

    burst = config["hard_negatives"]["micro_burst"]
    bursty = _accounts_with_bursts(
        honest, float(burst["amount_max"]), int(burst["txns_min"]), int(burst["window_minutes"])
    )
    burst_expected = n_accounts * float(burst["account_share"])

    return [
        Check(
            "VPN use produces legitimate impossible travel",
            "§4.4",
            abroad["account_id"].nunique() >= 0.25 * vpn_expected,
            f"{len(abroad):,} events from {abroad['account_id'].nunique()} accounts "
            f"(~{vpn_expected:.0f} expected to use a VPN)",
        ),
        Check(
            "households share a device",
            "§4.4",
            shared_devices >= 0.5 * family_expected,
            f"{shared_devices:,} shared devices (~{family_expected:.0f} households expected)",
        ),
        Check(
            "new legitimate accounts transact",
            "§4.4",
            active_newcomers >= 0.5 * newcomer_expected,
            f"{active_newcomers:,} of {len(newcomers):,} mid-simulation accounts are active",
        ),
        Check(
            "legitimate micro-payment bursts exist",
            "§4.4",
            bursty >= 0.25 * burst_expected,
            f"{bursty:,} accounts show a burst (~{burst_expected:.0f} configured)",
        ),
        Check(
            "low-friction merchants have honest customers",
            "§4.4",
            bool(
                (
                    honest["merchant_id"].isin(
                        tables.merchants.loc[
                            tables.merchants["category"] == "digital_goods", "merchant_id"
                        ]
                    )
                ).sum()
                > 0
            ),
            "small digital purchases occur outside card-testing attacks",
        ),
    ]


def _accounts_with_bursts(
    honest: pd.DataFrame, amount_max: float, minimum: int, window_minutes: int
) -> int:
    small = honest[honest["amount"] <= amount_max]
    window = pd.Timedelta(minutes=window_minutes)
    found = 0

    for _, rows in small.groupby("account_id"):
        times = rows["event_time"].sort_values().to_numpy()
        if len(times) < minimum:
            continue
        if (times[minimum - 1 :] - times[: len(times) - minimum + 1] <= window).any():
            found += 1
    return found


# --- determinism ------------------------------------------------------------


def _determinism_check(tmp_root: Path) -> Check:
    """Two runs of the small config must produce identical files (PLAN §4.8)."""
    digests = []
    for index in (0, 1):
        target = tmp_root / f"determinism_{index}"
        write_dataset(build_dataset("sim_tiny"), target)
        digests.append(
            {
                name: hashlib.sha256((target / f"{name}.parquet").read_bytes()).hexdigest()
                for name in ("events", "accounts", "merchants", "labels")
            }
        )

    matched = digests[0] == digests[1]
    return Check(
        "same seed reproduces identical files (sim_tiny)",
        "§4.8",
        matched,
        f"events sha256 `{digests[0]['events'][:16]}…`" if matched else "hashes differ",
    )


# --- report -----------------------------------------------------------------


def run_checks(tables: Tables, config: dict[str, Any], tmp_root: Path | None) -> list[Check]:
    checks = _schema_checks(tables)
    checks += _volume_checks(tables, config)
    checks.append(_card_testing_check(tables, config))
    checks.append(_ato_speed_check(tables)[0])
    checks += _ring_checks(tables, config)
    checks += _hard_negative_checks(tables, config)
    if tmp_root is not None:
        checks.append(_determinism_check(tmp_root))
    return checks


def _example_attacks(tables: Tables) -> str:
    """One worked example per pattern, so the report can be read without the code."""
    joined = tables.joined
    blocks: list[str] = []

    for pattern in ("VELOCITY", "ATO", "CARD_TESTING", "RING"):
        rows = joined[joined["fraud_type"] == pattern]
        if rows.empty:
            continue

        attack_id = rows["attack_id"].iloc[len(rows) // 2]
        attack = rows[rows["attack_id"] == attack_id].sort_values("event_time")
        shown = attack.head(6)[
            ["event_time", "account_id", "merchant_category", "amount", "city", "status"]
        ]
        span = (attack["event_time"].max() - attack["event_time"].min()).total_seconds() / 60.0

        blocks.append(
            f"### {pattern} — `{attack_id}`\n\n"
            f"{len(attack)} transactions, {attack['account_id'].nunique()} account(s), "
            f"{attack['device_id'].nunique()} device(s), spanning {span:,.0f} minutes.\n\n"
            + _markdown_table(shown)
            + (f"\n_(first 6 of {len(attack)})_" if len(attack) > 6 else "")
        )

    return "\n\n".join(blocks)


def _markdown_table(frame: pd.DataFrame) -> str:
    """Render a small frame as a Markdown table.

    pandas' own to_markdown needs tabulate, which is not a project dependency and is not
    worth adding for a handful of example rows.
    """
    header = "| " + " | ".join(str(column) for column in frame.columns) + " |"
    divider = "|" + "|".join(["---"] * len(frame.columns)) + "|"
    body = [
        "| " + " | ".join(f"{value}" for value in row) + " |"
        for row in frame.itertuples(index=False, name=None)
    ]
    return "\n".join([header, divider, *body]) + "\n"


def write_report(tables: Tables, config: dict[str, Any], checks: list[Check], path: Path) -> None:
    failed = [check for check in checks if not check.passed]
    manifest = tables.manifest
    labels = tables.labels

    rows = "\n".join(
        f"| {check.mark} | {check.name} | {check.section} | {check.detail} |" for check in checks
    )
    breakdown = "\n".join(
        f"| {pattern} | {int((labels['fraud_type'] == pattern).sum()):,} | "
        f"{int(config['patterns'][pattern.lower()]['target_transactions']):,} | "
        f"{int((labels['fraud_type'] == pattern).sum()) / int(config['patterns'][pattern.lower()]['target_transactions']) - 1:+.1%} |"
        for pattern in sorted(set(labels["fraud_type"]) - {NO_FRAUD})
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# Simulator validation report

Generated {datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")} by `python -m fraud.sim.checks`.
Every number below comes from that command (PLAN §0.4).

## Run

| | |
|---|---|
| Config | `configs/{manifest["config"]}.yaml` |
| **sha256** | `{manifest["config_sha256"]}` |
| Seed | {manifest["seed"]} |
| Generator version | {manifest["generator_version"]} |
| Events | {manifest["rows"]["events"]:,} |
| Accounts | {manifest["rows"]["accounts"]:,} |
| Merchants | {manifest["rows"]["merchants"]:,} |
| Fraud rate | {manifest["fraud_rate"]:.3%} |
| Attacks | {manifest["attacks"]:,} across {manifest["rings"]} rings |
| Generation time | {manifest["elapsed_seconds"]} s |

The sha256 above is what PLAN §4.8's freeze rule pins. It is recorded in every model's
metadata, and metrics are never improved by editing the config behind it.

## Checks

{"**All checks passed.**" if not failed else f"**{len(failed)} of {len(checks)} checks FAILED.**"}

| Result | Check | Plan | Detail |
|---|---|---|---|
{rows}

## Fraud by pattern

| Pattern | Produced | Target | Drift |
|---|---|---|---|
{breakdown}

## Example attacks

{_example_attacks(tables)}
""",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the generated dataset (PLAN §4.8).")
    parser.add_argument("--config", default="sim")
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "reports" / "sim_report.md")
    parser.add_argument(
        "--skip-determinism", action="store_true", help="skip the two-run hash comparison"
    )
    parser.add_argument("--tmp", type=Path, default=None, help="scratch dir for determinism runs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    directory = args.data or Settings.from_env().raw_dir
    tables = load_tables(directory)
    config = load_yaml(args.config)

    tmp_root = None
    if not args.skip_determinism:
        import tempfile

        tmp_root = Path(args.tmp or tempfile.mkdtemp(prefix="fraud-determinism-"))

    checks = run_checks(tables, config, tmp_root)
    write_report(tables, config, checks, args.report)

    failed = [check for check in checks if not check.passed]
    for check in checks:
        logger.info("%-5s %s — %s", "PASS" if check.passed else "FAIL", check.name, check.detail)
    logger.info(
        "%d/%d checks passed, report at %s", len(checks) - len(failed), len(checks), args.report
    )

    if failed:
        # A dataset that fails validation must never be trained on (PLAN §4.8).
        logger.error("%d checks FAILED", len(failed))
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
