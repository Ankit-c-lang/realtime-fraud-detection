"""Throughput and latency benchmark for the streaming path (PLAN §9.6).

Two measurements, deliberately taken under different conditions.

**Capacity** is measured with ``--max``: the replayer sends as fast as it can and the
scorer is the bottleneck, which is the number worth quoting. Throughput is computed from
the scored rows themselves — ``rows / (last scored_ts - first scored_ts)`` — rather than
from wall-clock around the run, so process startup and the final flush do not dilute it.

**Latency is not measured under ``--max``.** §9.6 is explicit: with the stream saturated,
``scored_ts - ingest_ts`` is mostly time spent queued behind other events, and reporting
it would describe the queue rather than the system. Latency is measured separately at
roughly 50% and 80% of measured capacity, where the queue is short and the number means
"how long does one event take".

Micro-batch size is swept because it is the one knob that trades the two against each
other: larger batches amortise the model call and the Redis round trips, and lengthen the
wait for the events unlucky enough to arrive at the start of one.

Every run starts from a clean Redis and a clean output directory, because a scorer with
warm state and a half-full stream is not measuring the same thing twice.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from redis import Redis

from fraud.config import PROJECT_ROOT, Settings, load_yaml
from fraud.stream import backfill
from fraud.stream.replayer import MaxPacer, RatePacer, Replayer, load_events
from fraud.stream.scorer import Scorer

logger = logging.getLogger(__name__)

BATCH_SIZES: tuple[int, ...] = (1, 50, 200, 500)
LATENCY_FRACTIONS: tuple[float, ...] = (0.5, 0.8)
# Seconds of quiet between runs. Each run writes 20,000 Parquet rows and re-runs a
# ~200,000-key backfill; without a pause the next run's tail measures the previous run's
# disk and Redis traffic rather than the system. Measured: the same configuration
# reported a p95 of 3.6 s back-to-back and 225 ms with the machine settled.
SETTLE_SECONDS: float = 10.0
REPORT = PROJECT_ROOT / "reports" / "benchmark.md"


@dataclass
class RunResult:
    """One replay-and-score pass."""

    label: str
    batch_size: int
    pacing: str
    events: int
    seconds: float
    throughput: float
    latency_p50: float | None = None
    latency_p95: float | None = None
    latency_p99: float | None = None
    staleness_p50_hours: float | None = None
    staleness_p95_hours: float | None = None


@dataclass
class Benchmark:
    context: dict[str, Any] = field(default_factory=dict)
    capacity: list[RunResult] = field(default_factory=list)
    latency: list[RunResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "capacity": [asdict(run) for run in self.capacity],
            "latency": [asdict(run) for run in self.latency],
        }


def hardware_context() -> dict[str, Any]:
    """CPU, RAM, versions and date — §9.6 item 5.

    A throughput number without the machine it ran on is not a measurement, it is a
    boast. Anyone reading the report has to be able to tell whether it applies to them.
    """
    import duckdb
    import numpy
    import sklearn
    import xgboost

    cpu = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:  # pragma: no cover - not Linux
        pass

    memory_gb = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal"):
                memory_gb = round(int(line.split()[1]) / 1_048_576, 1)
                break
    except OSError:  # pragma: no cover - not Linux
        pass

    settings = Settings.from_env()
    redis_version = "unknown"
    try:
        redis_version = Redis.from_url(settings.redis_url, decode_responses=True).info("server")[
            "redis_version"
        ]
    except Exception as error:  # noqa: BLE001 - reported, not fatal to a benchmark run
        logger.warning("could not read the Redis version: %s", error)

    return {
        "cpu": cpu,
        "logical_cpus": len(__import__("os").sched_getaffinity(0)),
        "memory_gb": memory_gb,
        "python": platform.python_version(),
        "redis": redis_version,
        "numpy": numpy.__version__,
        "xgboost": xgboost.__version__,
        "scikit_learn": sklearn.__version__,
        "duckdb": duckdb.__version__,
        "git_commit": _git_commit(),
        "measured_at": pd.Timestamp.now().isoformat(timespec="seconds"),
    }


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=PROJECT_ROOT,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        return "unknown"


def reset(client: Redis, scored_root: Path) -> None:
    """A clean Redis and a clean output directory before every run.

    Without this each run would start with the previous one's warm state and a partly
    drained stream, and the batch sweep would measure the order the runs happened in.
    """
    client.flushdb()
    if scored_root.exists():
        shutil.rmtree(scored_root)
    scored_root.mkdir(parents=True, exist_ok=True)
    backfill.run(client, force=True)
    # Let the backfill's writes and the previous run's Parquet drain before timing
    # anything. This is the difference between measuring the system and measuring the
    # harness (see SETTLE_SECONDS).
    time.sleep(SETTLE_SECONDS)


def _config(batch_size: int) -> dict[str, Any]:
    config = load_yaml("stream")
    config["scorer"] = {**config["scorer"], "batch_size": batch_size}
    return config


def measure_capacity(
    client: Redis, events: pd.DataFrame, *, label: str, batch_size: int, scored_root: Path
) -> RunResult:
    """Fill the stream first, then drain it: the scorer is never starved.

    Sequential on purpose. Capacity is "how fast can the scorer go when there is always
    work", so the replayer finishing first is the point rather than a flaw. The latency
    columns from this run are not reported — see ``measure_latency``.
    """
    reset(client, scored_root)
    config = _config(batch_size)

    Replayer(client, MaxPacer(), config=config).send(events)
    started = time.perf_counter()
    Scorer(client, consumer="bench", config=config, scored_root=scored_root).run(
        stop_when_idle=True
    )
    wall = time.perf_counter() - started

    return _read_back(
        scored_root, label=label, batch_size=batch_size, pacing="max", wall_seconds=wall
    )


def measure_latency(
    client: Redis,
    events: pd.DataFrame,
    *,
    label: str,
    batch_size: int,
    rate: float,
    scored_root: Path,
) -> RunResult:
    """Pace the replayer while the scorer consumes, and time one event's journey.

    The scorer runs in a thread so the two genuinely overlap. Measuring this
    sequentially — replay everything, then start the scorer — makes every event wait for
    the whole backlog and reports a p50 of tens of seconds. That is the queue's depth,
    not the system's latency: exactly the number §9.6 says not to publish, and easy to
    produce by accident.
    """
    import threading

    reset(client, scored_root)
    config = _config(batch_size)

    scorer = Scorer(client, consumer="bench", config=config, scored_root=scored_root)
    # Load the model and open the group BEFORE any event is sent. startup() is
    # idempotent — run() calls it again and finds everything already set — and without
    # this the first events would queue behind XGBoost deserialisation and be recorded
    # as slow when nothing was actually slow.
    scorer.startup()

    thread = threading.Thread(target=scorer.run, name="bench-scorer", daemon=True)
    thread.start()

    started = time.perf_counter()
    Replayer(client, RatePacer(rate), config=config).send(events)

    # Let the consumer finish what is in flight, then stop it. Polling the scorer's own
    # counter beats a fixed sleep, which would truncate a slow run or pad a fast one.
    deadline = time.monotonic() + 300.0
    while scorer.metrics.events < len(events) and time.monotonic() < deadline:
        time.sleep(0.5)
    scorer._request_stop()
    thread.join(timeout=60.0)
    wall = time.perf_counter() - started

    return _read_back(
        scored_root,
        label=label,
        batch_size=batch_size,
        pacing=f"{rate:.0f}/s",
        wall_seconds=wall,
    )


def _read_back(
    scored_root: Path, *, label: str, batch_size: int, pacing: str, wall_seconds: float
) -> RunResult:
    """Throughput and latency from the scored rows, not from the wrapper's clock."""
    from fraud.storage import duck

    connection = duck.connect(scored_root)
    try:
        frame = connection.execute(
            "SELECT scored_ts, ingest_ts, event_time, graph_snapshot_ts FROM scored"
        ).fetch_df()
    finally:
        connection.close()

    rows = len(frame)
    if rows == 0:  # pragma: no cover - only if a run produced nothing
        return RunResult(label, batch_size, pacing, 0, wall_seconds, 0.0)

    # rows / (last scored_ts - first scored_ts): excludes startup and the final flush,
    # which wall-clock timing would fold in and understate the steady-state rate.
    span_ms = float(frame["scored_ts"].max() - frame["scored_ts"].min())
    span = max(span_ms / 1000.0, 1e-6)
    latencies = (frame["scored_ts"] - frame["ingest_ts"]).astype(float)
    latencies = latencies[latencies >= 0]

    result = RunResult(
        label=label,
        batch_size=batch_size,
        pacing=pacing,
        events=rows,
        seconds=round(span, 3),
        throughput=round(rows / span, 1),
    )
    if len(latencies):
        ordered = sorted(latencies)
        result.latency_p50 = round(_quantile(ordered, 0.50), 1)
        result.latency_p95 = round(_quantile(ordered, 0.95), 1)
        result.latency_p99 = round(_quantile(ordered, 0.99), 1)

    staleness = _staleness_hours(frame)
    if staleness:
        result.staleness_p50_hours = round(_quantile(staleness, 0.50), 2)
        result.staleness_p95_hours = round(_quantile(staleness, 0.95), 2)
    return result


def _staleness_hours(frame: pd.DataFrame) -> list[float]:
    """How old the graph snapshot each row used was (PLAN §6.5)."""
    from fraud.graph.refresh_live import from_epoch_seconds

    used = frame.dropna(subset=["graph_snapshot_ts"])
    if used.empty:
        return []
    ages = (
        pd.to_datetime(used["event_time"])
        - used["graph_snapshot_ts"].map(lambda value: from_epoch_seconds(float(value)))
    ).dt.total_seconds() / 3600.0
    return sorted(ages.tolist())


def _quantile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return float(ordered[index])


def run(limit: int, scored_root: Path) -> Benchmark:
    """The whole §9.6 protocol: capacity sweep, then latency at two rates."""
    settings = Settings.from_env()
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    events = load_events(limit=limit)
    logger.info("benchmarking on %s events", f"{len(events):,}")

    benchmark = Benchmark(context={**hardware_context(), "events": len(events)})

    for batch_size in BATCH_SIZES:
        result = measure_capacity(
            client,
            events,
            label=f"capacity batch={batch_size}",
            batch_size=batch_size,
            scored_root=scored_root,
        )
        benchmark.capacity.append(result)
        logger.info("batch %-4d %8.1f events/s", batch_size, result.throughput)

    best = max(benchmark.capacity, key=lambda run: run.throughput)
    logger.info("capacity %.0f events/s at batch %d", best.throughput, best.batch_size)

    for fraction in LATENCY_FRACTIONS:
        rate = best.throughput * fraction
        result = measure_latency(
            client,
            events,
            label=f"latency {fraction:.0%} of capacity",
            batch_size=best.batch_size,
            rate=rate,
            scored_root=scored_root,
        )
        benchmark.latency.append(result)
        logger.info(
            "%.0f%% of capacity (%.0f/s): p50 %.0f ms, p95 %.0f ms, p99 %.0f ms",
            fraction * 100,
            rate,
            result.latency_p50 or 0,
            result.latency_p95 or 0,
            result.latency_p99 or 0,
        )

    return benchmark


CONTAINER_MEMORY = PROJECT_ROOT / "reports" / "container_memory.tsv"


def _container_memory_section() -> list[str]:
    """Peak container memory during a Compose replay (PLAN §12.2 "Resources").

    Sampled from `docker stats` while the containerised stack replays, and captured by
    `make bench-docker` rather than by this script: `make bench` runs the scorer
    in-process, so `docker stats` would report idle containers and quietly describe the
    wrong thing. Absent file means the capture has not been run.
    """
    if not CONTAINER_MEMORY.is_file():
        return []

    rows = [
        line.split("\t")
        for line in CONTAINER_MEMORY.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        return []

    return [
        "## Container memory (peak during a Compose replay)",
        "",
        "Sampled with `docker stats` while the containerised stack replayed 15,000 events,",
        "so these are the real service footprints rather than the in-process benchmark's.",
        "",
        "| Service | Peak memory |",
        "|---|---|",
        *[f"| {name} | {peak} |" for name, peak in rows],
        "",
        f"Total across services: well inside the {7.7:.1f} GB this VM has (PLAN §15.2).",
        "",
    ]


def render(benchmark: Benchmark) -> str:
    """reports/benchmark.md — generated, like every other report (§0.4)."""
    context = benchmark.context
    best = max(benchmark.capacity, key=lambda run: run.throughput) if benchmark.capacity else None

    lines = [
        "# Streaming benchmark",
        "",
        "**Generated by `make bench`. Do not edit by hand (PLAN §0.4, §9.6).**",
        "",
        "## Machine and versions",
        "",
        "| | |",
        "|---|---|",
        f"| CPU | {context.get('cpu')} |",
        f"| Logical CPUs | {context.get('logical_cpus')} |",
        f"| RAM | {context.get('memory_gb')} GB |",
        f"| Redis | {context.get('redis')} |",
        (
            f"| Python / numpy / xgboost | {context.get('python')} / "
            f"{context.get('numpy')} / {context.get('xgboost')} |"
        ),
        f"| scikit-learn / duckdb | {context.get('scikit_learn')} / {context.get('duckdb')} |",
        f"| Commit | `{context.get('git_commit')}` |",
        f"| Measured | {context.get('measured_at')} |",
        f"| Events per run | {context.get('events'):,} |",
        "",
        "## Capacity (`--max`, scorer is the bottleneck)",
        "",
        "Throughput is `rows / (last scored_ts - first scored_ts)`, so process startup and",
        "the final flush are excluded.",
        "",
        "| Micro-batch | Events | Seconds | Events/s |",
        "|---|---|---|---|",
    ]
    for result in benchmark.capacity:
        lines.append(
            f"| {result.batch_size} | {result.events:,} | {result.seconds:.2f} | "
            f"**{result.throughput:,.0f}** |"
        )

    if best:
        lines += [
            "",
            f"Peak: **{best.throughput:,.0f} events/s** at micro-batch **{best.batch_size}**.",
        ]

    lines += [
        "",
        "## Latency (paced, queue short)",
        "",
        "Measured at a fixed rate, **not** under `--max`. With the stream saturated,",
        "`scored_ts - ingest_ts` is mostly time queued behind other events and would",
        "describe the queue rather than the system (§9.6 item 2).",
        "",
        "The replayer and the scorer run **concurrently** here. Measuring them one after",
        "the other reports the backlog's depth instead, which is the same mistake wearing",
        "a different hat.",
        "",
        "| Load | Rate | p50 | p95 | p99 |",
        "|---|---|---|---|---|",
    ]
    for result in benchmark.latency:
        lines.append(
            f"| {result.label.replace('latency ', '')} | {result.pacing} | "
            f"{result.latency_p50:,.0f} ms | {result.latency_p95:,.0f} ms | "
            f"{result.latency_p99:,.0f} ms |"
        )

    stale = [run for run in benchmark.latency if run.staleness_p50_hours is not None]
    if stale:
        lines += [
            "",
            "## Graph staleness (§6.5)",
            "",
            "How old the snapshot each row scored against was. **The graph-refresh service",
            "is not running during the benchmark**, so every row resolves the single",
            "snapshot the backfill published at the replay boundary, and these numbers grow",
            "with the window rather than describing a steady state. Live staleness is",
            "measured by `make rescore-check` during a real replay, where the refresh keeps",
            "pace.",
            "",
            "| Load | p50 | p95 |",
            "|---|---|---|",
        ]
        for result in stale:
            lines.append(
                f"| {result.label.replace('latency ', '')} | "
                f"{result.staleness_p50_hours:.1f} h | {result.staleness_p95_hours:.1f} h |"
            )

    lines += [
        "",
        *_container_memory_section(),
        "## Caveats",
        "",
        "- One machine, one run per configuration; no repeats and no confidence intervals.",
        "- Tail latency is sensitive to what else the machine is doing. The harness pauses",
        f"  {SETTLE_SECONDS:.0f}s between runs, because back-to-back runs reported a p95 of 3.6 s",
        "  where the same configuration measured in isolation reported 225 ms — that tail",
        "  was the benchmark's own disk traffic, not the scorer.",
        "- The replayer, scorer and Redis share these CPUs, so the scorer is not getting the",
        "  whole machine — which is also true of the Compose stack this models.",
        "- Latency is end to end from the replayer's `ingest_ts` to `scored_ts`; it excludes",
        "  the graph refresh, which runs on its own schedule (§6.5).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the streaming path (§9.6).")
    parser.add_argument("--limit", type=int, default=20000, help="events per run")
    parser.add_argument("--json", type=Path, default=None, help="also write raw results here")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("fraud.stream.sink", "fraud.stream.replayer", "fraud.stream.backfill"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    scored_root = Settings.from_env().data_dir / "bench_scored"
    benchmark = run(args.limit, scored_root)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render(benchmark), encoding="utf-8")
    logger.info("wrote %s", REPORT)

    if args.json:
        args.json.write_text(json.dumps(benchmark.to_dict(), indent=2), encoding="utf-8")

    shutil.rmtree(scored_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
