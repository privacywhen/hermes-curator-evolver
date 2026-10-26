"""Non-deleting evidence/backup inventory. No purge, daemon, migration, or repair."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .hygiene import scrub
from .sqlite_read import open_read_snapshot
from .storage import cutoff_iso


def privacy_inventory(db_path: str | Path, *, backup_dir: str | Path,
                      retention_days: int = 30, file_limit: int = 100) -> dict[str, Any]:
    if not 1 <= retention_days <= 3650 or not 0 <= file_limit <= 1000:
        raise ValueError("retention days must be 1..3650 and file limit 0..1000")
    db = Path(db_path)
    tables = {}
    with open_read_snapshot(db) as conn:
        for table in ("tool_events", "turn_events", "session_events"):
            row = conn.execute(
                f"SELECT COUNT(*) AS rows, COALESCE(SUM(created_at < ?), 0) AS older_than_window, "
                f"MIN(created_at) AS oldest, MAX(created_at) AS newest FROM {table}",
                [cutoff_iso(retention_days)],
            ).fetchone()
            tables[table] = dict(row)
    root = Path(backup_dir)
    pending = [root] if (not root.is_symlink() and not getattr(root, "is_junction", lambda: False)()
                        and root.is_dir()) else []
    files = []
    visited = 0
    truncated = False
    while pending and not truncated:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                visited += 1
                if visited > 1000 or len(files) >= file_limit:
                    truncated = True
                    break
                path = Path(entry.path)
                if entry.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append({"path": scrub(str(path.relative_to(root))),
                                  "bytes": entry.stat(follow_symlinks=False).st_size})
    return {"dry_run": True, "deletion_supported": False, "retention_days": retention_days,
            "db_bytes": db.stat().st_size, "tables": tables,
            "backups": {"files_observed": len(files), "files": files, "truncated": truncated},
            "policy": "No rows or files deleted. Age is inventory, not approval to purge. "
                      "Legacy evidence and backups can retain raw content; do not restore raw snapshots as a privacy rollback."}
