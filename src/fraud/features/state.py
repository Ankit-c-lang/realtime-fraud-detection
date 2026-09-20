"""AccountState and its exact JSON round trip (PLAN §5.3).

The blob is what Redis stores per account, so the round trip has to be byte-stable:
serialise, restore, serialise again, and get identical bytes. Sets are written as sorted
lists, keys are sorted, and floats go through Python's repr, which round-trips exactly.
A drifting blob would make the two stores disagree and break the parity test in §5.4.

Timestamps are epoch MILLISECONDS as integers (§5.1 rule 1). Redis sorted-set scores are
doubles, which represent integers of this size exactly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

EPOCH: Final[datetime] = datetime(1970, 1, 1)  # noqa: DTZ001 - naive IST, matches §3.5

JSON_SEPARATORS: Final[tuple[str, str]] = (",", ":")


def to_millis(moment: datetime) -> int:
    """Epoch milliseconds from a naive IST timestamp.

    Deliberately not ``datetime.timestamp()``: that interprets a naive value in the
    machine's local zone, so the same event would get different state on a different
    machine and the replay would stop being reproducible.
    """
    return int((moment - EPOCH).total_seconds() * 1000)


@dataclass(slots=True)
class AccountState:
    """Everything the hot features need about one account (PLAN §5.3)."""

    # (ts_ms, amount, merchant_id, declined) within the last 24 h, in time order.
    recent: list[tuple[int, float, str, int]] = field(default_factory=list)
    last_ts: int | None = None
    last_lat: float | None = None
    last_lon: float | None = None
    n_all: int = 0
    n_ok: int = 0
    sum_ok: float = 0.0
    sumsq_ok: float = 0.0
    hour_buckets: list[int] = field(default_factory=lambda: [0] * 6)
    devices: dict[str, int] = field(default_factory=dict)
    merchants: set[str] = field(default_factory=set)
    cities: set[str] = field(default_factory=set)

    def mean_amount(self) -> float | None:
        """Mean of prior APPROVED transactions (PLAN §5.1 rule 5)."""
        return self.sum_ok / self.n_ok if self.n_ok else None

    def std_amount(self) -> float:
        """Sample standard deviation of prior approved amounts, never negative."""
        if self.n_ok < 2:
            return 0.0
        variance = (self.sumsq_ok - self.sum_ok**2 / self.n_ok) / (self.n_ok - 1)
        return max(variance, 0.0) ** 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "recent": [list(entry) for entry in self.recent],
            "last_ts": self.last_ts,
            "last_lat": self.last_lat,
            "last_lon": self.last_lon,
            "n_all": self.n_all,
            "n_ok": self.n_ok,
            "sum_ok": self.sum_ok,
            "sumsq_ok": self.sumsq_ok,
            "hour_buckets": list(self.hour_buckets),
            "devices": dict(sorted(self.devices.items())),
            "merchants": sorted(self.merchants),
            "cities": sorted(self.cities),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AccountState:
        return cls(
            recent=[(int(a), float(b), str(c), int(d)) for a, b, c, d in raw["recent"]],
            last_ts=raw["last_ts"],
            last_lat=raw["last_lat"],
            last_lon=raw["last_lon"],
            n_all=int(raw["n_all"]),
            n_ok=int(raw["n_ok"]),
            sum_ok=float(raw["sum_ok"]),
            sumsq_ok=float(raw["sumsq_ok"]),
            hour_buckets=[int(value) for value in raw["hour_buckets"]],
            devices={str(key): int(value) for key, value in raw["devices"].items()},
            merchants=set(raw["merchants"]),
            cities=set(raw["cities"]),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=JSON_SEPARATORS)

    @classmethod
    def from_json(cls, blob: str) -> AccountState:
        return cls.from_dict(json.loads(blob))
