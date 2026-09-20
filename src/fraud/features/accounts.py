"""AccountDirectory: read-only account reference data (PLAN §5.4).

Home city, home coordinates and creation time come from ``accounts.parquet`` and never
change during a run, so they are loaded once and shared. Keeping them out of the mutable
account state means the replay and the live scorer read the same fixed facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class AccountFacts:
    home_city: str
    home_lat: float
    home_lon: float
    created_at: datetime


class AccountDirectory:
    """Immutable lookup of the account attributes features need."""

    __slots__ = ("_by_id",)

    def __init__(self, by_id: dict[str, AccountFacts]) -> None:
        self._by_id = by_id

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> AccountDirectory:
        return cls(
            {
                str(row["account_id"]): AccountFacts(
                    home_city=str(row["home_city"]),
                    home_lat=float(row["home_lat"]),
                    home_lon=float(row["home_lon"]),
                    created_at=pd.Timestamp(row["created_at"]).to_pydatetime(),
                )
                for row in rows
            }
        )

    @classmethod
    def from_parquet(cls, path: Path) -> AccountDirectory:
        frame = pd.read_parquet(
            path, columns=["account_id", "home_city", "home_lat", "home_lon", "created_at"]
        )
        return cls.from_rows(frame.to_dict("records"))

    def get(self, account_id: str) -> AccountFacts | None:
        return self._by_id.get(account_id)

    def __contains__(self, account_id: str) -> bool:
        return account_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)
