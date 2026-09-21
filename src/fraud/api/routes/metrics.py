"""GET /metrics: what the scorer is doing right now (PLAN §10)."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter

from fraud.api.deps import RedisClient
from fraud.config import load_yaml
from fraud.schemas import MetricsResponse
from fraud.stream.scorer import LATENCY_KEY, METRICS_KEY, WATERMARK_KEY

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

# Throughput is measured over a short trailing window rather than since startup, which
# would average away exactly the slowdown anyone looking at this page cares about.
_WINDOW_SECONDS = 10.0
_previous: dict[str, Any] = {"at": 0.0, "events": 0}


@router.get("/metrics", response_model=MetricsResponse)
def metrics(redis: RedisClient) -> MetricsResponse:
    """Counters, lag and latency percentiles, read straight from Redis."""
    stream_cfg = load_yaml("stream")["stream"]
    stream, group = str(stream_cfg["name"]), str(stream_cfg["group"])

    counters = redis.hgetall(METRICS_KEY)
    events = int(counters.get("events", 0) or 0)

    stream_length = int(redis.xlen(stream) or 0)
    lag, pending = _group_state(redis, stream, group)
    latency = [float(value) for value in redis.lrange(LATENCY_KEY, 0, -1)]
    watermark = redis.get(WATERMARK_KEY)

    return MetricsResponse(
        events=events,
        alerts=int(counters.get("alerts", 0) or 0),
        dead_lettered=int(counters.get("dead_lettered", 0) or 0),
        flushes=int(counters.get("flushes", 0) or 0),
        stream_length=stream_length,
        consumer_lag=lag,
        pending=pending,
        dlq_size=int(redis.xlen(str(stream_cfg["dlq"])) or 0),
        throughput_per_second=_throughput(events),
        latency_p50_ms=_percentile(latency, 0.50),
        latency_p95_ms=_percentile(latency, 0.95),
        watermark=datetime.fromisoformat(watermark) if watermark else None,
    )


def _group_state(redis: Any, stream: str, group: str) -> tuple[int | None, int]:
    """Consumer lag and pending count, or (None, 0) before the group exists."""
    try:
        for found in redis.xinfo_groups(stream):
            if found.get("name") == group:
                raw = found.get("lag")
                return (None if raw is None else int(raw)), int(found.get("pending", 0))
    except Exception:  # noqa: BLE001 - no stream yet is not an error worth a 500
        return None, 0
    return None, 0


def _throughput(events: int) -> float:
    """Events per second since the previous call, over at least ``_WINDOW_SECONDS``.

    Module state, not a background task: this endpoint is polled by the dashboard, so the
    previous poll is exactly the sample needed and a scheduler would be machinery for
    nothing.
    """
    now = time.monotonic()
    baseline_at = float(_previous["at"])
    if baseline_at == 0.0:  # first call: no interval to measure over yet
        _previous.update(at=now, events=events)
        return 0.0

    elapsed = now - baseline_at
    rate = max(0.0, (events - int(_previous["events"])) / elapsed) if elapsed > 0 else 0.0
    if elapsed >= _WINDOW_SECONDS:
        # Move the baseline only once the window has really elapsed, so a burst of polls
        # does not shrink the interval to nothing and report a meaningless rate.
        _previous.update(at=now, events=events)
    return rate


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]
