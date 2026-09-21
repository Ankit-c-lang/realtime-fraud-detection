"""Read-only in-memory DuckDB views over Parquet (PLAN §9.4, §3.7, invariant 7).

No ``.duckdb`` file anywhere. A database file takes a lock, and the moment the dashboard
holds it the scorer's next write blocks — so services never open one. An in-memory
connection reading Parquet through a glob has no lock, no shared state and no startup
cost worth measuring, and every reader gets its own.

**The view deduplicates.** §9.3 accepts that a crash between the flush and the ``XACK``
writes a row twice: the redelivered event returns its stored record, so the duplicate is
byte-identical rather than contradictory. Nothing tries to prevent it, because preventing
it would mean giving up the cheap idempotency the whole design rests on. Instead readers
resolve it, keeping the earliest ``scored_ts`` per ``txn_id``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import duckdb

from fraud.config import Settings

logger = logging.getLogger(__name__)

SCORED_VIEW: Final[str] = "scored"

# PLAN §9.4, verbatim. union_by_name tolerates a schema that grew between runs, and
# hive_partitioning exposes the date= directory as a column.
_SCORED_SQL: Final[str] = """
CREATE OR REPLACE VIEW {view} AS
SELECT *
FROM read_parquet('{glob}', hive_partitioning = true, union_by_name = true)
QUALIFY row_number() OVER (PARTITION BY txn_id ORDER BY scored_ts) = 1
"""

# Until the scorer has written anything there is no schema to read. A view with one
# column and no rows keeps `SELECT count(*)` and the dashboard's empty state working,
# rather than making every caller handle a missing relation.
_EMPTY_SQL: Final[str] = """
CREATE OR REPLACE VIEW {view} AS SELECT NULL AS txn_id WHERE false
"""

# §9.4's monitoring queries. They live here, not in the README and the dashboard
# separately, so the numbers quoted in one are the numbers shown by the other (§0.4).
RISKIEST_MERCHANTS: Final[str] = """
SELECT merchant_id, COUNT(*) AS txns, AVG(risk) AS avg_risk,
       SUM(CASE WHEN decision <> 'ALLOW' THEN 1 ELSE 0 END) AS alerts
FROM scored GROUP BY merchant_id ORDER BY alerts DESC LIMIT 10
"""

ALERT_RATE_BY_HOUR: Final[str] = """
SELECT date_trunc('hour', event_time) AS hour,
       AVG(CASE WHEN decision <> 'ALLOW' THEN 1.0 ELSE 0.0 END) AS alert_rate
FROM scored GROUP BY 1 ORDER BY 1
"""

LATENCY_PERCENTILES: Final[str] = """
SELECT quantile_cont(scored_ts - ingest_ts, [0.5, 0.95, 0.99]) AS latency_ms FROM scored
"""


def scored_glob(root: Path | None = None) -> str:
    directory = root or Settings.from_env().scored_dir
    return str(directory / "*" / "*.parquet")


def has_scored_data(root: Path | None = None) -> bool:
    directory = root or Settings.from_env().scored_dir
    return directory.is_dir() and any(directory.glob("*/*.parquet"))


def connect(root: Path | None = None) -> duckdb.DuckDBPyConnection:
    """An in-memory connection with the ``scored`` view defined (PLAN §9.4).

    The caller owns the connection and should close it. Opening one per request is fine;
    there is no file and no lock to contend for.
    """
    connection = duckdb.connect(database=":memory:")
    if has_scored_data(root):
        connection.execute(_SCORED_SQL.format(view=SCORED_VIEW, glob=scored_glob(root)))
    else:
        logger.info("no scored parquet yet; %s is an empty view", SCORED_VIEW)
        connection.execute(_EMPTY_SQL.format(view=SCORED_VIEW))
    return connection
