"""The only code that writes Parquet, using atomic renames (PLAN §9.4, §16).

Everything that lands in ``data/`` goes through here, for one reason: a reader must never
see a half-written file. The scorer writes continuously while the dashboard and DuckDB
read the same directory, and a plain ``to_parquet`` straight onto the final path leaves a
window in which the file exists but its footer does not. DuckDB does not skip such a
file — it raises, and the dashboard goes down for as long as the write takes.

So every write goes to ``<name>.parquet.tmp`` and is then moved with ``os.replace``, which
is atomic within a filesystem. Readers glob ``*.parquet``, so a ``.tmp`` file is invisible
to them by construction rather than by a rule someone has to remember.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

import pandas as pd

logger = logging.getLogger(__name__)

TEMP_SUFFIX: Final[str] = ".tmp"
COMPRESSION: Final[str] = "snappy"


def write_atomic(frame: pd.DataFrame, path: Path, *, compression: str = COMPRESSION) -> Path:
    """Write ``frame`` to ``path`` so that readers only ever see it complete.

    The temporary file sits in the destination directory, not in ``/tmp``: ``os.replace``
    is only atomic within one filesystem, and a scratch directory is very often a
    different one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + TEMP_SUFFIX)

    try:
        frame.to_parquet(temporary, index=False, compression=compression)
        os.replace(temporary, path)
    except BaseException:
        # A leftover .tmp is invisible to readers but would accumulate, and on a retry
        # the stale bytes could be mistaken for progress. Clear it either way.
        temporary.unlink(missing_ok=True)
        raise

    logger.debug("wrote %s (%d rows)", path, len(frame))
    return path


def cleanup_temp_files(directory: Path) -> int:
    """Remove orphaned ``.tmp`` files left by a crashed writer (PLAN §9.3).

    Safe to run at startup: a live write holds its temporary file only for the moment
    between ``to_parquet`` and ``os.replace``, and the failure matrix already treats a
    leftover ``.tmp`` as "ignored".
    """
    removed = 0
    for stale in directory.rglob(f"*{TEMP_SUFFIX}"):
        stale.unlink(missing_ok=True)
        removed += 1
    if removed:
        logger.info("removed %d orphaned %s file(s) under %s", removed, TEMP_SUFFIX, directory)
    return removed
