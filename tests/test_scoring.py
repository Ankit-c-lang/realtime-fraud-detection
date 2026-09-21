"""`RiskModel`, the decision policy and the versioned model folder (PLAN §7.7, §8).

`RiskModel` is the only path from features to a decision, so these tests stand in for the
scorer, the API and the re-score check at once. The compatibility tests matter most: an
artifact that no longer matches the code produces plausible numbers rather than obvious
ones, and a score that is quietly wrong is far more expensive than a process that refuses
to start.

Everything here builds its own tiny model folder. Nothing depends on `models/v1`, which
is gitignored and does not exist in CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from fraud.features.spec import FEATURE_NAMES, FEATURE_SPEC_VERSION, category_levels
from fraud.modeling import artifacts, blend, iforest
from fraud.modeling.decisions import (
    ALLOW,
    DECISIONS,
    HOLD,
    REVIEW,
    Thresholds,
    choose,
    decide,
    is_alert,
)
from fraud.scoring.risk_model import ModelCompatibilityError, RiskModel

WEIGHT = 0.5


def _frame(n: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    fraud = rng.random(n) < 0.15

    frame = pd.DataFrame(
        {name: rng.normal(0.0, 1.0, n) for name in FEATURE_NAMES if name != "merchant_category"}
    )
    frame["acct_cnt_5m"] = np.where(fraud, rng.integers(6, 20, n), rng.integers(0, 2, n)).astype(
        float
    )
    frame["geo_speed_kmh"] = np.where(fraud, rng.uniform(900, 5000, n), rng.uniform(0, 50, n))
    levels = pd.CategoricalDtype(categories=list(category_levels()))
    frame["merchant_category"] = pd.Series(rng.choice(list(category_levels()), n)).astype(levels)

    frame = frame[list(FEATURE_NAMES)]
    frame["is_fraud"] = fraud.astype(int)
    return frame


def _build(directory: Path, *, version: str = "v1", **overrides: Any) -> tuple[Path, pd.DataFrame]:
    """A complete, loadable model folder built from tiny data."""
    train = _frame(600, 0)  # above iforest max_samples, so sklearn does not warn
    valid = _frame(300, 1)
    matrix = train[list(FEATURE_NAMES)]

    model = xgb.XGBClassifier(
        n_estimators=40, max_depth=3, enable_categorical=True, tree_method="hist", random_state=42
    )
    model.fit(matrix, train["is_fraud"])
    detector = iforest.fit(train, n_jobs=1)

    p_xgb = model.predict_proba(valid[list(FEATURE_NAMES)])[:, 1]
    risk = blend.blend(p_xgb, detector.percentile(valid), WEIGHT)
    thresholds = choose(valid["is_fraud"].to_numpy(), risk)

    # A real Evaluation, carrying the policy tier, exactly as artifacts.build() does.
    # A stubbed dict here would let the metadata-consistency test pass vacuously.
    from fraud.modeling.metrics import evaluate_at

    evaluation = evaluate_at(
        valid["is_fraud"].to_numpy(), risk, thresholds.review, hold=thresholds.hold
    )
    metadata = artifacts.build_metadata(
        version,
        xgb_params={"max_depth": 3},
        best_iteration=39,
        detector=detector,
        blend_weight=WEIGHT,
        thresholds=thresholds,
        valid_metrics=evaluation.to_dict(),
    )
    metadata.update(overrides)

    folder = artifacts.save(
        directory / version, booster=model.get_booster(), detector=detector, metadata=metadata
    )
    artifacts.write_current(directory, version)
    return folder, valid


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, pd.DataFrame]:
    directory = tmp_path_factory.mktemp("models")
    folder, valid = _build(directory)
    return folder.parent, valid


# --- decision policy (PLAN §7.7) ---------------------------------------------------


def _thresholds(review: float = 0.5, hold: float | None = 0.9) -> Thresholds:
    return Thresholds(
        review=review,
        hold=hold,
        review_budget=0.02,
        review_precision=0.9,
        review_recall=0.8,
        review_alerts=10,
        review_alert_rate=0.01,
    )


def test_decision_tiers() -> None:
    risk = np.array([0.1, 0.5, 0.7, 0.9, 0.99])
    assert list(decide(risk, _thresholds())) == [ALLOW, REVIEW, REVIEW, HOLD, HOLD]


def test_thresholds_are_inclusive() -> None:
    """A row exactly at the threshold counted as an alert when it was chosen."""
    assert decide(np.array([0.5]), _thresholds())[0] == REVIEW
    assert decide(np.array([0.9]), _thresholds())[0] == HOLD


def test_disabled_hold_never_fires() -> None:
    risk = np.array([0.1, 0.5, 0.99])
    assert list(decide(risk, _thresholds(hold=None))) == [ALLOW, REVIEW, REVIEW]


def test_alerts_are_review_union_hold() -> None:
    decisions = decide(np.array([0.1, 0.5, 0.95]), _thresholds())
    assert list(is_alert(decisions)) == [False, True, True]


def test_decisions_are_the_three_allowed_verbs() -> None:
    assert DECISIONS == ("ALLOW", "REVIEW", "HOLD")
    assert "BLOCK" not in DECISIONS


def test_review_threshold_respects_the_budget() -> None:
    """The constraint that makes the operating point honest (§7.7)."""
    rng = np.random.default_rng(0)
    y = (rng.random(5000) < 0.2).astype(int)
    risk = np.clip(y * 0.5 + rng.random(5000) * 0.5, 0, 1)

    thresholds = choose(y, risk, budget=0.02)
    assert thresholds.review_alert_rate <= 0.02
    assert (risk >= thresholds.review).mean() <= 0.02


def test_hold_is_disabled_when_nothing_is_precise_enough() -> None:
    """A pure-noise score has no high-precision tier, and the tier must not be invented."""
    rng = np.random.default_rng(1)
    y = (rng.random(4000) < 0.2).astype(int)
    thresholds = choose(y, rng.random(4000))
    assert thresholds.hold is None
    assert thresholds.hold_enabled is False


def test_hold_is_disabled_when_it_would_not_separate_from_review() -> None:
    """The case that actually occurs on this dataset: every alert clears the bar.

    Freezing every flagged card is a worse answer than having no HOLD tier, so the
    collapse is recorded rather than tuned around.
    """
    y = np.zeros(4000, dtype=int)
    y[:400] = 1
    # Perfectly separable: precision at the budget threshold is 1.0, well over the bar.
    risk = np.where(y == 1, 0.99, 0.01)

    thresholds = choose(y, risk)
    assert thresholds.hold is None
    assert thresholds.hold_degenerate is True
    # The measurement survives even though the tier is off, so metadata can tell the
    # two disabled cases apart.
    assert thresholds.hold_precision == pytest.approx(1.0)


def test_reported_hold_block_follows_the_policy() -> None:
    """A disabled tier must not be advertised as a live one.

    ``evaluate_at`` runs its own HOLD search unless a policy is handed to it, so an
    artifact could report ``thresholds.hold = null`` beside ``metrics.hold.threshold =
    0.51`` — telling a reader the system freezes cards when it does not. The shipped
    paths pass the policy; this pins that.
    """
    y = np.repeat([1, 0], [400, 3600])
    risk = np.repeat([0.99, 0.01], [400, 3600])
    thresholds = choose(y, risk)
    assert thresholds.hold is None

    from fraud.modeling.metrics import evaluate_at

    searched = evaluate_at(y, risk, thresholds.review).to_dict()["hold"]
    assert searched["enabled"] is True and searched["source"] == "search"

    reported = evaluate_at(y, risk, thresholds.review, hold=thresholds.hold).to_dict()["hold"]
    assert reported == {
        "enabled": False,
        "threshold": None,
        "precision": None,
        "alerts": 0,
        "source": "policy",
    }


def test_an_enabled_hold_policy_is_measured_not_researched() -> None:
    """When a policy tier exists, its numbers are the policy's, not a fresh search's."""
    from fraud.modeling.metrics import evaluate_at

    y = np.repeat([1, 0], [400, 3600])
    risk = np.concatenate([np.linspace(0.6, 0.99, 400), np.linspace(0.0, 0.59, 3600)])

    block = evaluate_at(y, risk, 0.6, hold=0.9).to_dict()["hold"]
    assert block["enabled"] is True
    assert block["threshold"] == 0.9
    assert block["source"] == "policy"
    assert block["alerts"] == int((risk >= 0.9).sum())


def test_artifact_metadata_agrees_with_its_own_policy(built: tuple[Path, pd.DataFrame]) -> None:
    """The whole point of the fix, asserted on a real saved folder."""
    metadata = json.loads((built[0] / "v1" / "metadata.json").read_text())
    policy = metadata["thresholds"]
    reported = metadata["metrics"]["valid"]["hold"]

    assert reported["source"] == "policy"
    assert reported["enabled"] == policy["hold_enabled"]
    assert reported["threshold"] == policy["hold"]


def test_a_disabled_policy_never_reaches_a_decision(built: tuple[Path, pd.DataFrame]) -> None:
    """End to end: if the artifact disables HOLD, scoring cannot emit one."""
    directory, valid = built
    model = RiskModel.load(model_dir=directory)
    scored = model.score_batch(valid, explain=False)

    if model.bundle.thresholds.hold is None:
        assert HOLD not in set(scored.decision)
    assert set(scored.decision) <= set(DECISIONS)


def test_thresholds_serialise_the_disabled_reason() -> None:
    payload = choose(np.repeat([1, 0], [400, 3600]), np.repeat([0.99, 0.01], [400, 3600])).to_dict()
    assert payload["hold"] is None
    assert payload["hold_enabled"] is False
    assert payload["hold_degenerate"] is True
    assert payload["chosen_on"] == "valid"


# --- artifacts (PLAN §8) -----------------------------------------------------------


def test_folder_has_every_planned_file(built: tuple[Path, pd.DataFrame]) -> None:
    directory = built[0] / "v1"
    for name in (
        "xgb.ubj",
        "iforest.joblib",
        "anomaly_quantiles.npy",
        "feature_stats.json",
        "reason_codes.yaml",
        "metadata.json",
    ):
        assert (directory / name).is_file(), name
    assert (built[0] / "CURRENT").read_text().strip() == "v1"


def test_metadata_has_every_planned_field(built: tuple[Path, pd.DataFrame]) -> None:
    metadata = json.loads((built[0] / "v1" / "metadata.json").read_text())
    for field in (
        "model_version",
        "created_at",
        "git_commit",
        "plan_version",
        "feature_spec_version",
        "feature_names",
        "categorical_levels",
        "sim_config_sha256",
        "generator_version",
        "splits",
        "graph_config",
        "xgb_params",
        "best_iteration",
        "iforest_params",
        "blend_weight",
        "thresholds",
        "review_budget",
        "metrics",
        "library_versions",
    ):
        assert field in metadata, field
    assert metadata["feature_names"] == list(FEATURE_NAMES)


def test_test_metrics_start_empty(built: tuple[Path, pd.DataFrame]) -> None:
    """evaluate_test.py fills this, exactly once (§7.11)."""
    metadata = json.loads((built[0] / "v1" / "metadata.json").read_text())
    assert metadata["metrics"]["test"] is None
    assert metadata["metrics"]["valid"] is not None


def test_templates_are_copied_into_the_artifact(built: tuple[Path, pd.DataFrame]) -> None:
    """An artifact must explain itself with the wording it was built with."""
    copied = (built[0] / "v1" / "reason_codes.yaml").read_text()
    assert copied == Path("configs/reason_codes.yaml").read_text()


def test_round_trip_preserves_the_detector(built: tuple[Path, pd.DataFrame]) -> None:
    directory, valid = built
    bundle = artifacts.load(directory / "v1")
    assert bundle.blend_weight == WEIGHT
    assert bundle.detector.feature_names == iforest.NUMERIC_FEATURE_NAMES
    assert len(bundle.detector.quantiles) == 1001
    assert np.isfinite(bundle.detector.percentile(valid)).all()


# --- RiskModel (PLAN §8, §16) ------------------------------------------------------


def test_load_uses_current(built: tuple[Path, pd.DataFrame]) -> None:
    model = RiskModel.load(model_dir=built[0])
    assert model.model_version == "v1"
    assert model.blend_weight == WEIGHT


def test_load_prefers_the_model_version_env(
    built: tuple[Path, pd.DataFrame], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _build(tmp_path, version="v9")
    (tmp_path / "CURRENT").write_text("v1\n")  # CURRENT says v1, the env says v9
    monkeypatch.setenv("MODEL_VERSION", "v9")
    assert RiskModel.load(model_dir=tmp_path).model_version == "v9"


def test_missing_version_is_a_clear_error(built: tuple[Path, pd.DataFrame]) -> None:
    with pytest.raises(FileNotFoundError, match="make train"):
        RiskModel.load("v404", model_dir=built[0])


def test_missing_current_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="make train"):
        RiskModel.load(model_dir=tmp_path)


def test_scoring_is_deterministic(built: tuple[Path, pd.DataFrame]) -> None:
    """The re-score check in §9.6 compares to 1e-9, so this has to be exact."""
    directory, valid = built
    model = RiskModel.load(model_dir=directory)
    first, second = model.score_batch(valid), model.score_batch(valid)
    np.testing.assert_array_equal(first.risk, second.risk)
    np.testing.assert_array_equal(first.p_xgb, second.p_xgb)
    np.testing.assert_array_equal(first.decision, second.decision)


def test_risk_is_the_blend(built: tuple[Path, pd.DataFrame]) -> None:
    directory, valid = built
    model = RiskModel.load(model_dir=directory)
    scored = model.score_batch(valid)
    np.testing.assert_allclose(
        scored.risk, WEIGHT * scored.p_xgb + (1 - WEIGHT) * scored.anomaly_pct
    )


def test_probability_matches_the_booster(built: tuple[Path, pd.DataFrame]) -> None:
    """p_xgb comes from the margin, so it must equal a direct probability prediction."""
    directory, valid = built
    model = RiskModel.load(model_dir=directory)
    bundle = model.bundle

    matrix = model.model_input(valid)
    direct = bundle.booster.predict(xgb.DMatrix(matrix, enable_categorical=True))
    np.testing.assert_allclose(model.score_batch(valid, explain=False).p_xgb, direct, atol=1e-6)


def test_anomaly_percentile_is_bounded_and_monotone(built: tuple[Path, pd.DataFrame]) -> None:
    directory, valid = built
    model = RiskModel.load(model_dir=directory)
    scored = model.score_batch(valid, explain=False)
    raw = model.bundle.detector.raw(valid)

    assert scored.anomaly_pct.min() >= 0.0
    assert scored.anomaly_pct.max() <= 1.0
    assert np.array_equal(np.argsort(np.argsort(raw)), np.argsort(np.argsort(scored.anomaly_pct)))


def test_decisions_follow_the_stored_thresholds(built: tuple[Path, pd.DataFrame]) -> None:
    directory, valid = built
    model = RiskModel.load(model_dir=directory)
    scored = model.score_batch(valid, explain=False)
    expected = decide(scored.risk, model.bundle.thresholds)
    np.testing.assert_array_equal(scored.decision, expected)


def test_only_alerts_are_explained(built: tuple[Path, pd.DataFrame]) -> None:
    """§7.10 explains REVIEW and HOLD rows; contributions are too costly for the rest."""
    directory, valid = built
    model = RiskModel.load(model_dir=directory)
    scored = model.score_batch(valid)

    for decision, row in zip(scored.decision, scored.reasons, strict=True):
        if decision == ALLOW:
            assert row == []
    assert any(row for row in scored.reasons)


def test_reasons_reach_the_scored_frame(built: tuple[Path, pd.DataFrame]) -> None:
    """The scored-row schema stores reasons as a JSON string (§9.2)."""
    directory, valid = built
    scored = RiskModel.load(model_dir=directory).score_batch(valid)
    frame = scored.to_frame()

    assert set(frame.columns) >= {"p_xgb", "anomaly_pct", "risk", "decision", "reasons"}
    assert (frame["model_version"] == "v1").all()
    assert (frame["feature_spec_version"] == FEATURE_SPEC_VERSION).all()
    alert = frame[frame["decision"] != ALLOW].iloc[0]
    assert isinstance(json.loads(alert["reasons"]), list)


def test_column_order_comes_from_the_artifact(built: tuple[Path, pd.DataFrame]) -> None:
    """A shuffled frame must score identically, not be silently misread (L8)."""
    directory, valid = built
    model = RiskModel.load(model_dir=directory)
    shuffled = valid[list(reversed(valid.columns))]
    np.testing.assert_array_equal(
        model.score_batch(valid, explain=False).risk,
        model.score_batch(shuffled, explain=False).risk,
    )


def test_missing_feature_is_refused(built: tuple[Path, pd.DataFrame]) -> None:
    directory, valid = built
    model = RiskModel.load(model_dir=directory)
    with pytest.raises(KeyError, match="feature columns missing"):
        model.score_batch(valid.drop(columns=["geo_speed_kmh"]))


# --- startup compatibility (PLAN §8 loading rules) ---------------------------------


def test_stale_feature_spec_refuses_to_load(tmp_path: Path) -> None:
    _build(tmp_path, feature_spec_version="fs0")
    with pytest.raises(ModelCompatibilityError, match="feature spec fs0"):
        RiskModel.load(model_dir=tmp_path)


def test_mismatched_library_refuses_to_load(tmp_path: Path) -> None:
    """Pickles are version-sensitive, so major.minor has to match."""
    versions = dict(artifacts.library_versions())
    versions["scikit-learn"] = "0.1.0"
    _build(tmp_path, library_versions=versions)
    with pytest.raises(ModelCompatibilityError, match="scikit-learn 0.1.0"):
        RiskModel.load(model_dir=tmp_path)


def test_patch_releases_are_tolerated(tmp_path: Path) -> None:
    """Pinning the patch version would make the artifact brittle for no safety gain."""
    versions = dict(artifacts.library_versions())
    major_minor = ".".join(versions["xgboost"].split(".")[:2])
    versions["xgboost"] = f"{major_minor}.999"
    _build(tmp_path, library_versions=versions)
    assert RiskModel.load(model_dir=tmp_path).model_version == "v1"


def test_checks_can_be_skipped_for_offline_inspection(tmp_path: Path) -> None:
    _build(tmp_path, feature_spec_version="fs0")
    assert RiskModel.load(model_dir=tmp_path, check=False).model_version == "v1"
