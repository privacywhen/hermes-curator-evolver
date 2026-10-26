import sqlite3
import pytest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermes_curator_evolver.storage import EvidenceStore


def test_migration_failure_rolls_back_tables_columns_and_preserves_legacy_rows(tmp_path):
    from hermes_curator_evolver.sqlite_read import ClosingConnection
    db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tool_events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL, tool_name TEXT NOT NULL, duration_ms INTEGER, is_error INTEGER NOT NULL, skill_name TEXT, args_json TEXT NOT NULL, result_preview TEXT NOT NULL)")
        conn.execute("INSERT INTO tool_events VALUES (1, '2026-01-01', 's', 't', 'terminal', NULL, 0, NULL, '{}', '')")
        before = list(conn.iterdump())
    before_names = sorted(p.name for p in tmp_path.iterdir())

    class FailingConnection(ClosingConnection):
        def execute(self, sql, parameters=()):
            if sql.startswith("ALTER TABLE tool_events ADD COLUMN turn_id"):
                raise sqlite3.OperationalError("synthetic migration failure")
            return super().execute(sql, parameters)

    class FailingStore(EvidenceStore):
        def connect(self):
            conn = sqlite3.connect(self.db_path, factory=FailingConnection)
            conn.row_factory = sqlite3.Row
            return conn

    with pytest.raises(sqlite3.OperationalError, match="synthetic migration failure"):
        FailingStore(db)
    with sqlite3.connect(db) as conn:
        assert list(conn.iterdump()) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == before_names
    EvidenceStore(db)  # A later explicit retry can migrate the preserved source.
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT session_id FROM tool_events WHERE id=1").fetchone() == ("s",)


def test_native_status_and_ids_are_separate_from_minimized_payload(tmp_path):
    store = EvidenceStore(tmp_path / "events.sqlite", collect_previews=False)
    store.record_tool_call(
        tool_name="terminal", args={"command": "secret"}, result="looks successful",
        task_id="task", session_id="session", tool_call_id="native-call",
        turn_id="native-turn", api_request_id="native-api", status="blocked",
        error_type="PolicyError",
    )
    row = store.recent_tool_events(days=30, limit=1)[0]
    assert row["tool_call_id"] == "native-call"
    assert row["turn_id"] == "native-turn"
    assert row["api_request_id"] == "native-api"
    assert row["status"] == "blocked"
    assert row["error_type"] == "PolicyError"
    assert "secret" not in row["args_json"]
    assert row["is_error"] == 1


def test_unknown_native_status_does_not_get_inferred_as_success(tmp_path):
    store = EvidenceStore(tmp_path / "events.sqlite")
    store.record_tool_call(
        tool_name="terminal", args={}, result="success", status="future-status"
    )
    row = store.recent_tool_events(days=30, limit=1)[0]
    assert row["status"] == "unknown"
    assert row["outcome_known"] is False


def test_batch_actions_are_children_of_one_event_and_only_confirmed_results_succeed(tmp_path):
    store = EvidenceStore(tmp_path / "events.sqlite")
    store.record_tool_call(
        tool_name="skill_manage",
        args={"operations": [
            {"name": "alpha", "action": "create"},
            {"name": "beta", "action": "patch"},
        ]},
        result={"success": False, "failed_index": 1, "completed_before_failure": 1,
                "error": "rolled back"},
        task_id="t", session_id="s",
    )
    summary = store.summary(days=30)
    assert summary["tool_events"] == 1
    assert summary["tool_actions"] == 2
    with store.connect() as conn:
        rows = conn.execute("SELECT skill_name, action, succeeded FROM tool_event_actions ORDER BY action_index").fetchall()
    assert [(r["skill_name"], r["action"], r["succeeded"]) for r in rows] == [
        ("alpha", "create", None), ("beta", "patch", None)
    ]


def test_legacy_database_initialization_preserves_rows_and_adds_nullable_schema(tmp_path):
    db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tool_events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL, tool_name TEXT NOT NULL, duration_ms INTEGER, is_error INTEGER NOT NULL, skill_name TEXT, args_json TEXT NOT NULL, result_preview TEXT NOT NULL)")
        conn.execute("INSERT INTO tool_events VALUES (1, '2026-01-01T00:00:00+00:00', 's', 't', 'terminal', NULL, 0, NULL, '{}', '')")
    EvidenceStore(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT id, session_id, task_id FROM tool_events").fetchone()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_events)")}
    assert row == (1, "s", "t")
    assert {"tool_call_id", "turn_id", "api_request_id", "status", "error_type"} <= cols


def test_concurrent_store_initialization_is_safe(tmp_path):
    db = tmp_path / "concurrent.sqlite"
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: EvidenceStore(db), range(6)))
    store = EvidenceStore(db)
    store.record_tool_call(tool_name="terminal", args={}, result="ok")
    assert store.summary(days=30)["tool_events"] == 1


def test_read_only_legacy_snapshot_reads_without_schema_migration(tmp_path):
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE tool_events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL, tool_name TEXT NOT NULL, duration_ms INTEGER, is_error INTEGER NOT NULL, skill_name TEXT, args_json TEXT NOT NULL, result_preview TEXT NOT NULL); CREATE INDEX idx_tool_events_created ON tool_events(created_at); CREATE INDEX idx_tool_events_skill ON tool_events(skill_name); CREATE INDEX idx_tool_events_backfill_key ON tool_events(session_id, task_id, tool_name); CREATE TABLE turn_events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, session_id TEXT NOT NULL, model TEXT NOT NULL, platform TEXT NOT NULL, user_preview TEXT NOT NULL, assistant_preview TEXT NOT NULL); CREATE INDEX idx_turn_events_created ON turn_events(created_at); CREATE INDEX idx_turn_events_session ON turn_events(session_id); CREATE TABLE session_events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, session_id TEXT NOT NULL, completed INTEGER NOT NULL, interrupted INTEGER NOT NULL, model TEXT NOT NULL, platform TEXT NOT NULL); CREATE INDEX idx_session_events_created ON session_events(created_at); CREATE INDEX idx_session_events_session ON session_events(session_id); INSERT INTO tool_events VALUES (1, '2026-01-01', 's', 't', 'terminal', NULL, 0, NULL, '{}', '');")
    store = EvidenceStore(db, read_only=True)
    row = store.recent_tool_events(days=9999, limit=1)[0]
    assert row["status"] is None
    with sqlite3.connect(db) as conn:
        assert "status" not in {r[1] for r in conn.execute("PRAGMA table_info(tool_events)")}


def test_collected_turns_preserve_event_identity_independently_of_text(tmp_path):
    store = EvidenceStore(tmp_path / "turns.sqlite", collect_previews=True)
    store.record_turn(session_id="s", model="m", platform="p", event_id="native-1", user_message="same", assistant_response="text one")
    store.record_turn(session_id="s", model="m", platform="p", event_id="native-2", user_message="same", assistant_response="text two")
    with store.connect() as conn:
        ids = [row[0] for row in conn.execute("SELECT event_id FROM turn_events ORDER BY event_id")]
    assert ids == ["native-1", "native-2"]
