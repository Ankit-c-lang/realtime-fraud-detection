"""InMemoryStore: the offline state backend (PLAN §5.3, §5.4).

Used by the training replay and as the reference the Redis store is checked against.
The two hold state differently but must produce identical features, which is what the
parity test in §5.4 asserts.

Entity fan-out keys on the LAST time each account used an entity, so the number of
distinct accounts in a window is the number whose last use falls inside it. That works
only because events arrive in time order, and it is why a sorted structure is enough
instead of keeping every visit.
"""

from __future__ import annotations

from typing import Any

from sortedcontainers import SortedList

from fraud.features.engine import StateView
from fraud.features.spec import FeatureConfig
from fraud.features.state import AccountState, to_millis
from fraud.schemas import TransactionEvent

MILLIS_PER_SECOND = 1000


class _EntityIndex:
    """Last-use timestamp per account for one entity, with windowed counts."""

    __slots__ = ("last", "order")

    def __init__(self) -> None:
        self.last: dict[str, int] = {}
        self.order: SortedList[tuple[int, str]] = SortedList()

    def touch(self, account_id: str, ts: int) -> None:
        previous = self.last.get(account_id)
        if previous is not None:
            if previous >= ts:
                # Mirrors Redis ZADD GT: a late arrival never moves a timestamp back.
                return
            self.order.remove((previous, account_id))
        self.last[account_id] = ts
        self.order.add((ts, account_id))

    def count(self, low: int, high: int) -> int:
        """Accounts whose last use falls in the half-open window [low, high)."""
        # Account ids are never empty, so (bound, "") sorts before any real entry at
        # that timestamp, which gives an inclusive low and an exclusive high.
        return self.order.bisect_left((high, "")) - self.order.bisect_left((low, ""))


class InMemoryStore:
    """Account blobs plus one entity index per device, IP and merchant."""

    __slots__ = ("_accounts", "_cfg", "_devices", "_ips", "_merchants", "_records")

    def __init__(self, cfg: FeatureConfig | None = None) -> None:
        self._cfg = cfg or FeatureConfig.load()
        self._accounts: dict[str, AccountState] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._devices: dict[str, _EntityIndex] = {}
        self._ips: dict[str, _EntityIndex] = {}
        self._merchants: dict[str, _EntityIndex] = {}

    # --- StateStore protocol (PLAN §5.4) ---

    def committed(self, txn_id: str) -> dict[str, Any] | None:
        return self._records.get(txn_id)

    def load(self, event: TransactionEvent) -> StateView:
        now = to_millis(event.event_time)
        hour_ago = now - self._cfg.hour_seconds * MILLIS_PER_SECOND
        month_ago = now - self._cfg.month_seconds * MILLIS_PER_SECOND

        device = self._devices.get(event.device_id)
        ip = self._ips.get(event.ip)
        merchant = self._merchants.get(event.merchant_id)

        return StateView(
            account=self._accounts.get(event.account_id, AccountState()),
            dev_accts_1h=device.count(hour_ago, now) if device else 0,
            dev_accts_30d=device.count(month_ago, now) if device else 0,
            ip_accts_1h=ip.count(hour_ago, now) if ip else 0,
            ip_accts_30d=ip.count(month_ago, now) if ip else 0,
            mer_accts_30d=merchant.count(month_ago, now) if merchant else 0,
        )

    def commit(
        self, event: TransactionEvent, account: AccountState, record: dict[str, Any]
    ) -> None:
        now = to_millis(event.event_time)
        self._records[event.txn_id] = record
        self._accounts[event.account_id] = account

        for index, key in (
            (self._devices, event.device_id),
            (self._ips, event.ip),
            (self._merchants, event.merchant_id),
        ):
            index.setdefault(key, _EntityIndex()).touch(event.account_id, now)

    # --- checkpointing (PLAN §5.5) ---

    def account_states(self) -> dict[str, AccountState]:
        return self._accounts

    def entity_last_seen(self) -> dict[str, dict[str, dict[str, int]]]:
        """Last-use maps, which is all a checkpoint needs to rebuild the indexes."""
        return {
            "dev": {key: dict(index.last) for key, index in self._devices.items()},
            "ip": {key: dict(index.last) for key, index in self._ips.items()},
            "mer": {key: dict(index.last) for key, index in self._merchants.items()},
        }

    def restore(
        self,
        accounts: dict[str, AccountState],
        entities: dict[str, dict[str, dict[str, int]]],
    ) -> None:
        self._accounts = dict(accounts)
        for name, target in (
            ("dev", self._devices),
            ("ip", self._ips),
            ("mer", self._merchants),
        ):
            target.clear()
            for key, last_seen in entities.get(name, {}).items():
                index = _EntityIndex()
                for account_id, ts in sorted(last_seen.items(), key=lambda item: item[1]):
                    index.touch(account_id, ts)
                target[key] = index
