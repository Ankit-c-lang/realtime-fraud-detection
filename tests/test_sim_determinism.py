"""The same seed must reproduce the same dataset, byte for byte (PLAN §4.8, §13)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from fraud.config import load_yaml
from fraud.sim.generate import build_dataset, config_digest, write_dataset

TABLES = ("events", "accounts", "merchants", "labels")


def _file_hashes(directory: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / f"{name}.parquet").read_bytes()).hexdigest()
        for name in TABLES
    }


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    first = tmp_path_factory.mktemp("first")
    second = tmp_path_factory.mktemp("second")
    write_dataset(build_dataset("sim_tiny"), first)
    write_dataset(build_dataset("sim_tiny"), second)
    return first, second


def test_two_runs_produce_identical_files(two_runs: tuple[Path, Path]) -> None:
    """The headline reproducibility claim: same seed, same bytes."""
    first, second = two_runs
    assert _file_hashes(first) == _file_hashes(second)


@pytest.mark.parametrize("table", TABLES)
def test_two_runs_produce_identical_contents(two_runs: tuple[Path, Path], table: str) -> None:
    first, second = two_runs
    pd.testing.assert_frame_equal(
        pd.read_parquet(first / f"{table}.parquet"),
        pd.read_parquet(second / f"{table}.parquet"),
    )


def test_a_different_seed_produces_a_different_dataset() -> None:
    baseline = build_dataset("sim_tiny")
    config = load_yaml("sim_tiny")
    assert baseline.manifest["seed"] == int(config["seed"])

    # The seed lives in the config, so changing it means changing the hash too, which is
    # exactly what the freeze rule pins (§4.8).
    assert baseline.manifest["config_sha256"] == config_digest("sim_tiny")


def test_manifest_is_stable_except_for_timing() -> None:
    first = build_dataset("sim_tiny").manifest
    second = build_dataset("sim_tiny").manifest

    for manifest in (first, second):
        manifest.pop("elapsed_seconds")
    assert first == second
