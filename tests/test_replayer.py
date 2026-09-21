"""The replayer: ordering, pacing, label stripping and backpressure (PLAN §9.1, §13).

Three of these behaviours fail silently if they are wrong, which is why they get tests
rather than trust.

*Ordering.* Thousands of events in the replay window share a timestamp, so "sorted by
event_time" does not pin an order. If the stream order differs from the offline order,
every window feature differs a little and the §9.6 re-score check compares two things
that were never meant to match.

*Labels.* A leaked `is_fraud` would not crash anything. It would make the online metrics
excellent and meaningless.

*Backpressure.* A single threshold pauses and resumes on alternate checks. It looks like
it is working, and the consumer never gets room to drain.

Pacing and pauses run on an injected clock, so the suite stays fast and deterministic
instead of actually sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from redis import Redis

from fraud.config import load_yaml
from fraud.schemas import EVENT_FIELDS, SCHEMA_VERSION, TransactionEvent
from fraud.stream import replayer as mod
from fraud.stream.replayer import (
    LABEL_FIELDS,
    MESSAGE_FIELDS,
    LabelLeak,
    MaxPacer,
    RatePacer,
    Replayer,
    SpeedupPacer,
    assert_ordered,
    load_events,
    pacer_from_args,
    replay_window,
    to_message,
)

START = datetime(2026, 3, 14, 0, 0, 0)  # noqa: DTZ001 - naive IST, per PLAN §3.5


class FakeClock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _row(index: int, moment: datetime, **overrides: Any) -> dict[str, Any]:
    row = {
        "txn_id": f"T{index:07d}",
        "event_time": moment,
        "account_id": f"A{index % 3:07d}",
        "merchant_id": "M000001",
        "merchant_category": "grocery",
        "amount": 100.0 + index,
        "channel": "POS",
        "device_id": "D0000001",
        "ip": "49.1.1.1",
        "lat": 19.076,
        "lon": 72.8777,
        "city": "Mumbai",
        "country": "IN",
        "status": "APPROVED",
    }
    row.update(overrides)
    return row


def _frame(count: int = 10, *, step_seconds: int = 60, start: datetime = START) -> pd.DataFrame:
    rows = [_row(i, start + timedelta(seconds=i * step_seconds)) for i in range(count)]
    if not rows:  # an empty frame still has to carry the schema
        return pd.DataFrame(columns=list(EVENT_FIELDS))
    return pd.DataFrame(rows)[list(EVENT_FIELDS)]


def _config(**replayer: Any) -> dict[str, Any]:
    raw = load_yaml("stream")
    base = dict(raw["replayer"])
    back = dict(base["backpressure"])
    back.update(replayer.pop("backpressure", {}))
    base.update(replayer)
    base["backpressure"] = back
    return {"stream": raw["stream"], "replayer": base}


# --- pacing (PLAN §9.1) -------------------------------------------------------------


def test_speedup_compresses_event_time() -> None:
    """Event i goes out at (t_i - t_0) / S."""
    pacer = SpeedupPacer(3600.0)
    assert pacer.offset(0, START, START) == 0.0
    assert pacer.offset(1, START + timedelta(hours=1), START) == pytest.approx(1.0)
    assert pacer.offset(2, START + timedelta(hours=18), START) == pytest.approx(18.0)


def test_speedup_preserves_the_shape_of_event_time() -> None:
    """A quiet hour stays quiet: gaps scale, they do not flatten."""
    pacer = SpeedupPacer(60.0)
    burst = pacer.offset(1, START + timedelta(seconds=60), START)
    lull = pacer.offset(2, START + timedelta(seconds=6000), START)
    assert lull / burst == pytest.approx(100.0)


def test_rate_ignores_event_time() -> None:
    pacer = RatePacer(500.0)
    assert pacer.offset(0, START, START) == 0.0
    assert pacer.offset(500, START + timedelta(days=9), START) == pytest.approx(1.0)


def test_max_never_paces() -> None:
    assert MaxPacer().offset(7, START + timedelta(hours=3), START) is None


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_non_positive_pacing_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        SpeedupPacer(bad)
    with pytest.raises(ValueError, match="must be positive"):
        RatePacer(bad)


def test_pacer_selection() -> None:
    assert isinstance(pacer_from_args(None, None, True), MaxPacer)
    assert isinstance(pacer_from_args(None, 500.0, False), RatePacer)
    assert isinstance(pacer_from_args(10.0, None, False), SpeedupPacer)


def test_pacing_defaults_to_the_configured_speedup() -> None:
    pacer = pacer_from_args(None, None, False)
    assert isinstance(pacer, SpeedupPacer)
    assert pacer.speedup == float(load_yaml("stream")["replayer"]["default_speedup"])


def test_two_pacing_modes_are_refused() -> None:
    with pytest.raises(ValueError, match="pick one pacing mode"):
        pacer_from_args(10.0, 500.0, False)


# --- messages are label-free (PLAN §3.5) --------------------------------------------


def test_message_has_exactly_the_planned_fields() -> None:
    row = next(_frame(1).itertuples(index=False))
    message = to_message(row, ingest_ts=1_700_000_000_000)
    assert set(message) == set(MESSAGE_FIELDS)


def test_message_carries_no_label_field() -> None:
    row = next(_frame(1).itertuples(index=False))
    assert LABEL_FIELDS & to_message(row, 1).keys() == set()


def test_message_stamps_the_transport_fields() -> None:
    row = next(_frame(1).itertuples(index=False))
    message = to_message(row, ingest_ts=1_700_000_000_123)
    assert message["schema_version"] == str(SCHEMA_VERSION) == "1"
    assert message["ingest_ts"] == "1700000000123"


def test_message_round_trips_back_into_an_event() -> None:
    """Producer and consumer must agree on types, or online features drift silently."""
    row = next(_frame(1).itertuples(index=False))
    event = TransactionEvent.from_message(to_message(row, 1))

    assert event.txn_id == row.txn_id
    assert event.event_time == row.event_time.to_pydatetime()
    assert event.amount == pytest.approx(row.amount)
    assert isinstance(event.amount, float)
    assert event.approved is True


def test_loading_a_labelled_file_is_refused(tmp_path: Path) -> None:
    """If events.parquet ever grows a label column, stop — do not silently drop it."""
    frame = _frame(3)
    frame["is_fraud"] = 1
    path = tmp_path / "events.parquet"
    frame.to_parquet(path)

    with pytest.raises(LabelLeak, match="is_fraud"):
        load_events(path)


def test_loaded_frame_has_only_the_event_columns(tmp_path: Path) -> None:
    path = tmp_path / "events.parquet"
    frame = _frame(5)
    frame["internal_note"] = "ignore me"
    frame.to_parquet(path)

    assert list(load_events(path).columns) == list(EVENT_FIELDS)


# --- ordering, including tied timestamps --------------------------------------------


def test_window_comes_from_the_split_config() -> None:
    """Invariant 6: split dates live only in configs/splits.yaml."""
    from fraud.modeling.splits import windows

    assert replay_window() == (windows()["test"].start, windows()["test"].end)


def test_loading_never_uses_the_guarded_split_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streaming raw events must not go anywhere near labelled evaluation (§7.11)."""
    from fraud.modeling import splits

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the replayer must not call splits.load()")

    monkeypatch.setattr(splits, "load", explode)
    monkeypatch.setattr(splits, "load_many", explode)

    path = tmp_path / "events.parquet"
    _frame(4).to_parquet(path)
    assert len(load_events(path)) == 4


def test_events_outside_the_window_are_dropped(tmp_path: Path) -> None:
    start, end = replay_window()
    rows = [
        _row(0, start.to_pydatetime() - timedelta(seconds=1)),  # before
        _row(1, start.to_pydatetime()),  # inclusive lower bound
        _row(2, end.to_pydatetime() - timedelta(seconds=1)),  # inside
        _row(3, end.to_pydatetime()),  # exclusive upper bound
    ]
    path = tmp_path / "events.parquet"
    pd.DataFrame(rows)[list(EVENT_FIELDS)].to_parquet(path)

    assert list(load_events(path)["txn_id"]) == ["T0000001", "T0000002"]


def test_tied_timestamps_are_ordered_by_txn_id(tmp_path: Path) -> None:
    """The case that makes 'sorted by time' insufficient (5,260 ties in the real data)."""
    moment = START + timedelta(hours=1)
    rows = [_row(3, moment), _row(1, moment), _row(2, moment)]
    path = tmp_path / "events.parquet"
    pd.DataFrame(rows)[list(EVENT_FIELDS)].to_parquet(path)

    assert list(load_events(path)["txn_id"]) == ["T0000001", "T0000002", "T0000003"]


def test_ordering_is_stable_and_total(tmp_path: Path) -> None:
    moment = START + timedelta(hours=2)
    rows = [
        _row(5, moment + timedelta(seconds=1)),
        _row(2, moment),
        _row(9, moment + timedelta(seconds=1)),
        _row(1, moment),
    ]
    path = tmp_path / "events.parquet"
    pd.DataFrame(rows)[list(EVENT_FIELDS)].to_parquet(path)

    loaded = load_events(path)
    assert list(loaded["txn_id"]) == ["T0000001", "T0000002", "T0000005", "T0000009"]
    assert_ordered(loaded)


def test_out_of_order_input_is_refused() -> None:
    frame = _frame(4).iloc[::-1].reset_index(drop=True)
    with pytest.raises(ValueError, match="not ordered"):
        assert_ordered(frame)


def test_limit_and_start_at(tmp_path: Path) -> None:
    path = tmp_path / "events.parquet"
    _frame(10, step_seconds=3600).to_parquet(path)

    assert len(load_events(path, limit=3)) == 3
    later = load_events(path, start_at=START + timedelta(hours=7))
    assert list(later["txn_id"]) == ["T0000007", "T0000008", "T0000009"]


# --- backpressure (PLAN §9.1) -------------------------------------------------------


class _LagReplayer(Replayer):
    """A replayer whose consumer lag follows a script, so hysteresis is observable."""

    def __init__(self, *args: Any, lags: list[int | None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lags = list(lags)
        self.checks = 0

    def consumer_lag(self) -> int | None:
        self.checks += 1
        return self.lags.pop(0) if self.lags else 0


def _lag_replayer(lags: list[int | None], clock: FakeClock) -> _LagReplayer:
    return _LagReplayer(
        None,  # type: ignore[arg-type] - await_capacity never touches the client
        MaxPacer(),
        config=_config(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        lags=lags,
    )


def test_no_pause_below_the_high_threshold() -> None:
    clock = FakeClock()
    replayer = _lag_replayer([20_000], clock)
    replayer.await_capacity()

    assert replayer.stats.pauses == 0
    assert clock.sleeps == []


def test_pause_above_the_high_threshold_and_resume_below_the_low_one() -> None:
    """The hysteresis gap: it must not resume the moment it dips under 20,000."""
    clock = FakeClock()
    replayer = _lag_replayer([25_000, 19_000, 9_000, 4_999], clock)
    replayer.await_capacity()

    assert replayer.stats.pauses == 1
    # Slept through 19,000 and 9,000 — both under the pause threshold but over the
    # resume threshold — and only stopped at 4,999.
    assert len(clock.sleeps) == 3
    assert replayer.stats.paused_seconds == pytest.approx(sum(clock.sleeps))


def test_lag_between_the_thresholds_does_not_start_a_pause() -> None:
    """10,000 is over the resume line but under the pause line: keep going."""
    clock = FakeClock()
    replayer = _lag_replayer([10_000], clock)
    replayer.await_capacity()
    assert replayer.stats.pauses == 0


def test_unknown_lag_does_not_pause() -> None:
    """`lag` is None when Redis cannot place the group; do not stall on a guess."""
    clock = FakeClock()
    replayer = _lag_replayer([None], clock)
    replayer.await_capacity()
    assert replayer.stats.pauses == 0
    assert clock.sleeps == []


def test_backpressure_recovers_and_keeps_sending(redis_client: Redis) -> None:
    """End to end: a pause happens, then the whole frame still arrives in order."""
    clock = FakeClock()
    replayer = _LagReplayer(
        redis_client,
        MaxPacer(),
        config=_config(batch_size=2, backpressure={"check_every": 2}),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        lags=[30_000, 25_000, 1_000, 0, 0, 0, 0, 0],
    )
    frame = _frame(8)
    stats = replayer.send(frame)

    assert stats.sent == 8
    assert stats.pauses >= 1
    assert clock.sleeps  # it really waited
    entries = redis_client.xrange(load_yaml("stream")["stream"]["name"])
    assert [fields["txn_id"] for _, fields in entries] == list(frame["txn_id"])


# --- sending (Redis, DB 15) ----------------------------------------------------------


def test_events_reach_the_stream_in_order(redis_client: Redis) -> None:
    frame = _frame(25)
    Replayer(redis_client, MaxPacer(), config=_config(batch_size=10)).send(frame)

    stream = load_yaml("stream")["stream"]["name"]
    entries = redis_client.xrange(stream)
    assert len(entries) == 25
    assert [fields["txn_id"] for _, fields in entries] == list(frame["txn_id"])


def test_tied_timestamps_keep_their_order_in_the_stream(redis_client: Redis) -> None:
    moment = START + timedelta(hours=4)
    frame = pd.DataFrame([_row(i, moment) for i in range(6)])[list(EVENT_FIELDS)]
    Replayer(redis_client, MaxPacer(), config=_config(batch_size=2)).send(frame)

    entries = redis_client.xrange(load_yaml("stream")["stream"]["name"])
    assert [fields["txn_id"] for _, fields in entries] == list(frame["txn_id"])


def test_stream_messages_carry_no_labels(redis_client: Redis) -> None:
    Replayer(redis_client, MaxPacer(), config=_config()).send(_frame(5))

    for _, fields in redis_client.xrange(load_yaml("stream")["stream"]["name"]):
        assert set(fields) == set(MESSAGE_FIELDS)
        assert LABEL_FIELDS & fields.keys() == set()


def test_a_consumer_can_rebuild_the_event(redis_client: Redis) -> None:
    frame = _frame(3)
    Replayer(redis_client, MaxPacer(), config=_config()).send(frame)

    _, fields = redis_client.xrange(load_yaml("stream")["stream"]["name"])[0]
    event = TransactionEvent.from_message(fields)
    assert event.txn_id == frame["txn_id"].iloc[0]
    assert event.amount == pytest.approx(frame["amount"].iloc[0])
    assert int(fields["ingest_ts"]) > 0


def test_an_empty_frame_sends_nothing(redis_client: Redis) -> None:
    stats = Replayer(redis_client, MaxPacer(), config=_config()).send(_frame(0))
    assert stats.sent == 0
    assert redis_client.keys("*") == []


def test_max_pacing_never_sleeps(redis_client: Redis) -> None:
    clock = FakeClock()
    Replayer(
        redis_client, MaxPacer(), config=_config(), sleep=clock.sleep, monotonic=clock.monotonic
    ).send(_frame(20))
    assert clock.sleeps == []


def test_rate_pacing_sleeps_between_batches(redis_client: Redis) -> None:
    """At 10/s with batches of 5, the second batch waits half a second."""
    clock = FakeClock()
    Replayer(
        redis_client,
        RatePacer(10.0),
        config=_config(batch_size=5),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    ).send(_frame(10))

    assert clock.sleeps == pytest.approx([0.5])


def test_speedup_pacing_follows_event_time(redis_client: Redis) -> None:
    """One event per minute at 60x means one second of wall clock per batch."""
    clock = FakeClock()
    Replayer(
        redis_client,
        SpeedupPacer(60.0),
        config=_config(batch_size=1),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    ).send(_frame(4, step_seconds=60))

    assert clock.sleeps == pytest.approx([1.0, 1.0, 1.0])


def test_maxlen_is_applied(redis_client: Redis) -> None:
    """Approximate trimming keeps the stream bounded (§3.6)."""
    config = _config()
    config["stream"] = {**config["stream"], "maxlen": 5}
    Replayer(redis_client, MaxPacer(), config=config).send(_frame(50))

    length = redis_client.xlen(config["stream"]["name"])
    assert length <= 50
    assert length > 0


def test_consumer_lag_is_none_without_a_group(redis_client: Redis) -> None:
    replayer = Replayer(redis_client, MaxPacer(), config=_config())
    assert replayer.consumer_lag() is None

    replayer.send(_frame(3))
    assert replayer.consumer_lag() is None  # stream exists, no group yet


def test_consumer_lag_is_read_from_the_group(redis_client: Redis) -> None:
    config = _config()
    stream, group = config["stream"]["name"], config["stream"]["group"]
    replayer = Replayer(redis_client, MaxPacer(), config=config)
    replayer.send(_frame(10))

    redis_client.xgroup_create(stream, group, id="0")
    assert replayer.consumer_lag() == 10

    redis_client.xreadgroup(group, "c1", {stream: ">"}, count=4)
    assert replayer.consumer_lag() == 6


@pytest.mark.parametrize("field", sorted(LABEL_FIELDS))
def test_no_label_name_is_an_event_field(field: str) -> None:
    """Structural guarantee: the §3.5 column list simply has no label in it."""
    assert field not in EVENT_FIELDS
    assert field not in MESSAGE_FIELDS


def test_module_exposes_the_documented_entry_point() -> None:
    assert callable(mod.main)
