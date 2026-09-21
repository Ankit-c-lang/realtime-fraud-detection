"""FastAPI application and lifespan (PLAN §10).

The lifespan is the whole point of this file. It runs once when the process starts and
once when it stops, and it is where the model, the Redis client and the FeatureEngine are
built. Doing that per request would cost a few hundred milliseconds of XGBoost
deserialisation every call; worse, it would let two concurrent requests answer from two
different model versions during a deploy, which is the kind of inconsistency nobody
notices until someone asks why two identical transactions scored differently.

Read-only by construction. The stream is the single writer of state (invariant 7), so
nothing here commits: ``/score`` runs the engine with ``commit=False``, and the rest of
the endpoints read Redis or Parquet. An API that wrote state would double-count every
transaction it was asked about.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from fraud.api.deps import AppState, build_state
from fraud.api.routes import alerts, health, metrics, score, transactions

logger = logging.getLogger(__name__)

TITLE = "realtime-fraud-detection"
DESCRIPTION = (
    "Near-real-time transaction fraud monitoring. This API is read-only: the stream "
    "scorer is the single writer of state, and /score never commits (PLAN §10)."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the expensive things once, and close them once."""
    state: AppState = build_state()
    app.state.app_state = state
    try:
        yield
    finally:
        state.redis.close()
        logger.info("API shut down")


def create_app(state: AppState | None = None) -> FastAPI:
    """App factory. Passing ``state`` skips the lifespan, which tests use."""
    app = FastAPI(
        title=TITLE,
        description=DESCRIPTION,
        version="1.0",
        lifespan=None if state is not None else lifespan,
    )
    if state is not None:
        app.state.app_state = state

    @app.exception_handler(RedisConnectionError)
    @app.exception_handler(RedisTimeoutError)
    def _redis_unavailable(request: Request, exc: Exception) -> JSONResponse:
        """503, not 500 (PLAN §10).

        A dependency being down is not the same as this service being broken, and the
        distinction is what a load balancer and an on-call engineer both act on.
        """
        logger.warning("Redis unavailable on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=503,
            content={"detail": f"redis unavailable: {exc}", "path": request.url.path},
        )

    for module in (health, score, alerts, transactions, metrics):
        app.include_router(module.router)
    return app


app = create_app()
