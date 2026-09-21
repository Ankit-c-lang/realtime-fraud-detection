"""The dashboard's data layer (PLAN §11, §13).

Streamlit rendering is not tested here — the fragments are a thin shell — but everything
they display is. The scorecard maths in particular is worth pinning: it is the one panel
that joins labels to scored output, and a wrong denominator there would quietly overstate
how well the live system is doing, on the screen people look at during a demo.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from fraud.config import PROJECT_ROOT
from fraud.storage import duck


def _module() -> Any:
    """dashboard/ is not an installed package, so load app.py by path."""
    spec = importlib.util.spec_from_file_location(
        "dashboard_app", PROJECT_ROOT / "dashboard" / "app.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


app = _module()


def _scored(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    from fraud.stream.sink import ParquetSink

    sink = ParquetSink("scorer-1", tmp_path)
    sink.append(rows, [f"{index}-0" for index in range(len(rows))])
    sink.flush()
    return tmp_path


def _row(
    index: int, *, decision: str = "ALLOW", risk: float = 0.1, **overrides: Any
) -> dict[str, Any]:
    row = {
        "txn_id": f"T{index:07d}",
        "event_time": pd.Timestamp("2026-03-14 10:00:00") + pd.Timedelta(minutes=index * 20),
        "account_id": f"A{index % 3:07d}",
        "merchant_id": f"M{index % 2:06d}",
        "amount": 100.0 + index,
        "risk": risk,
        "decision": decision,
        "p_xgb": 0.5,
        "anomaly_pct": 0.5,
        "scored_ts": 1_700_000_000_000 + index,
    }
    row.update(overrides)
    return row


def _labels(tmp_path: Path, rows: list[tuple[str, int, str]]) -> Path:
    path = tmp_path / "labels.parquet"
    pd.DataFrame([{"txn_id": t, "is_fraud": f, "fraud_type": k} for t, f, k in rows]).to_parquet(
        path
    )
    return path


# --- the stale-empty view (the bug this design exists to avoid) ----------------------


def test_the_view_picks_up_files_written_after_the_connection_opened(
    tmp_path: Path,
) -> None:
    """A dashboard started before the scorer must not show zeros forever.

    A view is defined once. Without redefining it, the empty placeholder installed at
    startup would outlive the whole replay and look like a broken scorer.
    """
    connection = duck.connect(tmp_path)
    try:
        assert duck.define_scored_view(connection, tmp_path) is False
        assert connection.execute("SELECT count(*) FROM scored").fetchone()[0] == 0

        _scored(tmp_path, [_row(0), _row(1)])

        assert duck.define_scored_view(connection, tmp_path) is True
        assert connection.execute("SELECT count(*) FROM scored").fetchone()[0] == 2
    finally:
        connection.close()


# --- hourly panel ---------------------------------------------------------------------


def test_alert_rate_is_bucketed_by_simulated_hour(tmp_path: Path) -> None:
    """Event time, not arrival time: at 3,600x the two are completely different."""
    _scored(
        tmp_path,
        [
            _row(0, decision="REVIEW", event_time=pd.Timestamp("2026-03-14 10:05")),
            _row(1, decision="ALLOW", event_time=pd.Timestamp("2026-03-14 10:45")),
            _row(2, decision="ALLOW", event_time=pd.Timestamp("2026-03-14 11:05")),
        ],
    )
    connection = duck.connect(tmp_path)
    try:
        rows = app.alert_rate_by_hour(connection)
    finally:
        connection.close()

    assert len(rows) == 2
    assert rows.iloc[0]["alert_rate"] == pytest.approx(0.5)
    assert rows.iloc[1]["alert_rate"] == pytest.approx(0.0)


def test_holds_are_counted_separately(tmp_path: Path) -> None:
    _scored(tmp_path, [_row(0, decision="HOLD"), _row(1, decision="REVIEW")])
    connection = duck.connect(tmp_path)
    try:
        rows = app.alert_rate_by_hour(connection)
    finally:
        connection.close()

    assert int(rows["alerts"].sum()) == 2  # HOLD is an alert too (§7.7)
    assert int(rows["holds"].sum()) == 1


# --- the scorecard --------------------------------------------------------------------


def test_scorecard_recall_is_per_pattern(tmp_path: Path) -> None:
    _scored(
        tmp_path,
        [
            _row(0, decision="REVIEW"),
            _row(1, decision="ALLOW"),
            _row(2, decision="REVIEW"),
            _row(3, decision="ALLOW"),
        ],
    )
    labels = _labels(
        tmp_path,
        [
            ("T0000000", 1, "RING"),
            ("T0000001", 1, "RING"),
            ("T0000002", 1, "VELOCITY"),
            ("T0000003", 0, "NONE"),
        ],
    )

    connection = duck.connect(tmp_path)
    try:
        rows = app.scorecard(connection, labels)
    finally:
        connection.close()

    by_pattern = rows.set_index("pattern")
    assert by_pattern.loc["RING", "recall"] == pytest.approx(0.5)
    assert by_pattern.loc["VELOCITY", "recall"] == pytest.approx(1.0)


def test_scorecard_totals_use_the_right_denominators(tmp_path: Path) -> None:
    """Precision over all alerts, recall over all labelled fraud.

    A false positive belongs to no pattern — it is a legitimate transaction — so
    precision is only meaningful overall, and the NONE row is where the false positives
    live.
    """
    rows = pd.DataFrame(
        [
            {"pattern": "NONE", "labelled": 900, "caught": 10},  # 10 false positives
            {"pattern": "RING", "labelled": 60, "caught": 50},
            {"pattern": "ATO", "labelled": 40, "caught": 30},
        ]
    )
    totals = app.scorecard_totals(rows)

    assert totals["alerts"] == 90  # 80 true + 10 false
    assert totals["precision"] == pytest.approx(80 / 90)
    assert totals["recall"] == pytest.approx(80 / 100)
    assert totals["fraud"] == 100


def test_scorecard_totals_survive_an_empty_table() -> None:
    totals = app.scorecard_totals(pd.DataFrame(columns=["pattern", "labelled", "caught"]))
    assert totals == {"precision": 0.0, "recall": 0.0, "alerts": 0, "fraud": 0}


def test_scorecard_without_labels_is_empty_not_an_error(tmp_path: Path) -> None:
    _scored(tmp_path, [_row(0)])
    connection = duck.connect(tmp_path)
    try:
        assert app.scorecard(connection, tmp_path / "absent.parquet").empty
    finally:
        connection.close()


def test_the_caveat_names_what_the_scorer_cannot_see() -> None:
    """§11 requires this on screen, not in a comment."""
    assert "never sees" in app.SCORECARD_CAVEAT
    assert "labels" in app.SCORECARD_CAVEAT.lower()


# --- leaderboards ----------------------------------------------------------------------


def test_top_merchants_ranks_by_alerts(tmp_path: Path) -> None:
    _scored(
        tmp_path,
        [
            _row(0, decision="REVIEW", merchant_id="M000001"),
            _row(1, decision="REVIEW", merchant_id="M000001"),
            _row(2, decision="REVIEW", merchant_id="M000002"),
            _row(3, decision="ALLOW", merchant_id="M000003"),
        ],
    )
    connection = duck.connect(tmp_path)
    try:
        rows = app.top_merchants(connection)
    finally:
        connection.close()

    assert list(rows["merchant_id"]) == ["M000001", "M000002"]  # M000003 never alerted
    assert int(rows.iloc[0]["alerts"]) == 2


def test_top_accounts_reports_the_worst_risk(tmp_path: Path) -> None:
    _scored(
        tmp_path,
        [
            _row(0, decision="REVIEW", risk=0.7, account_id="A0000001"),
            _row(1, decision="REVIEW", risk=0.95, account_id="A0000001"),
        ],
    )
    connection = duck.connect(tmp_path)
    try:
        rows = app.top_accounts(connection)
    finally:
        connection.close()

    assert rows.iloc[0]["max_risk"] == pytest.approx(0.95)


def test_leaderboards_are_empty_before_any_alert(tmp_path: Path) -> None:
    _scored(tmp_path, [_row(0), _row(1)])
    connection = duck.connect(tmp_path)
    try:
        assert app.top_merchants(connection).empty
        assert app.top_accounts(connection).empty
    finally:
        connection.close()


# --- API helpers ------------------------------------------------------------------------


def test_api_failure_returns_none_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """A panel showing a warning beats a page that will not load."""
    import httpx

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("no API")

    monkeypatch.setattr(httpx, "get", explode)
    assert app.api_get("/metrics") is None


def test_api_get_returns_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class _Response:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, int]:
            return {"events": 7}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response())
    assert app.api_get("/metrics") == {"events": 7}


@pytest.mark.parametrize(
    ("age", "expected"),
    [(None, "never"), (2.0, "live"), (45.0, "45s ago"), (600.0, "stale (10m)")],
)
def test_heartbeat_label(age: float | None, expected: str) -> None:
    assert app.heartbeat_label(age) == expected


def test_refresh_intervals_match_the_plan() -> None:
    """§11's table: 2 s live counters, 5 s hourly, 10 s aggregates."""
    assert (app.FAST_REFRESH, app.MEDIUM_REFRESH, app.SLOW_REFRESH) == (2, 5, 10)


# --- the app actually renders -----------------------------------------------------------


def test_the_app_renders_without_exceptions() -> None:
    """Runs the real script through Streamlit's own harness.

    The unit tests above cover the queries, but nothing in them would catch a misused
    Streamlit API — a removed keyword, a bad column count — because those only fail when
    the script executes. This runs it for real. It passes with no API and no scored
    output too: both paths degrade to a message rather than an exception, which is what
    the panels are written to do.
    """
    from streamlit.testing.v1 import AppTest

    app_test = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=90)
    app_test.run()

    assert [str(exc.value) for exc in app_test.exception] == []
    assert app_test.title[0].value.startswith("Realtime fraud detection")


def test_every_planned_panel_is_present() -> None:
    """§11 lists six panels; a silently dropped one would be easy to miss.

    The set must be the same with or without scored output — CI has none. A panel that
    disappears when its source is empty reads as a broken page, so every panel renders
    its heading and says it is waiting.
    """
    from streamlit.testing.v1 import AppTest

    app_test = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=90)
    app_test.run()

    headings = {heading.value for heading in app_test.subheader}
    assert headings == {
        "Alert rate per simulated hour",
        "Replay scorecard",
        "Recent alerts",
        "Top merchants by alerts",
        "Top accounts by alerts",
        "Alert detail",
    }


def test_the_scorecard_caveat_is_rendered_on_screen() -> None:
    """§11 requires the label on the panel, not in a docstring."""
    from streamlit.testing.v1 import AppTest

    app_test = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=90)
    app_test.run()

    assert any("never sees" in caption.value for caption in app_test.caption)
