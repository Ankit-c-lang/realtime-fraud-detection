"""Redis is reachable and behaves as the stream pipeline will need it to (PLAN §12.2, §13)."""

from __future__ import annotations

import pytest
from redis import Redis

pytestmark = pytest.mark.redis


def test_server_answers_ping(redis_client: Redis) -> None:
    assert redis_client.ping() is True


def test_server_is_the_pinned_major_version(redis_client: Redis) -> None:
    """docker-compose.yml pins redis:8.10.1-alpine; PLAN §12.2 needs Redis >= 7.0."""
    major = int(redis_client.info("server")["redis_version"].split(".")[0])
    assert major >= 7


def test_append_only_persistence_is_enabled(redis_client: Redis) -> None:
    """State must survive a container restart mid-demo (PLAN §12.2)."""
    assert redis_client.config_get("appendonly")["appendonly"] == "yes"


def test_the_fixture_isolates_the_test_database(redis_client: Redis) -> None:
    assert redis_client.dbsize() == 0
    redis_client.set("smoke:key", "value")
    assert redis_client.get("smoke:key") == "value"


def test_stream_primitives_are_available(redis_client: Redis) -> None:
    """The scorer depends on consumer groups (PLAN §9.2); prove they work here."""
    redis_client.xadd("smoke:stream", {"txn_id": "t1"})
    redis_client.xgroup_create("smoke:stream", "scorers", id="0")

    batch = redis_client.xreadgroup("scorers", "consumer-1", {"smoke:stream": ">"}, count=10)
    (_stream, messages) = batch[0]
    message_id, fields = messages[0]

    assert fields == {"txn_id": "t1"}
    assert redis_client.xack("smoke:stream", "scorers", message_id) == 1
