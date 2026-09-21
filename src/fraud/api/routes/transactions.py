"""GET /transactions/{txn_id}: the full scored record (PLAN §10)."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from fraud.storage import duck

logger = logging.getLogger(__name__)

router = APIRouter(tags=["monitoring"])


@router.get("/transactions/{txn_id}")
def transaction(txn_id: str) -> dict[str, Any]:
    """Everything the scorer recorded for one transaction.

    Reads through the §9.4 DuckDB view, so a row written twice by the
    crash-between-flush-and-ack case appears once. Parameterised, not interpolated: the
    id comes from the URL, and DuckDB will happily execute whatever is spliced into a
    query string.
    """
    connection = duck.connect()
    try:
        found = connection.execute(
            "SELECT * FROM scored WHERE txn_id = ? LIMIT 1", [txn_id]
        ).fetch_df()
    finally:
        connection.close()

    if found.empty:
        raise HTTPException(status_code=404, detail=f"no scored transaction {txn_id!r}")

    record = found.iloc[0].to_dict()
    # `reasons` is stored as a JSON string (§9.2); returning it parsed saves every caller
    # from doing it, and the dashboard from doing it wrong.
    if isinstance(record.get("reasons"), str):
        try:
            record["reasons"] = json.loads(record["reasons"])
        except ValueError:  # pragma: no cover - only a corrupted row
            pass
    return json.loads(json.dumps(record, default=str))
