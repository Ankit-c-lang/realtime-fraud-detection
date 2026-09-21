"""GET /alerts: the recent-alert ring buffer (PLAN §3.6, §10)."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from fraud.api.deps import RedisClient
from fraud.schemas import Alert
from fraud.stream.scorer import ALERTS_KEY

logger = logging.getLogger(__name__)

router = APIRouter(tags=["monitoring"])


@router.get("/alerts", response_model=list[Alert])
def alerts(
    redis: RedisClient,
    decision: Annotated[Literal["REVIEW", "HOLD"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[Alert]:
    """The most recent alerts, newest first.

    Bounded by construction: the scorer LTRIMs this list, and ``limit`` is capped, so no
    request can ask for an unbounded read. ``decision`` filters after the fetch because
    the list is small by design — a Redis-side filter would need a second index for no
    benefit at 500 entries.
    """
    raw = redis.lrange(ALERTS_KEY, 0, limit * 4 if decision else limit - 1)

    found: list[Alert] = []
    for entry in raw:
        try:
            payload = json.loads(entry)
        except (TypeError, ValueError):  # pragma: no cover - a malformed entry is skipped
            logger.warning("skipping unparseable alert entry")
            continue
        if decision and payload.get("decision") != decision:
            continue
        found.append(Alert(**payload))
        if len(found) >= limit:
            break
    return found
