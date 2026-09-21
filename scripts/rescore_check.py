"""Re-score the live output offline and assert it matches row for row (PLAN §9.6).

This is the check that makes "one FeatureEngine, one RiskModel" a verified claim rather
than an architectural intention. The streaming path and the offline path share their
code, but they do not share their *state*: one built it event by event through Redis, the
other through an in-memory store, starting from the same checkpoint. If those two states
have drifted anywhere, the features drift, the scores drift, and every number in
`reports/results.md` stops describing the running system — silently, because both halves
still look entirely reasonable on their own.

Three comparisons, all from §9.6 item 3:

1. **Re-score.** Feed the stored features straight back through `RiskModel`. ``p_xgb``,
   ``risk`` and ``decision`` must match the live values to 1e-9. This isolates the
   serving path: same features in, same score out.
2. **Features.** Compare the live hot features against `hot_features.parquet` for the
   replay window. They must be identical, because both start from the same state at
   ``test_start`` and process the same events in the same order.
3. **Graph parity.** For sampled boundaries, the live snapshot must equal the offline one.

A failure here is not a tolerance to widen. It means the online and offline systems are
computing different things, and the only useful response is to find out which one is
wrong.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd

from fraud.config import Settings
from fraud.features.spec import HOT_FEATURE_NAMES
from fraud.graph.algorithms import SNAPSHOT_COLUMNS
from fraud.storage import duck

logger = logging.getLogger(__name__)

TOLERANCE: Final[float] = 1e-9
GRAPH_TOLERANCE: Final[float] = 1e-9
SCORE_COLUMNS: Final[tuple[str, ...]] = ("p_xgb", "risk")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    worst: float | None = None


@dataclass
class CheckReport:
    checks: list[Check] = field(default_factory=list)
    rows: int = 0

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        level = logging.INFO if check.passed else logging.ERROR
        logger.log(
            level, "%-22s %s  %s", check.name, "PASS" if check.passed else "FAIL", check.detail
        )
        return check


def load_live(scored_root: Path | None = None) -> pd.DataFrame:
    """The scorer's output, deduplicated by the §9.4 view."""
    root = scored_root or Settings.from_env().scored_dir
    if not duck.has_scored_data(root):
        raise FileNotFoundError(
            f"no scored output under {root}. Run the replay first (PLAN §9.1-§9.2)."
        )
    connection = duck.connect(root)
    try:
        return connection.execute("SELECT * FROM scored ORDER BY txn_id").fetch_df()
    finally:
        connection.close()


def check_rescore(live: pd.DataFrame, report: CheckReport) -> Check:
    """§9.6 item 3a: the stored features must reproduce the stored scores."""
    from fraud.scoring.risk_model import RiskModel

    model = RiskModel.load()
    scored = model.score_batch(live, explain=False)

    worst = 0.0
    mismatched: list[str] = []
    for column, produced in (("p_xgb", scored.p_xgb), ("risk", scored.risk)):
        difference = float(np.abs(live[column].to_numpy() - produced).max())
        worst = max(worst, difference)
        if difference > TOLERANCE:
            mismatched.append(f"{column} max |diff| {difference:.3e}")

    decisions_differ = int((live["decision"].to_numpy() != scored.decision).sum())
    if decisions_differ:
        mismatched.append(f"{decisions_differ} decision(s) differ")

    return report.add(
        Check(
            name="re-score",
            passed=not mismatched,
            detail=(
                f"{len(live):,} rows, max |diff| {worst:.2e} (tolerance {TOLERANCE:.0e})"
                if not mismatched
                else "; ".join(mismatched)
            ),
            worst=worst,
        )
    )


def check_features(
    live: pd.DataFrame, report: CheckReport, features_path: Path | None = None
) -> Check:
    """§9.6 item 3b: live hot features must equal the offline ones, exactly.

    Only the hot features. The warm ones legitimately differ — the live system may be a
    snapshot behind while one is still being computed, which is what §6.5 calls
    staleness and reports separately rather than treating as an error.
    """
    settings = Settings.from_env()
    offline = pd.read_parquet(
        features_path or settings.features_dir / "hot_features.parquet",
        columns=["txn_id", *HOT_FEATURE_NAMES],
    )
    merged = live[["txn_id", *HOT_FEATURE_NAMES]].merge(
        offline, on="txn_id", suffixes=("_live", "_offline"), validate="one_to_one"
    )

    missing = len(live) - len(merged)
    differing: list[str] = []
    worst = 0.0
    for name in HOT_FEATURE_NAMES:
        left, right = merged[f"{name}_live"], merged[f"{name}_offline"]
        # is_numeric_dtype, not `== object`: pandas 3 backs string columns with Arrow,
        # so merchant_category is dtype "str" and the object test silently misses it —
        # then to_numpy(float) raises on real data.
        if not (pd.api.types.is_numeric_dtype(left) and pd.api.types.is_numeric_dtype(right)):
            count = int((left.astype(str) != right.astype(str)).sum())
            if count:
                differing.append(f"{name}: {count} rows")
            continue
        difference = float(np.abs(left.to_numpy(float) - right.to_numpy(float)).max())
        worst = max(worst, difference)
        if difference > TOLERANCE:
            differing.append(f"{name}: max |diff| {difference:.3e}")

    problems = differing[:5]
    if missing:
        problems.insert(0, f"{missing} live row(s) absent from hot_features.parquet")

    return report.add(
        Check(
            name="hot features",
            passed=not problems,
            detail=(
                f"{len(merged):,} rows compared across {len(HOT_FEATURE_NAMES)} features, "
                f"max |diff| {worst:.2e}"
                if not problems
                else "; ".join(problems)
            ),
            worst=worst,
        )
    )


def check_graph_parity(
    report: CheckReport,
    *,
    sample: int = 3,
    live_dir: Path | None = None,
    offline_dir: Path | None = None,
) -> Check:
    """§9.6 item 3c: live and offline snapshots must agree at sampled boundaries."""
    settings = Settings.from_env()
    live_root = live_dir or settings.graph_live_dir
    offline_root = offline_dir or settings.graph_offline_dir

    live_dirs = sorted(live_root.glob("snapshot_ts=*")) if live_root.is_dir() else []
    if not live_dirs:
        return report.add(
            Check(
                name="graph parity",
                passed=True,
                detail="skipped: no live snapshots yet (graph-refresh has not run)",
            )
        )

    chosen = live_dirs[:: max(1, len(live_dirs) // sample)][:sample]
    problems: list[str] = []
    worst = 0.0

    for directory in chosen:
        stamp = directory.name.split("=", 1)[1]
        offline_path = offline_root / directory.name / "part.parquet"
        if not offline_path.is_file():
            problems.append(f"{stamp}: no offline snapshot to compare")
            continue

        left = pd.read_parquet(directory / "part.parquet").set_index("account_id").sort_index()
        right = pd.read_parquet(offline_path).set_index("account_id").sort_index()
        if set(left.index) != set(right.index):
            only_live = len(set(left.index) - set(right.index))
            only_offline = len(set(right.index) - set(left.index))
            problems.append(f"{stamp}: {only_live} live-only, {only_offline} offline-only accounts")
            continue

        for name in SNAPSHOT_COLUMNS:
            if name in ("snapshot_ts", "account_id"):
                continue
            difference = float(
                np.abs(
                    left[name].to_numpy(float) - right.loc[left.index, name].to_numpy(float)
                ).max()
            )
            worst = max(worst, difference)
            if difference > GRAPH_TOLERANCE:
                problems.append(f"{stamp}/{name}: max |diff| {difference:.3e}")

    return report.add(
        Check(
            name="graph parity",
            passed=not problems,
            detail=(
                f"{len(chosen)} boundary/boundaries compared, max |diff| {worst:.2e}"
                if not problems
                else "; ".join(problems[:5])
            ),
            worst=worst,
        )
    )


def report_staleness(live: pd.DataFrame) -> str:
    """Graph staleness, reported rather than asserted (PLAN §6.5)."""
    if "graph_snapshot_ts" not in live.columns or live["graph_snapshot_ts"].isna().all():
        return "graph staleness: no snapshot was resolved for any row"

    from fraud.graph.refresh_live import from_epoch_seconds

    used = live["graph_snapshot_ts"].dropna()
    ages = (
        pd.to_datetime(live.loc[used.index, "event_time"])
        - used.map(lambda value: from_epoch_seconds(float(value)))
    ).dt.total_seconds() / 3600.0
    return (
        f"graph staleness (hours): p50 {ages.quantile(0.5):.1f}, p95 {ages.quantile(0.95):.1f}, "
        f"max {ages.max():.1f}; {len(used):,}/{len(live):,} rows resolved a snapshot"
    )


def run(scored_root: Path | None = None, *, sample: int = 3) -> CheckReport:
    live = load_live(scored_root)
    report = CheckReport(rows=len(live))

    check_rescore(live, report)
    check_features(live, report)
    check_graph_parity(report, sample=sample)
    logger.info("%s", report_staleness(live))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-score the live output (PLAN §9.6).")
    parser.add_argument("--sample", type=int, default=3, help="graph boundaries to compare")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = run(sample=args.sample)

    if not report.passed:
        logger.error(
            "re-score check FAILED. The online and offline systems are computing "
            "different things; do not widen the tolerance (PLAN §9.6)."
        )
        return 1
    logger.info("re-score check passed on %s rows", f"{report.rows:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
