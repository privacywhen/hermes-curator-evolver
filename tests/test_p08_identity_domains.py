"""Acceptance boundaries for identity domains and untrusted stored metadata."""
import hashlib
import json

import pytest

from hermes_curator_evolver.storage import EvidenceStore


@pytest.mark.parametrize("field", ["session_id", "task_id", "tool_call_id", "turn_id", "api_request_id"])
def test_raw_digest_marker_cannot_alias_the_original_identifier(tmp_path, field):
    original = "X" * 300
    marker = json.dumps(["sha256", hashlib.sha256(original.encode()).hexdigest()], separators=(",", ":"))
    store = EvidenceStore(tmp_path / "domains.sqlite")
    base = dict(tool_name="terminal", args={}, result="ok", session_id="s", task_id="task", tool_call_id="call")
    if field == "task_id":
        base["tool_call_id"] = None  # Explicit source fallback, not native invocation identity.
    for value in (original, marker):
        base[field] = value
        assert store.record_tool_call(**base, atomic_dedupe=True) is True
        assert store.record_tool_call(**base, atomic_dedupe=True) is False
    assert store.summary(days=30)["tool_events"] == 2


@pytest.mark.parametrize("field", ["session_id", "task_id"])
@pytest.mark.parametrize("kind", ["long", "sensitive"])
def test_session_and_fallback_task_keys_are_not_lossy(tmp_path, field, kind):
    values = ["X" * 256 + suffix if kind == "long" else "ghp_" + suffix * 40 for suffix in ("A", "B")]
    store = EvidenceStore(tmp_path / "fallback.sqlite")
    base = dict(tool_name="terminal", args={}, result="ok", session_id="s", task_id="task")
    for value in values:
        base[field] = value
        assert store.record_tool_call(**base, atomic_dedupe=True) is True
        assert store.record_tool_call(**base, atomic_dedupe=True) is False
    assert store.summary(days=30)["tool_events"] == 2
    assert all(value.encode() not in store.db_path.read_bytes() for value in values)


@pytest.mark.parametrize("field", ["tool_call_id", "turn_id", "api_request_id", "event_id", "status", "error_type"])
def test_untrusted_stored_identity_is_minimized_without_rewriting(tmp_path, field):
    store = EvidenceStore(tmp_path / "legacy.sqlite", collect_previews=False)
    secret = "ghp_" + "A" * 40
    if field == "event_id":
        store.record_turn(session_id="s", user_message="u", assistant_response="a")
        table = "turn_events"
    else:
        store.record_tool_call(tool_name="terminal", args={}, result="ok", session_id="s")
        table = "tool_events"
    with store.connect() as conn:
        conn.execute(f"UPDATE {table} SET {field} = ?", (secret,))  # Fixed parameterized fixture vocabulary.
    before = store.db_path.read_bytes()
    rows = store.recent_turns(days=30) if field == "event_id" else store.recent_tool_events(days=30)
    assert secret not in json.dumps(rows)
    assert store.db_path.read_bytes() == before
