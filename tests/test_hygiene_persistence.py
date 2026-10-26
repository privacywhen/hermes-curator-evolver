"""Synthetic-only SQLite byte-boundary tests for credential minimization."""

import sqlite3

from hermes_curator_evolver.storage import EvidenceStore


def test_digit_free_labeled_session_credential_never_reaches_sqlite(tmp_path):
    db_path = tmp_path / "evidence.db"
    store = EvidenceStore(db_path, collect_previews=True)
    store.record_tool_call(
        tool_name="synthetic_tool",
        args={"note": "password=hunter"},
        result={"ok": True},
        task_id="task-1",
        session_id="password=hunter",
        created_at="2026-01-01T00:00:00+00:00",
    )

    with sqlite3.connect(db_path) as connection:
        stored = connection.execute(
            "SELECT session_id, args_json, result_preview FROM tool_events"
        ).fetchone()

    assert stored is not None
    raw_bytes = db_path.read_bytes()
    assert b"password=hunter" not in raw_bytes
    assert b"hunter" not in raw_bytes
    assert stored[0].startswith('["sha256","') and stored[0].endswith('"]')
    assert "[REDACTED:generic-token-assignment]" in stored[1]
