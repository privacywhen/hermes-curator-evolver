"""Correlation groups cannot stand in for absent invocation identities."""
from datetime import datetime, timezone
import json

from hermes_curator_evolver import backfill, hooks
from hermes_curator_evolver.storage import EvidenceStore


def test_hooks_keep_idless_calls_in_same_turn_and_request(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path / "events.sqlite")
    monkeypatch.setattr(hooks, "_store", lambda: store)
    for _ in range(2):
        hooks.on_post_tool_call(tool_name="terminal", args={}, result="ok", session_id="s",
                               task_id="user-task", turn_id="turn", api_request_id="request")
    assert store.summary(days=30)["tool_events"] == 2


def test_backfill_idless_calls_share_correlation_but_keep_call_positions(tmp_path):
    store = EvidenceStore(tmp_path / "events.sqlite")
    folder = tmp_path / "sessions"
    folder.mkdir()
    data = {"session_id": "s", "session_start": datetime.now(timezone.utc).isoformat(), "messages": [
        {"role": "assistant", "id": "m", "turn_id": "turn", "api_request_id": "request", "tool_calls": [
            {"function": {"name": "terminal", "arguments": "{}"}},
            {"function": {"name": "terminal", "arguments": "{}"}}]}]}
    (folder / "session_s.json").write_text(json.dumps(data), encoding="utf-8")
    first = backfill.backfill_sessions(sessions_dir=folder, store=store)
    replay = backfill.backfill_sessions(sessions_dir=folder, store=store)
    assert first["tool_events_imported"] == 2
    assert replay["tool_events_imported"] == 0 and replay["events_duplicates"] >= 2
    assert store.summary(days=30)["tool_events"] == 2
