"""P08 native identity values remain distinct after bounded privacy handling."""
from datetime import datetime, timezone
import json

import pytest

from hermes_curator_evolver import backfill, hooks
from hermes_curator_evolver.storage import EvidenceStore


@pytest.mark.parametrize("field", ["tool_call_id", "turn_id", "api_request_id"])
@pytest.mark.parametrize("make_values", [
    lambda: ("X" * 256 + "A", "X" * 256 + "B"),
    lambda: ("ghp_" + "A" * 36, "ghp_" + "B" * 36),
])
def test_native_identity_fields_distinguish_bounded_or_scrubbed_values(tmp_path, field, make_values):
    store = EvidenceStore(tmp_path / "native.sqlite")
    first, second = make_values()
    base = dict(tool_name="terminal", args={}, result="ok", session_id="s", tool_call_id="call")
    base[field] = first
    assert store.record_tool_call(**base, atomic_dedupe=True)
    assert not store.record_tool_call(**base, atomic_dedupe=True)
    base[field] = second
    assert store.record_tool_call(**base, atomic_dedupe=True)
    assert not store.record_tool_call(**base, atomic_dedupe=True)
    assert store.summary(days=30)["tool_events"] == 2
    raw = store.db_path.read_bytes()
    assert first.encode() not in raw and second.encode() not in raw
    rows = store.recent_tool_events(days=30)
    assert first not in repr(rows) and second not in repr(rows)


def test_backfill_native_ids_do_not_collide_or_persist_raw_credentials(tmp_path):
    store = EvidenceStore(tmp_path / "backfill.sqlite")
    folder = tmp_path / "sessions"
    folder.mkdir()
    a, b = "ghp_" + "A" * 36, "ghp_" + "B" * 36
    data = {"session_id": "s", "session_start": datetime.now(timezone.utc).isoformat(), "messages": [
        {"role": "assistant", "id": "m", "turn_id": "turn", "tool_calls": [
            {"id": a, "function": {"name": "terminal", "arguments": "{}"}},
            {"id": b, "function": {"name": "terminal", "arguments": "{}"}},
        ]}]}
    (folder / "session_s.json").write_text(json.dumps(data), encoding="utf-8")
    first = backfill.backfill_sessions(sessions_dir=folder, store=store)
    replay = backfill.backfill_sessions(sessions_dir=folder, store=store)
    assert first["tool_events_imported"] == 2
    assert replay["tool_events_imported"] == 0 and replay["events_duplicates"] >= 2
    assert store.summary(days=30)["tool_events"] == 2
    raw = store.db_path.read_bytes()
    assert a.encode() not in raw and b.encode() not in raw
    assert a not in repr(store.recent_tool_events(days=30)) and b not in repr(store.recent_tool_events(days=30))


def test_hook_native_long_ids_distinguish_calls(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path / "hooks.sqlite")
    monkeypatch.setattr(hooks, "_store", lambda: store)
    for suffix in ("A", "B", "A", "B"):
        hooks.on_post_tool_call(tool_name="terminal", args={}, result="ok", session_id="s",
                                task_id="same", tool_call_id="X" * 256 + suffix,
                                turn_id="turn", api_request_id="request")
    assert store.summary(days=30)["tool_events"] == 2
