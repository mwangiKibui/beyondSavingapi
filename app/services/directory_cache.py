"""The shared payee Directory, built once per process (ab-300).

Building it compiles every approved name pattern (hundreds - more than
Python's re cache holds), which cost ~16 ms of event-loop CPU per request.
Each use now reads a one-row version of the source tables instead and
rebuilds only when that changes, so it stays right across API processes,
the worker, migrations and admin edits without Redis or explicit
invalidation. MAX_AGE_SECONDS is a backstop for hand-run SQL that changes
a row without touching its timestamps.

The cached Directory is shared by every request: read it, never mutate it.
"""

import time

import asyncpg

from app.repositories.category_suggestions import (
    get_approved_directory,
    get_blocked_numbers,
    get_directory_version,
)
from app.services.category_suggestions import Directory

MAX_AGE_SECONDS = 600

# (version, built_at monotonic seconds, directory). No lock: two requests
# missing at once both build (same result), only at start or after an edit.
_cached: tuple[tuple, float, Directory] | None = None


async def get_directory(conn: asyncpg.Pool | asyncpg.Connection) -> Directory:
    global _cached
    version = await get_directory_version(conn)
    now = time.monotonic()
    if _cached is not None and _cached[0] == version and now - _cached[1] < MAX_AGE_SECONDS:
        return _cached[2]
    # Stored under the version read BEFORE the rows: a write landing in
    # between shows up as a newer version next time and rebuilds.
    directory = Directory.from_rows(await get_approved_directory(conn), blocked=await get_blocked_numbers(conn))
    _cached = (version, now, directory)
    return directory


def clear() -> None:
    global _cached
    _cached = None
