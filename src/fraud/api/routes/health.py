"""GET /health: liveness plus the readiness details §10 asks for."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from redis.exceptions import RedisError

from fraud.api.deps import State
from fraud.features.spec import FEATURE_SPEC_VERSION
from fraud.features.store_redis import STATE_VERSION_KEY
from fraud.graph.refresh_live import PUBLISHED_KEY, from_epoch_seconds
from fraud.schemas import HealthResponse
from fraud.stream.scorer import METRICS_KEY

router = APIRouter(tags=["ops"])


@router.get("/health", response_model=HealthResponse)
def health(state: State) -> HealthResponse:
    """Is this process able to serve, and is the rest of the system alive?

    Deliberately never raises. A health endpoint that 500s tells you nothing except that
    it is also broken; this one reports "degraded" plus the specific thing that is wrong,
    which is what someone woken at 3am actually needs.
    """
    redis_ok = True
    state_version: str | None = None
    latest: datetime | None = None
    heartbeat_age: float | None = None

    try:
        state.redis.ping()
        state_version = state.redis.get(STATE_VERSION_KEY)
        published = state.redis.zrange(PUBLISHED_KEY, -1, -1)
        if published:
            latest = from_epoch_seconds(published[0])
        beat = state.redis.hget(METRICS_KEY, "heartbeat")
        if beat:
            heartbeat_age = (datetime.now() - datetime.fromisoformat(beat)).total_seconds()  # noqa: DTZ005
    except RedisError:
        redis_ok = False

    model_version = state.model.model_version if state.model else None
    return HealthResponse(
        status="ok" if (redis_ok and state.ready) else "degraded",
        redis=redis_ok,
        model_version=model_version,
        feature_spec_version=FEATURE_SPEC_VERSION if state.ready else None,
        state_version=state_version,
        latest_snapshot=latest,
        scorer_heartbeat_age_seconds=heartbeat_age,
    )
