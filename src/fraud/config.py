"""Typed configuration loading: YAML files from configs/ plus environment overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "configs"

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return default if not raw else Path(raw).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings. Every field can be overridden by an environment variable."""

    redis_url: str
    data_dir: Path
    model_dir: Path
    model_version: str | None

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the environment, falling back to the repository defaults."""
        return cls(
            redis_url=os.environ.get("REDIS_URL") or DEFAULT_REDIS_URL,
            data_dir=_env_path("DATA_DIR", PROJECT_ROOT / "data"),
            model_dir=_env_path("MODEL_DIR", PROJECT_ROOT / "models"),
            model_version=os.environ.get("MODEL_VERSION") or None,
        )

    # Storage layout (PLAN §3.7). Nothing outside this class hard-codes these paths.

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def features_dir(self) -> Path:
        return self.data_dir / "features"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def graph_offline_dir(self) -> Path:
        return self.data_dir / "graph" / "offline"

    @property
    def graph_live_dir(self) -> Path:
        return self.data_dir / "graph" / "live"

    @property
    def scored_dir(self) -> Path:
        return self.data_dir / "scored"

    def resolve_model_version(self) -> str:
        """Return MODEL_VERSION if set, otherwise the version named by models/CURRENT (PLAN §8)."""
        if self.model_version:
            return self.model_version
        current = self.model_dir / "CURRENT"
        if not current.is_file():
            raise FileNotFoundError(
                f"No MODEL_VERSION set and {current} does not exist. "
                "Train a model first (make train V=v1)."
            )
        return current.read_text(encoding="utf-8").strip()

    def model_path(self, version: str | None = None) -> Path:
        """Path of one versioned model folder (PLAN §8)."""
        return self.model_dir / (version or self.resolve_model_version())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Call Settings.from_env() directly when the environment changes."""
    return Settings.from_env()


@cache
def load_yaml(name: str) -> dict[str, Any]:
    """Load configs/<name>.yaml as a dict. Results are cached per name."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in CONFIG_DIR.glob("*.yaml"))
        raise FileNotFoundError(f"{path} not found. Available configs: {available or 'none'}")
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise TypeError(
            f"{path} must contain a mapping at the top level, got {type(loaded).__name__}"
        )
    return loaded
