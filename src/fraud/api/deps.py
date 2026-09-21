"""Shared dependencies: settings, Redis, RiskModel (PLAN §10).

Everything expensive is built once in the app's lifespan and handed to requests from
there. Loading the model per request would be the obvious mistake — a few hundred
milliseconds of XGBoost deserialisation on every call — but the subtler reason is
consistency: two requests must not be able to answer from two different model versions
while a deploy is in flight.

**Sync, not async.** The endpoints are plain ``def``, so FastAPI runs them in its
threadpool and the sync redis-py client is the right one. Mixing a sync client into an
async endpoint blocks the event loop, which looks fine under one caller and collapses
under load; §10 says pick one, so this picks sync throughout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request
from redis import Redis

from fraud.config import Settings

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from fraud.features.engine import FeatureEngine
    from fraud.scoring.risk_model import RiskModel

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AppState:
    """What the lifespan builds and every request borrows."""

    settings: Settings
    redis: Redis
    model: RiskModel | None = None
    engine: FeatureEngine | None = None
    load_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.model is not None and self.engine is not None


def build_state(settings: Settings | None = None) -> AppState:
    """Open Redis and load the model once (PLAN §8, §10).

    A failure to load is recorded rather than raised. The process still starts and
    ``/health`` reports what is wrong — a service that refuses to boot cannot tell you
    why, and the first thing anyone does with a dead container is ask it.
    """
    resolved = settings or Settings.from_env()
    state = AppState(
        settings=resolved, redis=Redis.from_url(resolved.redis_url, decode_responses=True)
    )

    from fraud.features.accounts import AccountDirectory
    from fraud.features.engine import FeatureEngine
    from fraud.features.store_redis import RedisStore
    from fraud.scoring.risk_model import RiskModel

    try:
        state.model = RiskModel.load()
        state.engine = FeatureEngine(
            RedisStore(state.redis),
            AccountDirectory.from_parquet(resolved.raw_dir / "accounts.parquet"),
        )
        logger.info("API ready: model %s", state.model.model_version)
    except Exception as error:  # noqa: BLE001 - reported through /health, not fatal
        state.load_error = f"{type(error).__name__}: {error}"
        logger.error("API started WITHOUT a model: %s", state.load_error)
    return state


def get_state(request: Request) -> AppState:
    return request.app.state.app_state  # type: ignore[no-any-return]


# Annotated rather than `= Depends(...)` in the signature: the default-argument form is a
# function call evaluated at import (ruff B008), and Annotated is FastAPI's current idiom.
State = Annotated[AppState, Depends(get_state)]


def get_redis(state: State) -> Redis:
    return state.redis


def get_model(state: State) -> RiskModel:
    if state.model is None:
        raise HTTPException(status_code=503, detail=f"model not loaded: {state.load_error}")
    return state.model


def get_engine(state: State) -> FeatureEngine:
    if state.engine is None:
        raise HTTPException(status_code=503, detail=f"engine not ready: {state.load_error}")
    return state.engine


RedisClient = Annotated[Redis, Depends(get_redis)]
Model = Annotated["RiskModel", Depends(get_model)]
Engine = Annotated["FeatureEngine", Depends(get_engine)]
