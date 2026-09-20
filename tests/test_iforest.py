"""The label-free anomaly detector (PLAN §7.5).

The property worth protecting here is negative: the forest must not know anything about
the labels. A detector that quietly benefits from them would look excellent in the
leave-one-pattern-out experiment and be worthless the first time a genuinely unlabelled
pattern arrives (leakage rule L6).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fraud.features.spec import NUMERIC_FEATURE_NAMES
from fraud.modeling import iforest


def _frame(n: int = 600, seed: int = 0) -> pd.DataFrame:
    """Ordinary rows: every numeric feature drawn from one tight distribution."""
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {name: rng.normal(0.0, 1.0, n) for name in NUMERIC_FEATURE_NAMES},
    )
    # A deliberately sparse count column, to exercise the zero-IQR fallback.
    frame["acct_declines_1h"] = np.where(rng.random(n) < 0.05, rng.integers(1, 4, n), 0)
    frame["is_fraud"] = (rng.random(n) < 0.1).astype(int)
    frame["fraud_type"] = np.where(frame["is_fraud"] == 1, "VELOCITY", "NONE")
    return frame


def _outlier(frame: pd.DataFrame) -> pd.DataFrame:
    """One row far outside the training cloud on every feature."""
    row = frame.iloc[[0]].copy()
    row[list(NUMERIC_FEATURE_NAMES)] = 40.0
    return row


def test_fit_ignores_labels() -> None:
    """Shuffling the labels must not move a single score (L6, §7.5)."""
    frame = _frame()
    shuffled = frame.copy()
    rng = np.random.default_rng(7)
    shuffled["is_fraud"] = rng.permutation(shuffled["is_fraud"].to_numpy())
    shuffled["fraud_type"] = rng.permutation(shuffled["fraud_type"].to_numpy())

    a = iforest.fit(frame, n_jobs=1)
    b = iforest.fit(shuffled, n_jobs=1)
    np.testing.assert_array_equal(a.raw(frame), b.raw(frame))


def test_fit_is_deterministic() -> None:
    """Same rows, same seed, same scores — the artifact has to be reproducible."""
    frame = _frame()
    np.testing.assert_array_equal(
        iforest.fit(frame, n_jobs=1).raw(frame), iforest.fit(frame, n_jobs=1).raw(frame)
    )


def test_uses_only_spec_features() -> None:
    """An extra numeric column must not become a model input (L8)."""
    frame = _frame()
    detector = iforest.fit(frame, n_jobs=1)

    polluted = frame.copy()
    polluted["account_id_numeric"] = np.arange(len(frame), dtype=float) * 1000.0
    np.testing.assert_array_equal(detector.raw(frame), detector.raw(polluted))


def test_missing_feature_raises() -> None:
    frame = _frame()
    with pytest.raises(KeyError, match="numeric feature columns missing"):
        iforest.numeric_matrix(frame.drop(columns=["log_amount"]))


def test_percentile_is_bounded_and_monotone() -> None:
    frame = _frame()
    detector = iforest.fit(frame, n_jobs=1)

    raw = detector.raw(frame)
    pct = detector.percentile(frame)
    assert pct.min() >= 0.0
    assert pct.max() <= 1.0
    # Percentiles must rank exactly as the raw scores do.
    assert np.array_equal(np.argsort(np.argsort(raw)), np.argsort(np.argsort(pct)))


def test_quantile_table_has_the_planned_size() -> None:
    detector = iforest.fit(_frame(), n_jobs=1)
    assert len(detector.quantiles) == 1001
    assert np.all(np.diff(detector.quantiles) >= 0)


def test_outlier_lands_at_the_top_percentile() -> None:
    """A row unlike anything in train clamps to 1.0 rather than extrapolating."""
    frame = _frame()
    detector = iforest.fit(frame, n_jobs=1)
    assert detector.percentile(_outlier(frame))[0] == pytest.approx(1.0)


def test_percentiles_are_spread_over_the_unit_interval() -> None:
    """The point of percentiles over min-max: train scores fill [0, 1] evenly."""
    frame = _frame()
    detector = iforest.fit(frame, n_jobs=1)
    pct = detector.percentile(frame)
    assert 0.4 < float(np.median(pct)) < 0.6
    assert float(np.mean(pct > 0.9)) > 0.05


def test_one_extreme_score_does_not_squash_the_rest() -> None:
    """The failure min-max scaling would cause, asserted directly (§7.5)."""
    frame = _frame()
    detector = iforest.fit(frame, n_jobs=1)
    with_outlier = pd.concat([frame, _outlier(frame)], ignore_index=True)

    pct = detector.percentile(with_outlier)
    assert pct[-1] == pytest.approx(1.0)
    # The ordinary rows keep their spread; min-max would have crushed them near zero.
    assert float(np.median(pct[:-1])) > 0.3


def test_top_features_name_the_deviating_column() -> None:
    frame = _frame()
    detector = iforest.fit(frame, n_jobs=1)

    row = frame.iloc[[1]].copy()
    row["geo_speed_kmh"] = 500.0
    row["acct_cnt_5m"] = 60.0
    assert set(detector.top_features(row, count=2)[0]) == {"geo_speed_kmh", "acct_cnt_5m"}


def test_zero_iqr_falls_back_to_standard_deviation() -> None:
    """A sparse count must not win every reason just because its IQR is zero."""
    frame = _frame()
    detector = iforest.fit(frame, n_jobs=1)
    index = list(NUMERIC_FEATURE_NAMES).index("acct_declines_1h")

    assert np.percentile(frame["acct_declines_1h"], 75) == np.percentile(
        frame["acct_declines_1h"], 25
    )
    assert detector.spreads[index] > 0.01
    assert np.isfinite(detector.deviations(frame)).all()


def test_constant_feature_never_produces_a_deviation() -> None:
    frame = _frame()
    frame["acct_declines_1h"] = 0.0
    detector = iforest.fit(frame, n_jobs=1)
    index = list(NUMERIC_FEATURE_NAMES).index("acct_declines_1h")
    assert detector.deviations(frame)[:, index].max() == 0.0


def test_metadata_records_the_label_free_fit() -> None:
    detector = iforest.fit(_frame(), n_jobs=1)
    meta = detector.metadata()
    assert meta["label_free"] is True
    assert meta["n_features"] == len(NUMERIC_FEATURE_NAMES) == 35
    assert meta["train_rows"] == 600
    assert meta["quantile_points"] == 1001
