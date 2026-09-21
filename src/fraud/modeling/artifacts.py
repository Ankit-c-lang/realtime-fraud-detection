"""Versioned model folders instead of a registry (PLAN §8).

MLflow would be the reflex here and it would be the wrong call: there is one model and no
promotion workflow, so a registry service would be infrastructure with nothing to
register. A folder, a metadata file and startup compatibility checks are the honest
minimum, and they make the same guarantees that matter — you can tell exactly what a
scored row was scored by, and a mismatched artifact refuses to load rather than silently
producing nonsense.

The metadata is deliberately over-complete. It records the simulator config hash, the
split boundaries, the graph caps and the library versions, so a number in the README can
be traced all the way back to the data that produced it without trusting anyone's memory
(§0.4).

**Security note (§8).** ``iforest.joblib`` is a pickle, and unpickling executes code.
Only ever load a model folder you built yourself.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb

from fraud.config import CONFIG_DIR, PROJECT_ROOT, Settings, load_yaml
from fraud.features.spec import (
    FEATURE_NAMES,
    FEATURE_SPEC_VERSION,
    category_levels,
)
from fraud.modeling.decisions import Thresholds
from fraud.modeling.iforest import AnomalyDetector

logger = logging.getLogger(__name__)

PLAN_VERSION: Final[str] = "v1 (2026-09-16)"
CURRENT_FILE: Final[str] = "CURRENT"

BOOSTER_FILE: Final[str] = "xgb.ubj"
FOREST_FILE: Final[str] = "iforest.joblib"
QUANTILES_FILE: Final[str] = "anomaly_quantiles.npy"
STATS_FILE: Final[str] = "feature_stats.json"
REASONS_FILE: Final[str] = "reason_codes.yaml"
METADATA_FILE: Final[str] = "metadata.json"


@dataclass(frozen=True, slots=True)
class Artifacts:
    """Everything needed to score, loaded from one folder."""

    booster: xgb.Booster
    detector: AnomalyDetector
    blend_weight: float
    thresholds: Thresholds
    metadata: dict[str, Any]
    directory: Path

    @property
    def model_version(self) -> str:
        return str(self.metadata["model_version"])


def library_versions() -> dict[str, str]:
    """What the pickles and the native booster were written by (§8 loading rules)."""
    import duckdb
    import networkx
    import sklearn

    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "xgboost": xgb.__version__,
        "scikit-learn": sklearn.__version__,
        "networkx": networkx.__version__,
        "duckdb": duckdb.__version__,
    }


def _data_provenance(settings: Settings) -> dict[str, Any]:
    """The simulator hash and generator version, straight from the run manifest."""
    manifest = settings.raw_dir / "manifest.json"
    if not manifest.is_file():  # pragma: no cover - only when data has not been built
        logger.warning("no %s; the artifact will not record its data provenance", manifest)
        return {"sim_config_sha256": None, "generator_version": None}
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        "sim_config_sha256": payload.get("config_sha256"),
        "generator_version": payload.get("generator_version"),
    }


def build_metadata(
    version: str,
    *,
    xgb_params: dict[str, Any],
    best_iteration: int,
    detector: AnomalyDetector,
    blend_weight: float,
    thresholds: Thresholds,
    valid_metrics: dict[str, Any],
    settings: Settings | None = None,
) -> dict[str, Any]:
    """The §8 metadata block, with `metrics.test` left for evaluate_test.py to fill."""
    from fraud.modeling.experiments import git_commit

    settings = settings or Settings.from_env()
    return {
        "model_version": version,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": git_commit(),
        "plan_version": PLAN_VERSION,
        "feature_spec_version": FEATURE_SPEC_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "categorical_levels": list(category_levels()),
        **_data_provenance(settings),
        "splits": {
            name: {"start": str(spec["start"]), "end": str(spec["end"])}
            for name, spec in load_yaml("splits")["splits"].items()
        },
        "graph_config": {
            key: load_yaml("graph")[key]
            for key in ("device_cap", "ip_cap", "min_accounts", "lookback_days", "label_delay_days")
        },
        "xgb_params": xgb_params,
        "best_iteration": best_iteration,
        "iforest_params": detector.metadata(),
        "blend_weight": blend_weight,
        "thresholds": thresholds.to_dict(),
        "review_budget": thresholds.review_budget,
        # test stays null until `make evaluate-test` fills it, exactly once (§7.11).
        "metrics": {"valid": valid_metrics, "test": None},
        "library_versions": library_versions(),
    }


def save(
    directory: Path,
    *,
    booster: xgb.Booster,
    detector: AnomalyDetector,
    metadata: dict[str, Any],
) -> Path:
    """Write the six files of §8 into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)

    booster.save_model(str(directory / BOOSTER_FILE))
    joblib.dump(detector.forest, directory / FOREST_FILE)
    np.save(directory / QUANTILES_FILE, detector.quantiles)

    # Medians and spreads travel as JSON rather than inside the pickle: the anomaly
    # reason has to stay readable and diffable even if the joblib file ever goes stale.
    (directory / STATS_FILE).write_text(
        json.dumps(
            {
                "feature_names": list(detector.feature_names),
                "medians": [float(v) for v in detector.medians],
                "spreads": [float(v) for v in detector.spreads],
                "train_rows": detector.train_rows,
                "note": (
                    "spreads are the train IQR, falling back to the standard deviation "
                    "where the IQR is zero (PLAN §7.5)"
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    # The templates are copied in, not referenced: an artifact has to explain itself with
    # the wording it was built with, even after configs/ moves on.
    shutil.copyfile(CONFIG_DIR / REASONS_FILE, directory / REASONS_FILE)

    (directory / METADATA_FILE).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    logger.info("wrote %s", directory)
    return directory


def write_current(model_dir: Path, version: str) -> Path:
    """Point ``models/CURRENT`` at a version. There is no hot reload (§8)."""
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / CURRENT_FILE
    path.write_text(version + "\n", encoding="utf-8")
    logger.info("models/CURRENT -> %s", version)
    return path


def load(directory: Path) -> Artifacts:
    """Read a model folder back.

    Compatibility checks live in ``RiskModel.load`` rather than here, so offline tools
    can still open an old folder to inspect it while the serving path refuses it.
    """
    metadata = json.loads((directory / METADATA_FILE).read_text(encoding="utf-8"))

    booster = xgb.Booster()
    booster.load_model(str(directory / BOOSTER_FILE))

    stats = json.loads((directory / STATS_FILE).read_text(encoding="utf-8"))
    detector = AnomalyDetector(
        forest=joblib.load(directory / FOREST_FILE),
        quantiles=np.load(directory / QUANTILES_FILE),
        medians=np.asarray(stats["medians"], dtype=float),
        spreads=np.asarray(stats["spreads"], dtype=float),
        feature_names=tuple(stats["feature_names"]),
        train_rows=int(stats["train_rows"]),
        feature_spec_version=str(metadata["feature_spec_version"]),
    )

    stored = metadata["thresholds"]
    valid = stored.get("valid", {})
    thresholds = Thresholds(
        review=float(stored["review"]),
        hold=None if stored["hold"] is None else float(stored["hold"]),
        review_budget=float(stored["review_budget"]),
        review_precision=float(valid.get("review_precision", 0.0)),
        review_recall=float(valid.get("review_recall", 0.0)),
        review_alerts=int(valid.get("review_alerts", 0)),
        review_alert_rate=float(valid.get("review_alert_rate", 0.0)),
        hold_precision=valid.get("hold_precision"),
        hold_alerts=int(valid.get("hold_alerts", 0) or 0),
        hold_degenerate=bool(stored.get("hold_degenerate", False)),
    )

    return Artifacts(
        booster=booster,
        detector=detector,
        blend_weight=float(metadata["blend_weight"]),
        thresholds=thresholds,
        metadata=metadata,
        directory=directory,
    )


def build(version: str, *, n_jobs: int = -1) -> tuple[Path, dict[str, Any]]:
    """Fit the final model (E4) and write ``models/<version>`` (PLAN §8, prompt P5.2).

    ``w*`` is read from ``reports/experiments/lopo.json`` rather than recomputed, so the
    weight in the artifact is the one a recorded experiment chose. Run ``make lopo``
    first if it is missing.
    """
    from fraud.modeling import blend, iforest, train_xgb
    from fraud.modeling.decisions import choose
    from fraud.modeling.experiments import EXPERIMENTS_DIR, read
    from fraud.modeling.metrics import evaluate_at
    from fraud.modeling.splits import load_many, target
    from fraud.scoring import reasons

    settings = Settings.from_env()
    lopo_path = EXPERIMENTS_DIR / "lopo.json"
    if not lopo_path.is_file():
        raise FileNotFoundError(
            f"{lopo_path} is missing: the blend weight comes from a recorded "
            "leave-one-pattern-out run (PLAN §7.6). Run `make lopo` first."
        )
    weight = float(json.loads(lopo_path.read_text(encoding="utf-8"))["best_weight"])
    params = read("E2")["details"]["params"]
    logger.info("blend weight w* = %g, E2 parameter set %s", weight, params)

    frames = load_many(["train", "early_stop", "valid"], warm=True)
    model = train_xgb.fit(frames["train"], frames["early_stop"], params, warm=True, n_jobs=n_jobs)
    detector = iforest.fit(frames["train"], n_jobs=n_jobs)

    valid = frames["valid"]
    # The probability is taken from the booster margin, exactly as RiskModel does at
    # serving time, rather than from predict_proba. The two agree to about 1e-7, which is
    # enough to move rows across the threshold and leave the metadata claiming metrics the
    # deployed artifact does not reproduce. _verify_round_trip below pins them together.
    margin = reasons.margins(model.get_booster(), train_xgb.as_model_input(valid, warm=True))
    p_xgb = 1.0 / (1.0 + np.exp(-margin))
    anomaly = detector.percentile(valid)
    risk = blend.blend(p_xgb, anomaly, weight)

    y_valid = target(valid).to_numpy()
    thresholds = choose(y_valid, risk)
    evaluation = evaluate_at(
        y_valid,
        risk,
        thresholds.review,
        amounts=valid["amount"].to_numpy() if "amount" in valid.columns else None,
        fraud_type=valid["fraud_type"].to_numpy(),
        # The policy tier, not a fresh search: the metadata has to describe the model
        # that ships, and this one disables HOLD (§7.7).
        hold=thresholds.hold,
    )

    metadata = build_metadata(
        version,
        xgb_params=params,
        best_iteration=int(getattr(model, "best_iteration", 0) or 0),
        detector=detector,
        blend_weight=weight,
        thresholds=thresholds,
        valid_metrics=evaluation.to_dict(),
        settings=settings,
    )

    directory = save(
        settings.model_dir / version,
        booster=model.get_booster(),
        detector=detector,
        metadata=metadata,
    )
    _verify_round_trip(directory, valid, risk)
    write_current(settings.model_dir, version)
    return directory, metadata


def _verify_round_trip(directory: Path, valid: pd.DataFrame, risk: np.ndarray) -> None:
    """The saved artifact must reproduce the scores its own metadata reports.

    Serialisation is where a model quietly becomes a different model: a category level
    reordered, a quantile array truncated, a booster written at the wrong iteration. None
    of those raise; they just shift the scores a little. Checking here means the failure
    surfaces at build time rather than as an unexplained gap between the results table and
    production months later.
    """
    from fraud.scoring.risk_model import RiskModel

    reloaded = RiskModel.load(directory.name, model_dir=directory.parent).score_batch(
        valid, explain=False
    )
    worst = float(np.abs(reloaded.risk - risk).max())
    if worst > 1e-9:
        raise AssertionError(
            f"the saved artifact does not reproduce its own scores: max |diff| = {worst:.3e}. "
            "The metadata metrics would not describe the deployed model (PLAN §8)."
        )
    logger.info("round trip verified: max |risk diff| = %.1e", worst)


def main(argv: list[str] | None = None) -> int:
    """``make train V=v1`` (PLAN §8)."""
    import argparse

    parser = argparse.ArgumentParser(description="Build a versioned model folder (PLAN §8).")
    parser.add_argument("--version", default="v1", help="model version folder name")
    parser.add_argument("--jobs", type=int, default=-1, help="XGBoost and forest n_jobs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory, metadata = build(args.version, n_jobs=args.jobs)

    valid = metadata["metrics"]["valid"]
    logger.info(
        "%s: valid PR-AUC %.4f  P %.3f  R %.3f  alerts %.2f%%  HOLD %s",
        directory.relative_to(PROJECT_ROOT),
        valid["pr_auc"],
        valid["precision"],
        valid["recall"],
        valid["alert_rate"] * 100,
        "disabled"
        if metadata["thresholds"]["hold"] is None
        else f"{valid['hold']['precision']:.3f} precision",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
