"""Streamlit dashboard over the live replay (PLAN §11).

Six panels, each refreshing on its own clock through ``st.fragment``, so the 2-second
system strip does not drag the 10-second DuckDB aggregates along with it. Streaming data
and Parquet aggregates have genuinely different costs, and refreshing everything at the
fastest panel's rate would re-scan the whole output four times a second for numbers that
change once a minute.

**Two sources, on purpose.** Live counters come from the API (`/metrics`, `/alerts`,
`/transactions/{id}`); anything aggregated over history comes from DuckDB reading the
scorer's Parquet directly. Putting the aggregates behind the API too would mean shipping
a query language through HTTP, and the dashboard is the only thing that wants them.

**The scorecard is evaluation data.** It joins the scored output with `labels.parquet`,
which the scorer never sees — the stream carries no labels (§3.5, L4). That has to be on
screen, not in a comment, because a panel showing precision next to live throughput
implies the running system knows which transactions are fraud, and it does not.

Data access lives in plain functions below so it can be tested without a Streamlit
runtime; the ``render_*`` fragments are a thin layer over them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

import duckdb
import httpx
import pandas as pd
import streamlit as st

from fraud.config import Settings
from fraud.storage import duck

API_URL: Final[str] = os.environ.get("API_URL", "http://localhost:8000")
REQUEST_TIMEOUT: Final[float] = 5.0

FAST_REFRESH: Final[int] = 2
MEDIUM_REFRESH: Final[int] = 5
SLOW_REFRESH: Final[int] = 10

SCORECARD_CAVEAT: Final[str] = (
    "**Evaluation data the scorer never sees.** The stream carries no labels "
    "(PLAN §3.5); this panel joins the scored output with `labels.parquet` after the "
    "fact, purely to show how the replay is going."
)


# --- data access (no Streamlit; tested directly) -------------------------------------


def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """One GET against the API, returning None rather than raising.

    A dashboard whose panels disappear when a dependency hiccups is worse than one that
    shows a stale number and says so, so failures are surfaced in the panel instead of
    taking the page down.
    """
    try:
        response = httpx.get(f"{API_URL}{path}", params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def alert_rate_by_hour(connection: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Alerts and HOLDs per simulated hour (PLAN §11).

    Simulated hour, not wall-clock: at 3,600x an hour of event time passes every second,
    and bucketing by arrival time would show the replay's pace rather than the data's
    shape.
    """
    return connection.execute(
        """
        SELECT date_trunc('hour', event_time)                                AS hour,
               count(*)                                                      AS events,
               sum(CASE WHEN decision <> 'ALLOW' THEN 1 ELSE 0 END)          AS alerts,
               sum(CASE WHEN decision = 'HOLD'  THEN 1 ELSE 0 END)           AS holds,
               avg(CASE WHEN decision <> 'ALLOW' THEN 1.0 ELSE 0.0 END)      AS alert_rate
        FROM scored GROUP BY 1 ORDER BY 1
        """
    ).fetch_df()


def scorecard(
    connection: duckdb.DuckDBPyConnection, labels_path: Path | None = None
) -> pd.DataFrame:
    """Precision and recall so far, overall and per pattern (PLAN §11).

    Recall is per pattern; precision is only meaningful overall, because a false positive
    belongs to no pattern — it is a legitimate transaction. Reporting a per-pattern
    precision would require inventing an attribution that does not exist.
    """
    path = labels_path or Settings.from_env().raw_dir / "labels.parquet"
    if not Path(path).is_file():
        return pd.DataFrame(columns=["pattern", "labelled", "caught", "recall"])

    joined = connection.execute(
        """
        SELECT l.fraud_type                                           AS pattern,
               count(*)                                               AS labelled,
               sum(CASE WHEN s.decision <> 'ALLOW' THEN 1 ELSE 0 END) AS caught
        FROM scored s
        JOIN read_parquet(?) l ON l.txn_id = s.txn_id
        GROUP BY 1 ORDER BY 1
        """,
        [str(path)],
    ).fetch_df()
    if joined.empty:
        return pd.DataFrame(columns=["pattern", "labelled", "caught", "recall"])

    joined["recall"] = joined["caught"] / joined["labelled"].replace(0, pd.NA)
    return joined


def scorecard_totals(rows: pd.DataFrame) -> dict[str, float]:
    """Overall precision, recall and alert count from the per-pattern table."""
    if rows.empty:
        return {"precision": 0.0, "recall": 0.0, "alerts": 0, "fraud": 0}

    fraud = rows[rows["pattern"] != "NONE"]
    legit = rows[rows["pattern"] == "NONE"]

    true_positives = int(fraud["caught"].sum())
    false_positives = int(legit["caught"].sum())
    alerts = true_positives + false_positives
    labelled_fraud = int(fraud["labelled"].sum())

    return {
        "precision": true_positives / alerts if alerts else 0.0,
        "recall": true_positives / labelled_fraud if labelled_fraud else 0.0,
        "alerts": alerts,
        "fraud": labelled_fraud,
    }


def top_merchants(connection: duckdb.DuckDBPyConnection, limit: int = 10) -> pd.DataFrame:
    return connection.execute(
        """
        SELECT merchant_id, count(*) AS txns, avg(risk) AS avg_risk,
               sum(CASE WHEN decision <> 'ALLOW' THEN 1 ELSE 0 END) AS alerts
        FROM scored GROUP BY 1 HAVING alerts > 0 ORDER BY alerts DESC, avg_risk DESC LIMIT ?
        """,
        [limit],
    ).fetch_df()


def top_accounts(connection: duckdb.DuckDBPyConnection, limit: int = 10) -> pd.DataFrame:
    return connection.execute(
        """
        SELECT account_id, count(*) AS txns, max(risk) AS max_risk,
               sum(CASE WHEN decision <> 'ALLOW' THEN 1 ELSE 0 END) AS alerts
        FROM scored GROUP BY 1 HAVING alerts > 0 ORDER BY alerts DESC, max_risk DESC LIMIT ?
        """,
        [limit],
    ).fetch_df()


def heartbeat_label(age_seconds: float | None) -> str:
    """The scorer's liveness, in words rather than a raw age."""
    if age_seconds is None:
        return "never"
    if age_seconds < 10:
        return "live"
    if age_seconds < 120:
        return f"{age_seconds:.0f}s ago"
    return f"stale ({age_seconds / 60:.0f}m)"


# --- Streamlit plumbing ---------------------------------------------------------------


@st.cache_resource
def _connection() -> duckdb.DuckDBPyConnection:
    """One DuckDB connection for the session (PLAN §11).

    In-memory, so there is no file and no lock for the scorer to contend with
    (invariant 7).
    """
    return duck.connect()


def connection() -> tuple[duckdb.DuckDBPyConnection, bool]:
    """The cached connection, with its view pointed at whatever exists *now*.

    The dashboard is usually started before the scorer has written anything, and a view
    is defined once. Without redefining it, every panel would read the empty placeholder
    for the rest of the replay and look like a broken scorer rather than an empty
    directory.
    """
    handle = _connection()
    return handle, duck.define_scored_view(handle)


def main() -> None:
    st.set_page_config(page_title="Fraud monitor", layout="wide")
    st.title("Realtime fraud detection — live replay")
    st.caption(
        f"Scored output from `data/scored/`, live counters from `{API_URL}`. "
        "Synthetic data (PLAN §4)."
    )

    render_system_strip()
    left, right = st.columns([2, 1])
    with left:
        render_hourly()
        render_scorecard()
        render_leaderboards()
    with right:
        render_alerts()
    render_alert_detail()


@st.fragment(run_every=FAST_REFRESH)
def render_system_strip() -> None:
    """Throughput, lag, latency, DLQ and heartbeat (PLAN §11)."""
    metrics = api_get("/metrics")
    if metrics is None:
        st.warning(f"API unreachable at {API_URL} — `make api` starts it.")
        return

    columns = st.columns(6)
    columns[0].metric("Events scored", f"{metrics['events']:,}")
    columns[1].metric("Events/s", f"{metrics['throughput_per_second']:.0f}")
    lag = metrics["consumer_lag"]
    columns[2].metric("Stream lag", "-" if lag is None else f"{lag:,}")
    p95 = metrics["latency_p95_ms"]
    columns[3].metric("p95 latency", "-" if p95 is None else f"{p95:.0f} ms")
    columns[4].metric("DLQ", f"{metrics['dlq_size']:,}")

    health = api_get("/health") or {}
    columns[5].metric("Scorer", heartbeat_label(health.get("scorer_heartbeat_age_seconds")))
    if metrics["dlq_size"]:
        st.error(f"{metrics['dlq_size']} message(s) in the dead-letter queue (PLAN §9.3).")


@st.fragment(run_every=MEDIUM_REFRESH)
def render_hourly() -> None:
    """Alert rate and HOLD count per simulated hour."""
    st.subheader("Alert rate per simulated hour")
    handle, ready = connection()
    if not ready:
        st.info("No scored output yet. Start the replay.")
        return

    rows = alert_rate_by_hour(handle)
    if rows.empty:
        st.info("No scored output yet.")
        return

    st.line_chart(rows.set_index("hour")[["alert_rate"]])
    st.caption(
        f"{int(rows['events'].sum()):,} events · {int(rows['alerts'].sum()):,} alerts · "
        f"{int(rows['holds'].sum()):,} HOLD"
    )


@st.fragment(run_every=SLOW_REFRESH)
def render_scorecard() -> None:
    """Precision and recall so far — explicitly evaluation data (PLAN §11)."""
    st.subheader("Replay scorecard")
    st.caption(SCORECARD_CAVEAT)

    handle, ready = connection()
    if not ready:
        st.info("No scored output yet.")
        return

    rows = scorecard(handle)
    if rows.empty:
        st.info("No labelled rows scored yet.")
        return

    totals = scorecard_totals(rows)
    columns = st.columns(4)
    columns[0].metric("Precision", f"{totals['precision']:.3f}")
    columns[1].metric("Recall", f"{totals['recall']:.3f}")
    columns[2].metric("Alerts", f"{totals['alerts']:,}")
    columns[3].metric("Fraud in window", f"{totals['fraud']:,}")

    patterns = rows[rows["pattern"] != "NONE"][["pattern", "labelled", "caught", "recall"]]
    st.dataframe(patterns, hide_index=True, width="stretch")


@st.fragment(run_every=FAST_REFRESH)
def render_alerts() -> None:
    """The most recent alerts, newest first."""
    st.subheader("Recent alerts")
    alerts = api_get("/alerts", {"limit": 25})
    if not alerts:
        st.info("No alerts yet.")
        return

    frame = pd.DataFrame(alerts)[
        [
            "event_time",
            "txn_id",
            "account_id",
            "merchant_id",
            "amount",
            "decision",
            "risk",
            "reason",
        ]
    ]
    st.dataframe(frame, hide_index=True, width="stretch")


@st.fragment(run_every=SLOW_REFRESH)
def render_leaderboards() -> None:
    """Where the alerts are concentrating.

    The headings render even with no data, like every other panel. A panel that vanishes
    when its source is empty reads as a broken page rather than an idle one, and the
    layout shifting under the operator once the replay starts is its own small lie.
    """
    handle, ready = connection()

    left, right = st.columns(2)
    with left:
        st.subheader("Top merchants by alerts")
        if ready:
            st.dataframe(top_merchants(handle), hide_index=True, width="stretch")
        else:
            st.info("No scored output yet.")
    with right:
        st.subheader("Top accounts by alerts")
        if ready:
            st.dataframe(top_accounts(handle), hide_index=True, width="stretch")
        else:
            st.info("No scored output yet.")


def render_alert_detail() -> None:
    """One transaction in full, on demand (PLAN §11).

    Not a fragment: it refreshes when the operator asks, because a detail panel that
    reloaded itself every two seconds while being read would be unusable.
    """
    st.subheader("Alert detail")
    txn_id = st.text_input("Transaction id", placeholder="T0458247")
    if not txn_id:
        return

    record = api_get(f"/transactions/{txn_id.strip()}")
    if record is None:
        st.warning(f"No scored transaction {txn_id!r}.")
        return

    columns = st.columns(4)
    columns[0].metric("p_xgb", f"{float(record.get('p_xgb', 0)):.4f}")
    columns[1].metric("anomaly pct", f"{float(record.get('anomaly_pct', 0)):.4f}")
    columns[2].metric("risk", f"{float(record.get('risk', 0)):.4f}")
    columns[3].metric("decision", str(record.get("decision", "-")))

    reasons = record.get("reasons") or []
    if reasons:
        st.dataframe(pd.DataFrame(reasons), hide_index=True, width="stretch")

    health = api_get("/health") or {}
    st.caption(
        f"model `{record.get('model_version')}` · spec `{record.get('feature_spec_version')}` · "
        f"snapshot `{record.get('graph_snapshot_ts')}` · scored by `{record.get('consumer')}` · "
        f"API model `{health.get('model_version')}`"
    )
    with st.expander("All stored fields"):
        st.json(record)


if __name__ == "__main__":
    main()
