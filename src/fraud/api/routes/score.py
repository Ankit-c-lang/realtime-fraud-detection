"""POST /score: what-if scoring that never writes state (PLAN §10)."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from fraud.api.deps import Engine, Model, RedisClient
from fraud.graph.refresh_live import (
    fetch_graph_features,
    from_epoch_seconds,
    published_snapshots,
    resolve_snapshot,
)
from fraud.schemas import EventRequest, Reason, ScoreResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scoring"])


@router.post("/score", response_model=ScoreResponse)
def score(request: EventRequest, engine: Engine, model: Model, redis: RedisClient) -> ScoreResponse:
    """Score a transaction without changing anything.

    **Why read-only.** The stream scorer is the single writer of account state. If this
    endpoint committed, a transaction scored here and then delivered on the stream would
    be folded into the account twice — velocity counters would climb, the running mean
    would drift, and nothing downstream could tell the duplicate from a real second
    payment. So the engine runs with ``commit=False``.

    **Already-processed events.** If the stream has already scored this ``txn_id``, the
    stored feature record is returned instead of recomputing. Recomputing would use
    today's state rather than the state as it stood then, which is a different — and
    wrong — answer to the question that was actually asked.

    **A caveat worth stating, and now detected.** Scoring an event *older* than the
    account's last processed transaction is not a point-in-time result: the state has
    moved on. It also produces a negative ``secs_since_last``, a value the model has
    never seen in training, so the score is confident-looking and meaningless. The
    engine is shared with the offline replay, where events always arrive in time order,
    so this can only happen here — which is why it is caught here rather than by
    changing a feature definition that is verified bit-identical against the live system.
    """
    import pandas as pd

    event = request.to_event()

    committed = engine.store.committed(event.txn_id)
    already = committed is not None
    if already:
        features = dict(committed)
        snapshot_ts = features.get("graph_snapshot_ts")
    else:
        # commit=False: computes against current state and discards the update.
        features = engine.process(event, commit=False)
        snapshot_ts = resolve_snapshot(event.event_time, published_snapshots(redis))
        features["graph_snapshot_ts"] = snapshot_ts

    _warn_if_stale(event.txn_id, features)

    graph = fetch_graph_features(redis, [(snapshot_ts, event.account_id)])[0]
    frame = pd.DataFrame([{**features, **graph}])
    scored = model.score_batch(frame)

    return ScoreResponse(
        txn_id=event.txn_id,
        fraud_probability=float(scored.p_xgb[0]),
        anomaly_percentile=float(scored.anomaly_pct[0]),
        risk_score=float(scored.risk[0]),
        decision=str(scored.decision[0]),  # type: ignore[arg-type]
        reasons=[Reason(**reason.to_dict()) for reason in scored.reasons[0]],
        model_version=scored.model_version,
        graph_snapshot_ts=from_epoch_seconds(snapshot_ts) if snapshot_ts is not None else None,
        already_processed=already,
    )


def _warn_if_stale(txn_id: str, features: dict[str, object]) -> None:
    """Log when a what-if event predates the account's state (PLAN §10).

    A negative gap is the tell. It cannot arise in the stream, where events arrive in
    time order, so it always means someone asked about the past — and the answer is
    out-of-distribution rather than merely approximate.
    """
    gap = features.get("secs_since_last")
    if isinstance(gap, int | float) and gap < 0:
        logger.warning(
            "%s is older than the account's last processed event (secs_since_last=%.0f). "
            "The score is not point-in-time and the model is seeing a value it was never "
            "trained on (PLAN §10).",
            txn_id,
            gap,
        )
