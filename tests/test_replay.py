"""Offline replay and the state checkpoint (PLAN §5.5, §13)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
from feature_helpers import CREATED_AT, MUMBAI, at, directory, make_event

from fraud.features.engine import FeatureEngine
from fraud.features.replay import (
    OUTPUT_COLUMNS,
    ReplayResult,
    dump_state,
    load_state,
    replay,
    tag_splits,
    write_report,
)
from fraud.features.spec import FEATURE_SPEC_VERSION, HOT_FEATURE_NAMES
from fraud.features.store_memory import InMemoryStore
from fraud.schemas import EVENT_FIELDS, TransactionEvent

SPLIT_START = pd.Timestamp("2026-03-14")


def _events(count: int = 40, *, accounts: int = 4) -> pd.DataFrame:
    """A small stream spanning the train/test boundary, several accounts deep."""
    base = pd.Timestamp("2026-03-12")
    rows = []
    for index in range(count):
        moment = base + timedelta(hours=3 * index)
        rows.append(
            {
                "txn_id": f"T{index:07d}",
                "event_time": moment.to_pydatetime(),
                "account_id": f"A000000{index % accounts + 1}",
                "merchant_id": f"M00000{index % 3 + 1}",
                "merchant_category": "grocery",
                "amount": 100.0 + index,
                "channel": "POS",
                "device_id": f"D000000{index % 2 + 1}",
                "ip": "49.1.1.1",
                "lat": MUMBAI[0],
                "lon": MUMBAI[1],
                "city": "Mumbai",
                "country": "IN",
                "status": "APPROVED",
            }
        )
    return pd.DataFrame(rows)[list(EVENT_FIELDS)]


def _directory(count: int = 4):
    return directory({f"A000000{i}": ("Mumbai", MUMBAI, CREATED_AT) for i in range(1, count + 1)})


@pytest.fixture(scope="module")
def result() -> ReplayResult:
    return replay(_events(), _directory(), checkpoint_at=SPLIT_START)


# --- output shape (PLAN §5.5) -----------------------------------------------


def test_one_output_row_per_event(result: ReplayResult) -> None:
    assert len(result.features) == len(_events())


def test_output_columns_match_the_spec(result: ReplayResult) -> None:
    assert tuple(result.features.columns) == OUTPUT_COLUMNS
    assert set(HOT_FEATURE_NAMES) < set(result.features.columns)


def test_there_are_no_missing_values(result: ReplayResult) -> None:
    assert not result.features.isna().any().any()


def test_every_row_records_the_feature_spec(result: ReplayResult) -> None:
    """A model must never be served against features built by a different spec."""
    assert set(result.features["feature_spec_version"]) == {FEATURE_SPEC_VERSION}


def test_events_keep_their_order(result: ReplayResult) -> None:
    assert result.features["event_time"].is_monotonic_increasing


# --- split tagging (PLAN §4.7) ----------------------------------------------


def test_split_tags_come_from_the_config() -> None:
    times = pd.Series(
        pd.to_datetime(
            ["2026-01-02", "2026-01-20", "2026-02-25", "2026-03-05", "2026-03-20", "2026-05-01"]
        )
    )
    tagged = tag_splits(times)
    assert tagged["split"].tolist() == [
        "burn_in",
        "train",
        "early_stop",
        "valid",
        "test",
        "none",
    ]


def test_burn_in_is_flagged_separately() -> None:
    """Burn-in rows warm the state up and are excluded from training and metrics."""
    times = pd.Series(pd.to_datetime(["2026-01-02", "2026-01-20"]))
    tagged = tag_splits(times)
    assert tagged["in_burn_in"].tolist() == [1, 0]


def test_split_boundaries_are_half_open() -> None:
    """An event exactly at test_start belongs to test, not to valid."""
    times = pd.Series(pd.to_datetime(["2026-03-13 23:59:59", "2026-03-14 00:00:00"]))
    assert tag_splits(times)["split"].tolist() == ["valid", "test"]


# --- checkpoint (PLAN §5.5, §9.5) -------------------------------------------


def test_a_checkpoint_is_taken(result: ReplayResult) -> None:
    assert result.checkpoint is not None
    assert result.checkpoint["feature_spec_version"] == FEATURE_SPEC_VERSION
    assert result.checkpoint["accounts"]


def test_the_checkpoint_holds_nothing_from_the_test_window(result: ReplayResult) -> None:
    """Taken BEFORE the first test event, so it carries only what training knew.

    A checkpoint containing even one test event would leak the test window into the
    live replay's starting state.
    """
    assert result.checkpoint is not None
    boundary_ms = int((SPLIT_START - pd.Timestamp("1970-01-01")).total_seconds() * 1000)

    for state in result.checkpoint["accounts"].values():
        assert state["last_ts"] is None or state["last_ts"] < boundary_ms
        for entry in state["recent"]:
            assert entry[0] < boundary_ms


def test_the_checkpoint_restores_and_continues_identically() -> None:
    """PLAN §17 Phase 2: reload the checkpoint and keep producing the same features.

    This is what makes the live replay legitimate. The scorer starts from saved state
    rather than from nothing, and it has to behave exactly as an uninterrupted run would
    have. Any drift here would show up later as the Redis parity test failing for
    reasons that have nothing to do with the feature logic.
    """
    events = _events()
    accounts = _directory()
    uninterrupted = replay(events, accounts, checkpoint_at=SPLIT_START)

    before = events[events["event_time"] < SPLIT_START]
    after = events[events["event_time"] >= SPLIT_START]
    assert len(before) and len(after), "the fixture must span the boundary"

    # Run the prefix, dump, restore into a fresh store, then carry on.
    warm = InMemoryStore()
    FeatureEngine(warm, accounts)
    _process(warm, accounts, before)

    resumed_store = InMemoryStore()
    load_state(dump_state(warm), resumed_store)
    resumed = _process(resumed_store, accounts, after)

    expected = uninterrupted.features.tail(len(after))
    for column in HOT_FEATURE_NAMES:
        assert [row[column] for row in resumed] == expected[column].tolist(), column


def _process(store: InMemoryStore, accounts, events: pd.DataFrame) -> list[dict]:
    engine = FeatureEngine(store, accounts)
    return [engine.process(TransactionEvent.from_mapping(row)) for row in events.to_dict("records")]


def test_a_checkpoint_from_another_spec_is_refused() -> None:
    """Silently accepting it would serve a model against state it never saw."""
    store = InMemoryStore()
    with pytest.raises(ValueError, match="feature spec"):
        load_state({"feature_spec_version": "fs0", "accounts": {}, "entities": {}}, store)


# --- report ------------------------------------------------------------------


def test_report_lists_every_numeric_feature(result: ReplayResult, tmp_path: Path) -> None:
    path = tmp_path / "feature_report.md"
    write_report(result, path)
    text = path.read_text(encoding="utf-8")

    assert FEATURE_SPEC_VERSION in text
    for name in ("acct_cnt_5m", "amount_zscore", "geo_speed_kmh", "dev_accts_1h"):
        assert f"`{name}`" in text


# --- the engine contract still holds under replay ---------------------------


def test_replay_matches_processing_the_events_by_hand() -> None:
    """The replay must add nothing beyond ordering: same engine, same answers."""
    events = [
        make_event("T0000000", at(10, 0)),
        make_event("T0000001", at(10, 4)),
        make_event("T0000002", at(10, 30)),
    ]
    frame = pd.DataFrame(
        [{field: getattr(event, field) for field in EVENT_FIELDS} for event in events]
    )

    replayed = replay(frame, directory(), checkpoint_at=None).features
    assert replayed["acct_cnt_5m"].tolist() == [0, 1, 0]
    assert replayed["acct_history_cnt"].tolist() == [0, 1, 2]
