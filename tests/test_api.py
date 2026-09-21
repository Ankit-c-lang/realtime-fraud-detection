"""The FastAPI service (PLAN §10, §13).

Two properties here are load-bearing.

*`/score` never writes state.* The stream scorer is the single writer; if this endpoint
committed, a transaction scored here and then delivered on the stream would be folded
into the account twice. That is not asserted by reading the code — the tests snapshot
every state, feature and entity key in Redis before and after a call and compare them
literally.

*A body carrying a label is refused.* Leakage rule L4 is enforced at the boundary, so a
request containing `is_fraud` gets a 422 rather than being silently ignored and scored.

Validation tests need neither Redis nor a model and run in the unit suite; the rest use
the DB-15 fixture and are auto-marked as integration tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from feature_helpers import ACCOUNT, CREATED_AT, HOME_CITY, MUMBAI, directory
from redis import Redis

from fraud.api.deps import AppState
from fraud.api.main import create_app
from fraud.config import Settings
from fraud.features.engine import FeatureEngine
from fraud.features.store_redis import RedisStore
from fraud.graph.refresh_live import PUBLISHED_KEY, snapshot_key, to_epoch_seconds
from fraud.schemas import EventRequest
from fraud.stream.scorer import ALERTS_KEY, METRICS_KEY

EVENT_TIME = datetime(2026, 3, 14, 10, 0, 0)  # noqa: DTZ001 - naive IST, per PLAN §3.5


def _body(**overrides: Any) -> dict[str, Any]:
    body = {
        "txn_id": "T0000001",
        "event_time": EVENT_TIME.isoformat(),
        "account_id": ACCOUNT,
        "merchant_id": "M000001",
        "merchant_category": "grocery",
        "amount": 2500.0,
        "channel": "POS",
        "device_id": "D0000001",
        "ip": "49.1.1.1",
        "lat": 19.076,
        "lon": 72.8777,
        "city": HOME_CITY,
        "country": "IN",
        "status": "APPROVED",
    }
    body.update(overrides)
    return body


# --- request validation (no Redis, no model) -----------------------------------------


def test_a_valid_body_parses() -> None:
    assert EventRequest(**_body()).to_event().txn_id == "T0000001"


def test_a_body_carrying_a_label_is_refused() -> None:
    """Leakage rule L4 at the boundary (PLAN §10)."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="is_fraud"):
        EventRequest(**_body(is_fraud=1))


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"amount": 0}, "amount"),
        ({"amount": -1.0}, "amount"),
        ({"channel": "ATM"}, "channel"),
        ({"status": "PENDING"}, "status"),
        ({"merchant_category": "not-a-category"}, "merchant_category"),
        ({"lat": 120.0}, "lat"),
        ({"lon": -200.0}, "lon"),
        ({"ip": "not-an-ip"}, "ip"),
        ({"country": "IND"}, "country"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_field_constraints(overrides: dict[str, Any], field: str) -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError, match=field):
        EventRequest(**_body(**overrides))


def test_a_timezone_aware_event_time_is_refused() -> None:
    """A tz-aware value would shift every window by the offset (PLAN §3.5)."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="naive IST"):
        EventRequest(**_body(event_time="2026-03-14T10:00:00+05:30"))


def test_categories_stay_in_sync_with_config() -> None:
    """The validator reads categories.yaml, so the two cannot drift (L7)."""
    from fraud.features.spec import category_levels

    for level in category_levels():
        assert EventRequest(**_body(merchant_category=level)).merchant_category == level


# --- the app -------------------------------------------------------------------------


class _Scored:
    def __init__(self, decision: str = "ALLOW") -> None:
        import numpy as np

        self.p_xgb = np.array([0.62])
        self.anomaly_pct = np.array([0.41])
        self.risk = np.array([0.515])
        self.decision = np.array([decision], dtype=object)
        self.reasons = [[_Reason()]]
        self.model_version = "v1"
        self.feature_spec_version = "fs1"


class _Reason:
    text = "8 payments in the previous 5 minutes"

    def to_dict(self) -> dict[str, Any]:
        return {"feature": "acct_cnt_5m", "value": 8.0, "contribution": 1.5, "text": self.text}


class _Model:
    model_version = "v1"

    def __init__(self, decision: str = "ALLOW") -> None:
        self._decision = decision
        self.seen: list[pd.DataFrame] = []

    def score_batch(self, frame: pd.DataFrame, *, explain: bool = True) -> _Scored:
        self.seen.append(frame.copy())
        return _Scored(self._decision)


def _client(redis_client: Redis, *, model: Any = None, ready: bool = True) -> TestClient:
    store = RedisStore(redis_client)
    store.set_state_version()
    state = AppState(settings=Settings.from_env(), redis=redis_client)
    if ready:
        state.model = model or _Model()
        state.engine = FeatureEngine(
            store,
            directory({ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT)}),
        )
    else:
        state.load_error = "no model folder"
    return TestClient(create_app(state))


def _snapshot(redis_client: Redis) -> dict[str, Any]:
    """Every key the scorer would write, so "nothing changed" is literal."""
    state: dict[str, Any] = {}
    for key in sorted(redis_client.keys("*")):
        kind = redis_client.type(key)
        state[key] = (
            redis_client.zrange(key, 0, -1, withscores=True)
            if kind == "zset"
            else redis_client.get(key)
            if kind == "string"
            else redis_client.hgetall(key)
            if kind == "hash"
            else redis_client.lrange(key, 0, -1)
            if kind == "list"
            else kind
        )
    return state


# --- /health -------------------------------------------------------------------------


def test_health_reports_ok_when_ready(redis_client: Redis) -> None:
    body = _client(redis_client).get("/health").json()
    assert body["status"] == "ok"
    assert body["redis"] is True
    assert body["model_version"] == "v1"
    assert body["feature_spec_version"] == "fs1"
    assert body["state_version"] == "fs1"


def test_health_reports_degraded_without_a_model(redis_client: Redis) -> None:
    """Degraded, not dead: a health endpoint that cannot answer tells you nothing."""
    response = _client(redis_client, ready=False).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["model_version"] is None


def test_health_reports_the_latest_snapshot_and_heartbeat(redis_client: Redis) -> None:
    stamp = to_epoch_seconds(EVENT_TIME)
    redis_client.zadd(PUBLISHED_KEY, {str(stamp): stamp})
    redis_client.hset(METRICS_KEY, "heartbeat", datetime.now().isoformat())  # noqa: DTZ005

    body = _client(redis_client).get("/health").json()
    assert body["latest_snapshot"].startswith("2026-03-14T10:00")
    assert 0 <= body["scorer_heartbeat_age_seconds"] < 60


# --- /score: read-only (PLAN §10) -----------------------------------------------------


def test_score_returns_the_planned_shape(redis_client: Redis) -> None:
    body = _client(redis_client).post("/score", json=_body()).json()
    assert set(body) == {
        "txn_id",
        "fraud_probability",
        "anomaly_percentile",
        "risk_score",
        "decision",
        "reasons",
        "model_version",
        "graph_snapshot_ts",
        "already_processed",
        "state_updated",
    }
    assert body["state_updated"] is False
    assert body["already_processed"] is False
    assert body["reasons"][0]["feature"] == "acct_cnt_5m"


def test_score_writes_no_state(redis_client: Redis) -> None:
    """The headline promise, checked against the whole database (PLAN §10)."""
    client = _client(redis_client)
    before = _snapshot(redis_client)

    assert client.post("/score", json=_body()).status_code == 200

    assert _snapshot(redis_client) == before
    assert redis_client.keys("state:acct:*") == []
    assert redis_client.keys("feat:*") == []
    assert redis_client.keys("ent:*") == []


def test_repeated_scoring_still_writes_no_state(redis_client: Redis) -> None:
    """Ten calls must be as harmless as one; a leak would compound."""
    client = _client(redis_client)
    before = _snapshot(redis_client)
    for index in range(10):
        client.post("/score", json=_body(txn_id=f"T{index:07d}"))
    assert _snapshot(redis_client) == before


def test_score_does_not_disturb_state_the_scorer_wrote(redis_client: Redis) -> None:
    """The real failure this prevents: double-counting against live account state."""
    from feature_helpers import make_event

    store = RedisStore(redis_client)
    engine = FeatureEngine(store, directory({ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT)}))
    engine.process(make_event("T9999999", EVENT_TIME - timedelta(minutes=5)))

    # Build the client first: it claims meta:state_version, which is setup, not scoring.
    client = _client(redis_client)
    before = _snapshot(redis_client)

    client.post("/score", json=_body())
    assert _snapshot(redis_client) == before


def test_an_already_processed_transaction_is_flagged(redis_client: Redis) -> None:
    """Recomputing would score against today's state, not the state as it stood."""
    from feature_helpers import make_event

    store = RedisStore(redis_client)
    engine = FeatureEngine(store, directory({ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT)}))
    engine.process(make_event("T0000001", EVENT_TIME), extra={"graph_snapshot_ts": None})

    body = _client(redis_client).post("/score", json=_body()).json()
    assert body["already_processed"] is True
    assert body["state_updated"] is False


def test_scoring_an_event_older_than_the_state_is_logged(
    redis_client: Redis, caplog: pytest.LogCaptureFixture
) -> None:
    """A what-if about the past is out-of-distribution, not merely approximate.

    Events always arrive in time order on the stream, so a negative gap can only come
    from this endpoint. It is caught here rather than by changing a feature definition
    that is verified bit-identical against the live system.
    """
    import logging

    from feature_helpers import make_event

    store = RedisStore(redis_client)
    engine = FeatureEngine(store, directory({ACCOUNT: (HOME_CITY, MUMBAI, CREATED_AT)}))
    engine.process(make_event("T9999999", EVENT_TIME + timedelta(days=5)))

    client = _client(redis_client)
    with caplog.at_level(logging.WARNING):
        body = client.post("/score", json=_body(txn_id="OLD-001")).json()

    assert body["state_updated"] is False
    assert "older than the account's last processed event" in caplog.text


def test_a_current_event_logs_no_staleness_warning(
    redis_client: Redis, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    client = _client(redis_client)
    with caplog.at_level(logging.WARNING):
        client.post("/score", json=_body())
    assert "older than the account" not in caplog.text


def test_score_uses_the_snapshot_at_or_before_the_event(redis_client: Redis) -> None:
    older = to_epoch_seconds(EVENT_TIME - timedelta(days=1))
    newer = to_epoch_seconds(EVENT_TIME + timedelta(days=1))
    redis_client.zadd(PUBLISHED_KEY, {str(older): older, str(newer): newer})
    redis_client.hset(snapshot_key(older, ACCOUNT), mapping={"graph_degree": "6"})

    model = _Model()
    body = _client(redis_client, model=model).post("/score", json=_body()).json()

    assert body["graph_snapshot_ts"].startswith("2026-03-13")
    assert model.seen[0]["graph_degree"].iloc[0] == 6.0


def test_score_rejects_a_label_with_422(redis_client: Redis) -> None:
    """PLAN P7.1's explicit requirement."""
    response = _client(redis_client).post("/score", json=_body(is_fraud=1))
    assert response.status_code == 422
    assert "is_fraud" in response.text


def test_score_rejects_a_bad_amount_with_422(redis_client: Redis) -> None:
    assert _client(redis_client).post("/score", json=_body(amount=-10)).status_code == 422


def test_score_without_a_model_is_503(redis_client: Redis) -> None:
    response = _client(redis_client, ready=False).post("/score", json=_body())
    assert response.status_code == 503


# --- /alerts --------------------------------------------------------------------------


def _push_alert(redis_client: Redis, txn_id: str, decision: str = "REVIEW") -> None:
    redis_client.lpush(
        ALERTS_KEY,
        json.dumps(
            {
                "txn_id": txn_id,
                "account_id": ACCOUNT,
                "merchant_id": "M000001",
                "amount": 100.0,
                "event_time": EVENT_TIME.isoformat(),
                "risk": 0.9,
                "decision": decision,
                "reason": "device shared",
            }
        ),
    )


def test_alerts_come_back_newest_first(redis_client: Redis) -> None:
    for index in range(3):
        _push_alert(redis_client, f"T{index:07d}")

    body = _client(redis_client).get("/alerts").json()
    assert [alert["txn_id"] for alert in body] == ["T0000002", "T0000001", "T0000000"]


def test_alerts_respect_the_limit(redis_client: Redis) -> None:
    for index in range(10):
        _push_alert(redis_client, f"T{index:07d}")
    assert len(_client(redis_client).get("/alerts?limit=4").json()) == 4


def test_alerts_filter_by_decision(redis_client: Redis) -> None:
    _push_alert(redis_client, "T0000001", "REVIEW")
    _push_alert(redis_client, "T0000002", "HOLD")

    body = _client(redis_client).get("/alerts?decision=HOLD").json()
    assert [alert["txn_id"] for alert in body] == ["T0000002"]


@pytest.mark.parametrize("query", ["?limit=0", "?limit=99999", "?decision=BLOCK"])
def test_bad_alert_parameters_are_422(redis_client: Redis, query: str) -> None:
    assert _client(redis_client).get(f"/alerts{query}").status_code == 422


def test_alerts_are_empty_before_anything_fires(redis_client: Redis) -> None:
    assert _client(redis_client).get("/alerts").json() == []


# --- /transactions/{id} ----------------------------------------------------------------


def test_an_unknown_transaction_is_404(redis_client: Redis) -> None:
    """PLAN P7.1's explicit requirement."""
    response = _client(redis_client).get("/transactions/does-not-exist")
    assert response.status_code == 404
    assert "does-not-exist" in response.json()["detail"]


def test_a_scored_transaction_is_returned(redis_client: Redis, tmp_path: Path) -> None:
    from fraud.stream.sink import ParquetSink

    sink = ParquetSink("scorer-1", tmp_path)
    sink.append(
        [
            {
                "txn_id": "T0000001",
                "event_time": EVENT_TIME,
                "risk": 0.9,
                "decision": "REVIEW",
                "scored_ts": 1,
                "reasons": json.dumps([{"feature": "acct_cnt_5m", "text": "8 payments"}]),
            }
        ],
        ["1-0"],
    )
    sink.flush()

    import fraud.api.routes.transactions as module

    original = module.duck.connect
    module.duck.connect = lambda *a, **k: original(tmp_path)  # type: ignore[assignment]
    try:
        body = _client(redis_client).get("/transactions/T0000001").json()
    finally:
        module.duck.connect = original  # type: ignore[assignment]

    assert body["txn_id"] == "T0000001"
    assert body["decision"] == "REVIEW"
    # The stored JSON string is parsed, so callers do not each have to.
    assert body["reasons"][0]["feature"] == "acct_cnt_5m"


# --- /metrics ---------------------------------------------------------------------------


def test_metrics_reports_the_planned_fields(redis_client: Redis) -> None:
    redis_client.hset(METRICS_KEY, mapping={"events": 500, "alerts": 12, "flushes": 3})
    body = _client(redis_client).get("/metrics").json()

    assert body["events"] == 500
    assert body["alerts"] == 12
    assert body["flushes"] == 3
    assert body["pending"] == 0
    assert body["dlq_size"] == 0


def test_metrics_works_before_the_stream_exists(redis_client: Redis) -> None:
    """The dashboard starts before the scorer; it must not 500."""
    body = _client(redis_client).get("/metrics").json()
    assert body["events"] == 0
    assert body["consumer_lag"] is None


def test_metrics_reports_latency_percentiles(redis_client: Redis) -> None:
    redis_client.rpush("metrics:latency_ms", *[str(value) for value in range(1, 101)])
    body = _client(redis_client).get("/metrics").json()
    assert body["latency_p50_ms"] == pytest.approx(50.0, abs=1.5)
    assert body["latency_p95_ms"] == pytest.approx(95.0, abs=1.5)


# --- errors and docs --------------------------------------------------------------------


def test_redis_being_down_is_503_not_500(redis_client: Redis) -> None:
    """A dependency outage is not this service being broken (PLAN §10)."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    state = AppState(settings=Settings.from_env(), redis=redis_client)
    state.model = _Model()

    class _Broken:
        def __getattr__(self, name: str) -> Any:
            def explode(*args: Any, **kwargs: Any) -> Any:
                raise RedisConnectionError("connection refused")

            return explode

    state.redis = _Broken()  # type: ignore[assignment]
    client = TestClient(create_app(state), raise_server_exceptions=False)

    response = client.get("/metrics")
    assert response.status_code == 503
    assert "redis unavailable" in response.json()["detail"]


def test_openapi_is_served(redis_client: Redis) -> None:
    body = _client(redis_client).get("/openapi.json").json()
    assert "/score" in body["paths"]
    assert "/health" in body["paths"]
    assert set(body["paths"]) >= {"/health", "/score", "/alerts", "/metrics"}


def test_docs_are_served(redis_client: Redis) -> None:
    assert _client(redis_client).get("/docs").status_code == 200


def test_the_score_response_documents_that_it_writes_nothing(redis_client: Redis) -> None:
    """state_updated is pinned in the schema, not merely in prose."""
    schema = _client(redis_client).get("/openapi.json").json()["components"]["schemas"]
    assert schema["ScoreResponse"]["properties"]["state_updated"]["const"] is False
