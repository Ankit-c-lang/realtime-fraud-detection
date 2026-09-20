"""Account-to-account projection on toy data (PLAN §6.1, §13).

A family and a ring must become edges; an over-cap entity must produce none. That last
one is the whole reason the caps exist: without them a card-testing device fuses its
unrelated victims into a fake ring, and personalised PageRank then pushes that risk onto
the victims' later, legitimate purchases.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from fraud.graph.projection import (
    EDGE_COLUMNS,
    capped_entities,
    device_membership,
    graph_config,
    project,
)

T = pd.Timestamp("2026-02-01")


def events(rows: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    """(account, device, ip, when) tuples, where `when` is a day offset before T."""
    return pd.DataFrame(
        [
            {
                "event_time": T - pd.Timedelta(days=float(when)),
                "account_id": account,
                "device_id": device,
                "ip": ip,
            }
            for account, device, ip, when in rows
        ]
    )


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return graph_config()


def test_a_shared_device_creates_an_edge() -> None:
    frame = events([("A1", "D1", "IP1", "1"), ("A2", "D1", "IP2", "1")])
    edges = project(frame, T)

    assert tuple(edges.columns) == EDGE_COLUMNS
    assert edges[["u", "v"]].values.tolist() == [["A1", "A2"]]


def test_a_household_of_three_becomes_a_triangle() -> None:
    """Triangles are what make clustering non-zero, unlike the bipartite design (§1)."""
    frame = events([("A1", "D1", "IP1", "1"), ("A2", "D1", "IP2", "1"), ("A3", "D1", "IP3", "1")])
    edges = project(frame, T)
    assert edges[["u", "v"]].values.tolist() == [["A1", "A2"], ["A1", "A3"], ["A2", "A3"]]


def test_sharing_a_device_and_an_ip_weighs_two() -> None:
    """Edge weight is the number of capped entities the two accounts share."""
    frame = events([("A1", "D1", "IP1", "1"), ("A2", "D1", "IP1", "1")])
    edges = project(frame, T)

    assert len(edges) == 1
    assert int(edges["weight"].iloc[0]) == 2


def test_an_entity_used_by_one_account_creates_nothing() -> None:
    frame = events([("A1", "D1", "IP1", "1"), ("A1", "D1", "IP1", "2")])
    assert project(frame, T).empty


def test_an_over_cap_device_creates_no_edges(config: dict[str, Any]) -> None:
    """The card-testing case: one device, many unrelated victims (§6.1)."""
    cap = int(config["device_cap"])
    frame = events([(f"A{i:03d}", "DBAD", f"IP{i:03d}", "1") for i in range(cap + 1)])

    assert project(frame, T).empty


def test_a_device_exactly_at_the_cap_still_counts(config: dict[str, Any]) -> None:
    """BETWEEN is inclusive, so the cap itself is kept. A ring of 15 must survive."""
    cap = int(config["device_cap"])
    frame = events([(f"A{i:03d}", "DOK", f"IP{i:03d}", "1") for i in range(cap)])

    edges = project(frame, T)
    assert len(edges) == cap * (cap - 1) // 2


def test_an_over_cap_ip_creates_no_edges(config: dict[str, Any]) -> None:
    """Carrier NAT: hundreds of unrelated accounts behind one address."""
    cap = int(config["ip_cap"])
    frame = events([(f"A{i:03d}", f"D{i:03d}", "IPNAT", "1") for i in range(cap + 1)])

    assert project(frame, T).empty


def test_a_capped_entity_does_not_block_a_good_one(config: dict[str, Any]) -> None:
    """A ring sharing a device must survive even when its members also sit behind NAT."""
    cap = int(config["ip_cap"])
    rows = [(f"A{i:03d}", f"D{i:03d}", "IPNAT", "1") for i in range(cap + 1)]
    rows += [("A000", "DRING", "IPX", "1"), ("A001", "DRING", "IPY", "1")]

    edges = project(pd.DataFrame(events(rows)), T)
    assert edges[["u", "v"]].values.tolist() == [["A000", "A001"]]


def test_merchants_are_not_entities() -> None:
    """A popular merchant would fuse most of the population into one component (§6.1)."""
    frame = events([("A1", "D1", "IP1", "1"), ("A2", "D2", "IP2", "1")])
    frame["merchant_id"] = "M001"

    assert project(frame, T).empty


def test_edges_are_sorted() -> None:
    """Louvain is order-sensitive, so unsorted edges break run-to-run parity (§6.6)."""
    frame = events([("A9", "D1", "IP1", "1"), ("A2", "D1", "IP2", "1"), ("A5", "D1", "IP3", "1")])
    edges = project(frame, T)

    assert edges[["u", "v"]].values.tolist() == sorted(edges[["u", "v"]].values.tolist())


def test_projection_is_deterministic() -> None:
    frame = events([(f"A{i}", "D1", f"IP{i}", "1") for i in range(5)])
    pd.testing.assert_frame_equal(project(frame, T), project(frame, T))


def test_capped_entities_reports_account_counts(config: dict[str, Any]) -> None:
    frame = events([("A1", "D1", "IP1", "1"), ("A2", "D1", "IP2", "1"), ("A3", "D2", "IP3", "1")])
    kept = capped_entities(frame, T, "device", config)

    assert kept["ent"].tolist() == ["D1"]
    assert int(kept["accounts"].iloc[0]) == 2


def test_device_membership_uses_the_same_cap(config: dict[str, Any]) -> None:
    """community_shared_devices counts these, so the rule has to match the edges."""
    cap = int(config["device_cap"])
    rows = [("A1", "DOK", "IP1", "1"), ("A2", "DOK", "IP2", "1")]
    rows += [(f"B{i:03d}", "DBAD", f"IPB{i:03d}", "1") for i in range(cap + 1)]

    members = device_membership(pd.DataFrame(events(rows)), T, config)
    assert set(members["device_id"]) == {"DOK"}
