"""Skeleton smoke tests: the package imports and configuration behaves (PLAN §17, Phase 0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fraud.config import CONFIG_DIR, DEFAULT_REDIS_URL, PROJECT_ROOT, Settings, load_yaml


def test_package_imports() -> None:
    import fraud

    assert fraud.__doc__


def test_project_root_is_the_repository() -> None:
    assert (PROJECT_ROOT / "pyproject.toml").is_file()
    assert CONFIG_DIR == PROJECT_ROOT / "configs"


def test_defaults_when_nothing_is_set(clean_env: None) -> None:
    settings = Settings.from_env()
    assert settings.redis_url == DEFAULT_REDIS_URL
    assert settings.data_dir == PROJECT_ROOT / "data"
    assert settings.model_dir == PROJECT_ROOT / "models"
    assert settings.model_version is None


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MODEL_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("MODEL_VERSION", "v7")

    settings = Settings.from_env()

    assert settings.redis_url == "redis://redis:6379/0"
    assert settings.data_dir == tmp_path / "d"
    assert settings.model_dir == tmp_path / "m"
    assert settings.resolve_model_version() == "v7"
    assert settings.model_path() == tmp_path / "m" / "v7"


def test_storage_layout_matches_the_plan(clean_env: None) -> None:
    """The §3.7 layout lives in one place; nothing else may hard-code these paths."""
    settings = Settings.from_env()
    root = settings.data_dir

    assert settings.raw_dir == root / "raw"
    assert settings.features_dir == root / "features"
    assert settings.state_dir == root / "state"
    assert settings.graph_offline_dir == root / "graph" / "offline"
    assert settings.graph_live_dir == root / "graph" / "live"
    assert settings.scored_dir == root / "scored"


def test_model_version_falls_back_to_current_file(tmp_path: Path) -> None:
    (tmp_path / "CURRENT").write_text("v3\n", encoding="utf-8")
    settings = Settings(
        redis_url=DEFAULT_REDIS_URL,
        data_dir=tmp_path,
        model_dir=tmp_path,
        model_version=None,
    )
    assert settings.resolve_model_version() == "v3"


def test_missing_model_version_is_a_clear_error(tmp_path: Path, clean_env: None) -> None:
    settings = Settings(
        redis_url=DEFAULT_REDIS_URL,
        data_dir=tmp_path,
        model_dir=tmp_path,
        model_version=None,
    )
    with pytest.raises(FileNotFoundError, match="make train"):
        settings.resolve_model_version()


def test_missing_yaml_names_the_available_configs() -> None:
    with pytest.raises(FileNotFoundError, match="Available configs"):
        load_yaml("does_not_exist")
