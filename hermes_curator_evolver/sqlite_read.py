"""Strict, bounded inspection of a quiescent SQLite file without opening it in SQLite.

Unlike mode=ro, this never creates or modifies source WAL/SHM/journal files.
Live/journaled and WAL-format stores are refused: supply a closed, consistent
DELETE-mode snapshot. The copy exists only in memory, not a private-data temp file.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MAX_INSPECTION_BYTES = 64 * 1024 * 1024


class ClosingConnection(sqlite3.Connection):
    """Keep SQLite transaction semantics and deterministically release its handle."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def open_read_snapshot(path: str | Path) -> sqlite3.Connection:
    """Return a query-only in-memory copy; never initialize/repair the source.

    Detect source drift around the bounded copy. This is not a live database
    backup protocol: callers must provide a quiescent file, and journal markers
    or unsupported SQLite builds cause explicit refusal rather than a fallback.
    """
    source = Path(path)
    companions = [Path(str(source) + suffix) for suffix in ("-wal", "-shm", "-journal")]

    def fingerprint():
        if any(p.exists() for p in companions):
            raise RuntimeError("journaled store: provide a closed, consistent DELETE-mode snapshot")
        stat = source.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino

    before = fingerprint()
    if before[0] > MAX_INSPECTION_BYTES:
        raise RuntimeError("inspection snapshot exceeds the 64 MiB safety budget")
    with source.open("rb") as handle:
        payload = handle.read(MAX_INSPECTION_BYTES + 1)
    if len(payload) > MAX_INSPECTION_BYTES or fingerprint() != before:
        raise RuntimeError("store changed during inspection; supply a consistent snapshot")
    if payload[:16] != b"SQLite format 3\x00":
        raise sqlite3.DatabaseError("evidence file is not a SQLite database")
    if payload[18:20] != b"\x01\x01":
        raise RuntimeError("WAL-format store: provide a closed, consistent DELETE-mode snapshot")
    connection = sqlite3.connect(":memory:", factory=ClosingConnection)
    try:
        if not hasattr(connection, "deserialize"):
            raise RuntimeError("this SQLite build cannot inspect in-memory snapshots")
        connection.deserialize(payload)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.row_factory = sqlite3.Row
        return connection
    except BaseException:
        connection.close()
        raise
