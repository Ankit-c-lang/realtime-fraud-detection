"""Shared pytest fixtures (PLAN §13)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

# Settings are read from the environment, so a test that sets one must not leak it (PLAN §13).
_MANAGED_VARS = ("REDIS_URL", "DATA_DIR", "MODEL_DIR", "MODEL_VERSION")

# Integration tests only ever touch this database, never the one the services use.
_TEST_DB = 15


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every configuration variable so a test sees the repository defaults."""
    for var in _MANAGED_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(scope="session")
def redis_url() -> str:
    """The Redis server integration tests talk to. CI and local runs both use DB 15."""
    return os.environ.get("REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def redis_client(redis_url: str) -> Iterator[Redis]:
    """A client on the test database, flushed before and after so tests cannot leak state."""
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        client.ping()
    except RedisConnectionError as exc:  # pragma: no cover - environment problem, not a defect
        pytest.fail(f"No Redis at {redis_url}. Start it with `make redis-up`. ({exc})")

    if client.connection_pool.connection_kwargs.get("db") != _TEST_DB:
        pytest.fail(f"Refusing to flush {redis_url}: integration tests must use DB {_TEST_DB}.")

    client.flushdb()
    try:
        yield client
    finally:
        client.flushdb()
        client.close()
