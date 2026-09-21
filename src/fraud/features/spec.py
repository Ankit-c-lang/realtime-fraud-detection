"""Feature identity, order and defaults (PLAN §5.1 rule 6, §5.2).

This module is the ONLY definition of what the features are called and what order they
come in. Nothing else may hard-code a feature name or reorder a column: a model trained
on one order and served with another fails silently, producing plausible nonsense.

``FEATURE_SPEC_VERSION`` is bumped whenever any definition changes. It is written into
the replay output, the state checkpoint and every model's metadata, and checked at
startup, so a model can never be served against state built for a different spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fraud.config import load_yaml

FEATURE_SPEC_VERSION: Final[str] = "fs1"

# A. Context (stateless) · B. Account velocity · C. Behavioural deviation
# D. Novelty and location · E. Entity fan-out. PLAN §5.2 items 1-30.
HOT_FEATURE_NAMES: Final[tuple[str, ...]] = (
    "log_amount",
    "hour_of_day",
    "is_night",
    "is_online",
    "is_international",
    "merchant_category",
    "acct_cnt_5m",
    "acct_cnt_1h",
    "acct_cnt_24h",
    "acct_amt_24h",
    "acct_merchants_1h",
    "acct_declines_1h",
    "acct_small_1h",
    "secs_since_last",
    "amount_zscore",
    "amount_to_mean",
    "hour_unusualness",
    "account_age_days",
    "acct_history_cnt",
    "new_device",
    "acct_devices_30d",
    "new_merchant",
    "new_city",
    "km_from_home",
    "geo_speed_kmh",
    "dev_accts_1h",
    "dev_accts_30d",
    "ip_accts_1h",
    "ip_accts_30d",
    "mer_accts_30d",
)

# F. Graph snapshot, attached by the point-in-time join (PLAN §6). Items 31-36.
WARM_FEATURE_NAMES: Final[tuple[str, ...]] = (
    "graph_degree",
    "graph_clustering",
    "community_size",
    "community_shared_devices",
    "community_young_share",
    "ppr_risk",
)

FEATURE_NAMES: Final[tuple[str, ...]] = HOT_FEATURE_NAMES + WARM_FEATURE_NAMES

# The one non-numeric feature. Isolation Forest uses the other 35 (PLAN §5.2).
CATEGORICAL_FEATURES: Final[tuple[str, ...]] = ("merchant_category",)
NUMERIC_FEATURE_NAMES: Final[tuple[str, ...]] = tuple(
    name for name in FEATURE_NAMES if name not in CATEGORICAL_FEATURES
)

# Identifier-like columns must never reach the model (leakage rule L8, PLAN §7.1).
FORBIDDEN_FEATURES: Final[frozenset[str]] = frozenset(
    {"txn_id", "account_id", "merchant_id", "device_id", "ip", "attack_id", "ring_id"}
)


def category_levels() -> tuple[str, ...]:
    """Category order from configs/categories.yaml, which fixes the integer codes (L7)."""
    return tuple(entry["name"] for entry in load_yaml("categories")["categories"])


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    """Windows, thresholds and caps, loaded from configs/features.yaml."""

    short_seconds: int
    hour_seconds: int
    day_seconds: int
    month_seconds: int
    small_amount: float
    secs_since_last_default: int
    amount_to_mean_default: float
    geo_speed_default: float
    zscore_min_prior: int
    zscore_std_floor_ratio: float
    zscore_std_floor_abs: float
    zscore_clip_low: float
    zscore_clip_high: float
    amount_to_mean_clip: float
    speed_cap_kmh: float
    min_elapsed_seconds: int
    hour_bucket_hours: int
    hour_bucket_count: int
    night_start: int
    night_end: int
    device_retention_days: int
    # Online backend only (PLAN §3.6). Not part of any feature definition.
    redis_feature_ttl_seconds: int
    redis_entity_retention_seconds: int
    redis_trim_every: int

    @classmethod
    def load(cls) -> FeatureConfig:
        raw = load_yaml("features")
        redis_cfg = raw["redis"]
        windows = raw["windows"]
        defaults = raw["defaults"]
        zscore = raw["amount_zscore"]
        geo = raw["geo"]
        night_start, night_end = raw["night_hours"]

        return cls(
            short_seconds=int(windows["short_seconds"]),
            hour_seconds=int(windows["hour_seconds"]),
            day_seconds=int(windows["day_seconds"]),
            month_seconds=int(windows["month_seconds"]),
            small_amount=float(raw["small_amount"]),
            secs_since_last_default=int(defaults["secs_since_last"]),
            amount_to_mean_default=float(defaults["amount_to_mean"]),
            geo_speed_default=float(defaults["geo_speed_kmh"]),
            zscore_min_prior=int(zscore["min_prior_approved"]),
            zscore_std_floor_ratio=float(zscore["std_floor_ratio"]),
            zscore_std_floor_abs=float(zscore["std_floor_abs"]),
            zscore_clip_low=float(zscore["clip_low"]),
            zscore_clip_high=float(zscore["clip_high"]),
            amount_to_mean_clip=float(raw["amount_to_mean_clip"]),
            speed_cap_kmh=float(geo["speed_cap_kmh"]),
            min_elapsed_seconds=int(geo["min_elapsed_seconds"]),
            hour_bucket_hours=int(raw["hour_bucket_hours"]),
            hour_bucket_count=int(raw["hour_bucket_count"]),
            night_start=int(night_start),
            night_end=int(night_end),
            device_retention_days=int(raw["device_retention_days"]),
            redis_feature_ttl_seconds=int(redis_cfg["feature_ttl_seconds"]),
            redis_entity_retention_seconds=int(redis_cfg["entity_retention_seconds"]),
            redis_trim_every=int(redis_cfg["trim_every"]),
        )
