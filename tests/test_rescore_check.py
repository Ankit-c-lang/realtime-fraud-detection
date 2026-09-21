"""The re-score check itself (PLAN §9.6 item 3, §13).

A correctness check is only worth running if it fails when it should. These tests feed it
output that has been deliberately corrupted — a nudged score, a changed decision, a drifted
feature, a diverged graph snapshot — and assert it notices. A check that always passes is
worse than no check, because it is quoted as evidence.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from fraud.config import PROJECT_ROOT
from fraud.features.spec import HOT_FEATURE_NAMES


def _module() -> Any:
    """scripts/ is not an installed package, so load it by path."""
    spec = importlib.util.spec_from_file_location(
        "rescore_check", PROJECT_ROOT / "scripts" / "rescore_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: @dataclass resolves its own module through sys.modules,
    # and a module that is not there fails with an opaque AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _module()


class _Scored:
    def __init__(self, frame: pd.DataFrame) -> None:
        import numpy as np

        self.p_xgb = frame["p_xgb"].to_numpy()
        self.risk = frame["risk"].to_numpy()
        self.decision = frame["decision"].to_numpy()
        self.anomaly_pct = np.zeros(len(frame))


class _Model:
    """Returns exactly what the live rows claim, so only corruption can fail."""

    def score_batch(self, frame: pd.DataFrame, *, explain: bool = True) -> _Scored:
        return _Scored(frame)


def _live(rows: int = 5) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "txn_id": [f"T{i:07d}" for i in range(rows)],
            "event_time": pd.date_range("2026-03-14", periods=rows, freq="h"),
            "p_xgb": [0.1 * i for i in range(rows)],
            "risk": [0.05 * i for i in range(rows)],
            "decision": ["ALLOW"] * rows,
            "graph_snapshot_ts": [None] * rows,
        }
    )
    for name in HOT_FEATURE_NAMES:
        frame[name] = "grocery" if name == "merchant_category" else 1.0
    return frame


def _offline(live: pd.DataFrame) -> pd.DataFrame:
    return live[["txn_id", *HOT_FEATURE_NAMES]].copy()


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fraud.scoring import risk_model

    monkeypatch.setattr(risk_model.RiskModel, "load", classmethod(lambda cls, *a, **k: _Model()))
    return check


# --- re-score (§9.6 item 3a) ----------------------------------------------------------


def test_matching_output_passes(patched: Any) -> None:
    report = check.CheckReport()
    result = check.check_rescore(_live(), report)
    assert result.passed
    assert result.worst == 0.0


def test_a_nudged_score_fails(patched: Any) -> None:
    """1e-9 is the tolerance; anything above it means the paths diverged."""
    live = _live()
    live.loc[0, "risk"] = live.loc[0, "risk"] + 1e-6

    class _Wrong(_Model):
        def score_batch(self, frame: pd.DataFrame, *, explain: bool = True) -> _Scored:
            restored = frame.copy()
            restored.loc[0, "risk"] = restored.loc[0, "risk"] - 1e-6
            return _Scored(restored)

    from fraud.scoring import risk_model

    original = risk_model.RiskModel.load
    risk_model.RiskModel.load = classmethod(lambda cls, *a, **k: _Wrong())  # type: ignore[assignment]
    try:
        report = check.CheckReport()
        result = check.check_rescore(live, report)
    finally:
        risk_model.RiskModel.load = original  # type: ignore[assignment]

    assert not result.passed
    assert "risk" in result.detail


def test_a_changed_decision_fails(patched: Any) -> None:
    live = _live()

    class _Flipped(_Model):
        def score_batch(self, frame: pd.DataFrame, *, explain: bool = True) -> _Scored:
            restored = frame.copy()
            restored.loc[0, "decision"] = "REVIEW"
            return _Scored(restored)

    from fraud.scoring import risk_model

    original = risk_model.RiskModel.load
    risk_model.RiskModel.load = classmethod(lambda cls, *a, **k: _Flipped())  # type: ignore[assignment]
    try:
        result = check.check_rescore(live, check.CheckReport())
    finally:
        risk_model.RiskModel.load = original  # type: ignore[assignment]

    assert not result.passed
    assert "decision" in result.detail


def test_the_tolerance_is_the_planned_one() -> None:
    assert check.TOLERANCE == 1e-9


# --- features (§9.6 item 3b) ----------------------------------------------------------


def test_identical_features_pass(tmp_path: Path) -> None:
    live = _live()
    path = tmp_path / "hot_features.parquet"
    _offline(live).to_parquet(path)

    result = check.check_features(live, check.CheckReport(), features_path=path)
    assert result.passed


def test_a_drifted_feature_fails(tmp_path: Path) -> None:
    """The failure mode this check exists for: Redis and in-memory state diverging."""
    live = _live()
    offline = _offline(live)
    offline.loc[2, "acct_cnt_5m"] = 99.0
    path = tmp_path / "hot_features.parquet"
    offline.to_parquet(path)

    result = check.check_features(live, check.CheckReport(), features_path=path)
    assert not result.passed
    assert "acct_cnt_5m" in result.detail


def test_a_drifted_categorical_feature_fails(tmp_path: Path) -> None:
    live = _live()
    offline = _offline(live)
    offline.loc[1, "merchant_category"] = "travel"
    path = tmp_path / "hot_features.parquet"
    offline.to_parquet(path)

    result = check.check_features(live, check.CheckReport(), features_path=path)
    assert not result.passed
    assert "merchant_category" in result.detail


def test_only_hot_features_are_compared() -> None:
    """Warm features legitimately differ by staleness, which §6.5 reports separately."""
    import inspect

    source = inspect.getsource(check.check_features)
    assert "HOT_FEATURE_NAMES" in source
    assert "WARM_FEATURE_NAMES" not in source


# --- graph parity (§9.6 item 3c) ------------------------------------------------------


def _snapshot(directory: Path, stamp: str, degree: float = 3.0) -> None:
    target = directory / f"snapshot_ts={stamp}"
    target.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "snapshot_ts": [pd.Timestamp(stamp)],
            "account_id": ["A0000001"],
            "graph_degree": [degree],
            "graph_clustering": [0.5],
            "community_size": [3],
            "community_shared_devices": [1],
            "community_young_share": [0.2],
            "ppr_risk": [0.01],
        }
    ).to_parquet(target / "part.parquet", index=False)


def test_graph_parity_passes_when_snapshots_agree(tmp_path: Path) -> None:
    live, offline = tmp_path / "live", tmp_path / "offline"
    _snapshot(live, "2026-03-15")
    _snapshot(offline, "2026-03-15")

    result = check.check_graph_parity(check.CheckReport(), live_dir=live, offline_dir=offline)
    assert result.passed


def test_graph_parity_fails_on_a_diverged_value(tmp_path: Path) -> None:
    live, offline = tmp_path / "live", tmp_path / "offline"
    _snapshot(live, "2026-03-15", degree=3.0)
    _snapshot(offline, "2026-03-15", degree=4.0)

    result = check.check_graph_parity(check.CheckReport(), live_dir=live, offline_dir=offline)
    assert not result.passed
    assert "graph_degree" in result.detail


def test_graph_parity_fails_on_a_different_account_set(tmp_path: Path) -> None:
    live, offline = tmp_path / "live", tmp_path / "offline"
    _snapshot(live, "2026-03-15")
    target = offline / "snapshot_ts=2026-03-15"
    target.mkdir(parents=True)
    pd.DataFrame(
        {
            "snapshot_ts": [pd.Timestamp("2026-03-15")],
            "account_id": ["A0000009"],
            "graph_degree": [3.0],
            "graph_clustering": [0.5],
            "community_size": [3],
            "community_shared_devices": [1],
            "community_young_share": [0.2],
            "ppr_risk": [0.01],
        }
    ).to_parquet(target / "part.parquet", index=False)

    result = check.check_graph_parity(check.CheckReport(), live_dir=live, offline_dir=offline)
    assert not result.passed
    assert "account" in result.detail


def test_graph_parity_is_skipped_before_the_refresh_has_run(tmp_path: Path) -> None:
    """Skipped, and said so — not silently reported as a pass on nothing."""
    result = check.check_graph_parity(
        check.CheckReport(), live_dir=tmp_path / "absent", offline_dir=tmp_path
    )
    assert result.passed
    assert "skipped" in result.detail


# --- the report -----------------------------------------------------------------------


def test_the_report_fails_if_any_check_fails() -> None:
    report = check.CheckReport()
    report.add(check.Check("a", True, "fine"))
    assert report.passed
    report.add(check.Check("b", False, "broken"))
    assert not report.passed


def test_missing_output_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Run the replay first"):
        check.load_live(tmp_path)


def test_staleness_is_reported_not_asserted() -> None:
    """§6.5 treats staleness as a number to publish, not a failure."""
    live = _live()
    assert "no snapshot" in check.report_staleness(live)
