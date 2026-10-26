import pytest

from hermes_curator_evolver.storage import EvidenceStore


def test_batch_summary_attributes_invocation_once_per_skill_not_once_per_action(tmp_path):
    store = EvidenceStore(tmp_path / "batch-summary.sqlite")
    store.record_tool_call(tool_name="skill_manage", status="ok",
        args={"operations": [{"name": "alpha", "action": "edit"}, {"name": "beta", "action": "edit"}, {"name": "alpha", "action": "edit"}]},
        result={"success": True, "results": [{"success": True}] * 3})
    summary = store.summary(days=30)
    assert summary["tool_events"] == 1 and summary["tool_actions"] == 3
    assert summary["skill_events"] == 1
    assert {row["skill_name"]: row["event_count"] for row in summary["skills"]} == {"alpha": 1, "beta": 1}
    scoped = store.summary(days=30, skill="alpha")
    assert scoped["tool_events"] == 1 and scoped["tool_actions"] == 2



@pytest.mark.parametrize(
    "status,result,error_type,expected_error,expected_status,known",
    [
        ("ok", {"success": False, "error": "rollback"}, None, False, "ok", True),
        ("error", {"success": True}, None, True, "error", True),
        ("blocked", {"success": True}, None, True, "blocked", True),
        ("cancelled", "completed successfully", None, True, "cancelled", True),
        (None, {"exit_code": 0, "failed": 0, "error": "", "success": True}, None, False, None, False),
        (None, {"exit_code": 0, "failures": 0, "message": "no errors"}, None, False, None, False),
        (None, {"exit_code": 1, "success": True}, None, True, None, False),
        (None, {"success": False, "error": ""}, None, True, None, False),
        (None, {"exit_code": 0, "error": 0, "failed": 0}, None, False, None, False),
        (None, {"exit_code": 0, "error": [], "failure": ""}, None, False, None, False),
        (None, {"error": {"code": 0, "message": ""}}, None, False, None, False),
        (None, {"failed": 1}, None, True, None, False),
        (None, {"exit_code": "0", "success": True}, None, False, None, False),
        (None, "{malformed json with error word", None, False, None, False),
        ("future-status", {"success": True}, None, False, "unknown", False),
        (None, {"success": True}, "PolicyError", True, None, False),
    ],
)
def test_event_outcome_precedence_is_explicit(
    tmp_path, status, result, error_type, expected_error, expected_status, known
):
    store = EvidenceStore(tmp_path / "outcomes.sqlite")
    store.record_tool_call(
        tool_name="terminal",
        args={},
        result=result,
        status=status,
        error_type=error_type,
    )
    row = store.recent_tool_events(days=30, limit=1)[0]
    assert row["is_error"] is int(expected_error)
    assert row["status"] == expected_status
    assert row["outcome_known"] is known


def test_batch_action_success_requires_whole_and_each_operation_to_succeed(tmp_path):
    store = EvidenceStore(tmp_path / "actions.sqlite")
    args = {
        "operations": [
            {"name": "alpha", "action": "create"},
            {"name": "beta", "action": "patch"},
        ]
    }
    store.record_tool_call(
        tool_name="skill_manage",
        args=args,
        result={
            "success": True,
            "results": [{"success": True}, {"success": False}],
            "failures": 0,
        },
    )
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT skill_name, action, succeeded FROM tool_event_actions ORDER BY action_index"
        ).fetchall()
    assert [(r["skill_name"], r["action"], r["succeeded"]) for r in rows] == [
        ("alpha", "create", None),
        ("beta", "patch", None),
    ]


@pytest.mark.parametrize(
    "status,result,expected",
    [
        ("ok", {"success": True, "results": [{"success": True}, {"success": True}]}, [1, 1]),
        (None, {"success": True, "results": [{"success": True}, {"success": True}]}, [1, 1]),
        ("ok", {"success": False, "results": [{"success": True}, {"success": True}]}, [None, None]),
        ("ok", {"success": True, "results": [{"success": True}]}, [None, None]),
        ("error", {"success": True, "results": [{"success": True}, {"success": True}]}, [None, None]),
    ],
)
def test_action_success_requires_unambiguous_complete_result(tmp_path, status, result, expected):
    store = EvidenceStore(tmp_path / "actions.sqlite")
    store.record_tool_call(
        tool_name="skill_manage",
        args={"operations": [
            {"name": "alpha", "action": "create"},
            {"name": "beta", "action": "patch"},
        ]},
        result=result,
        status=status,
    )
    with store.connect() as conn:
        actual = [row[0] for row in conn.execute(
            "SELECT succeeded FROM tool_event_actions ORDER BY action_index"
        )]
    assert actual == expected


def test_batch_actions_are_visible_by_skill_without_multiplying_events(tmp_path):
    store = EvidenceStore(tmp_path / "visibility.sqlite")
    store.record_tool_call(
        tool_name="skill_manage",
        args={"operations": [
            {"name": "alpha", "action": "create"},
            {"name": "beta", "action": "patch"},
        ]},
        result={"success": True, "results": [{"success": True}, {"success": True}]},
    )
    rows = store.recent_tool_events(days=30, skill="beta", limit=10)
    assert len(rows) == 1
    assert rows[0]["actions"] == [{"skill_name": "beta", "action": "patch", "succeeded": 1}]
    assert store.summary(days=30)["tool_events"] == 1
    assert store.summary(days=30, skill="beta")["tool_actions"] == 1


def test_legacy_read_without_action_table_keeps_event_readable(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE tool_events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL, tool_name TEXT NOT NULL, duration_ms INTEGER, is_error INTEGER NOT NULL, skill_name TEXT, args_json TEXT NOT NULL, result_preview TEXT NOT NULL);"
            "INSERT INTO tool_events VALUES (1, '2026-01-01T00:00:00+00:00', 's', 't', 'terminal', NULL, 0, NULL, '{}', '');"
        )
    store = EvidenceStore(db, read_only=True)
    assert store.recent_tool_events(days=9999, limit=1)[0]["actions"] == []
