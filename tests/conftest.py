"""Shared pytest fixtures (PLAN §13)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Settings are read from the environment, so a test that sets one must not leak it (PLAN §13).
_MANAGED_VARS = ("REDIS_URL", "DATA_DIR", "MODEL_DIR", "MODEL_VERSION")


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
