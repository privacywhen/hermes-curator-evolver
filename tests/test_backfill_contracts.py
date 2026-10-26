import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_curator_evolver.backfill import backfill_sessions
from hermes_curator_evolver.storage import EvidenceStore


def _state_source(tmp_path, monkeypatch, rows, messages=None, *, page_error=None):
    path = tmp_path / "state.db"
    path.touch()
    now = datetime.now(timezone.utc).timestamp()
    calls = {"search": [], "messages": []}

    class FakeSessionDB:
        def __init__(self, db_path, read_only=False):
            assert Path(db_path) == path
            assert read_only is True

        def search_sessions(self, source=None, limit=20, offset=0):
            calls["search"].append((limit, offset))
            if page_error and offset == page_error[0]:
                raise page_error[1]
            return rows[offset : offset + limit]

        def get_messages(self, session_id):
            calls["messages"].append(session_id)
            if isinstance(messages, dict) and isinstance(messages.get(session_id), Exception):
                raise messages[session_id]
            return (messages or {}).get(session_id, [])

        def close(self):
            calls["closed"] = True

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=FakeSessionDB))
    return path, calls, now


def _row(session_id, timestamp):
    return {"id": session_id, "last_active": timestamp, "started_at": timestamp}


def _tool_messages(call_id):
    return [
        {"role": "assistant", "id": f"m-{call_id}", "tool_calls": [{"id": call_id, "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": "ok"},
    ]


def test_backfill_source_error_does_not_export_private_exception_detail(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    path, _, _ = _state_source(tmp_path, monkeypatch, [_row(str(i), now) for i in range(200)],
                             page_error=(200, OSError("synthetic-private-exception-detail")))
    result = backfill_sessions(state_db=path, store=EvidenceStore(tmp_path / "e.sqlite"), days=30)
    assert "synthetic-private-exception-detail" not in json.dumps(result)
    assert result["source_error"] == "OSError"
    assert result["partial"] is True


def test_metadata_is_sorted_limited_and_cut_off_before_transcripts(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    rows = [_row("old", now - 40 * 86400), _row("newest", now), _row("middle", now - 60)]
    path, calls, _ = _state_source(tmp_path, monkeypatch, rows, {"newest": [], "middle": [], "old": []})

    result = backfill_sessions(state_db=path, store=EvidenceStore(tmp_path / "e.sqlite"), days=30, limit=1)

    assert calls["messages"] == ["newest"]
    assert result["sessions_selected"] == 1
    assert result["sessions_skipped_old"] == 1


def test_repeated_metadata_page_terminates_and_reports_truncation(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    path.touch()
    now = datetime.now(timezone.utc).timestamp()
    calls = []

    class RepeatingDB:
        def __init__(self, db_path, read_only=False):
            assert read_only

        def search_sessions(self, source=None, limit=20, offset=0):
            calls.append(offset)
            # Emulate a broken API ignoring offset; keep the first page full.
            if offset == 0:
                return [_row(f"s{i}", now - i) for i in range(limit)]
            return [_row("s0", now)]

        def get_messages(self, session_id):
            return []

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=RepeatingDB))
    result = backfill_sessions(state_db=path, store=EvidenceStore(tmp_path / "e.sqlite"), days=30)

    assert len(calls) <= 2
    assert result["pagination_truncated"] is True
    assert result["sessions_seen"] == 200


def test_session_failure_does_not_abort_later_session_and_is_not_source_failure(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    rows = [_row("broken", now), _row("healthy", now - 1)]
    path, calls, _ = _state_source(tmp_path, monkeypatch, rows, {"broken": OSError("transcript unreadable"), "healthy": _tool_messages("good")})

    result = backfill_sessions(state_db=path, store=EvidenceStore(tmp_path / "e.sqlite"), days=30)

    assert calls["messages"] == ["broken", "healthy"]
    assert result["sessions_failed"] == 1
    assert result["source_failures"] == 0
    assert result["source_error"] is None
    assert result["tool_events_imported"] == 1


def test_source_failure_is_separate_and_preserves_prior_counts(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    rows = [_row(f"old-{i}", now - 90 * 86400 - i) for i in range(199)] + [_row("good", now)]
    path, calls, _ = _state_source(tmp_path, monkeypatch, rows, {"good": _tool_messages("one")}, page_error=(200, OSError("metadata read failed")))

    result = backfill_sessions(state_db=path, store=EvidenceStore(tmp_path / "e.sqlite"), days=30)

    assert result["source_failures"] == 1
    assert result["sessions_failed"] == 0
    assert result["tool_events_imported"] == 1
    assert result["source_error"]


def test_event_accounting_reconciles_duplicates_and_keeps_partial_writes(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    payload = {"session_id": "s", "session_start": datetime.now(timezone.utc).isoformat(), "messages": _tool_messages("c1") + _tool_messages("c2")}
    (sessions / "session_s.json").write_text(json.dumps(payload), encoding="utf-8")
    store = EvidenceStore(tmp_path / "e.sqlite")
    original = store.record_tool_call
    count = 0

    def fail_second(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("write failed")
        return original(**kwargs)

    store.record_tool_call = fail_second
    first = backfill_sessions(sessions_dir=sessions, store=store, days=30)
    assert first["sessions_failed"] == 1
    assert first["tool_events_imported"] == 1
    assert first["events_attempted"] == first["events_imported"] + first["events_duplicates"] + first["events_failed"]

    store.record_tool_call = original
    retry = backfill_sessions(sessions_dir=sessions, store=store, days=30)
    assert retry["events_duplicates"] >= 1
    assert retry["events_attempted"] == retry["events_imported"] + retry["events_duplicates"] + retry["events_failed"]
    assert retry["partial"] is False


def test_zero_limit_remains_explicitly_rejected(tmp_path):
    with pytest.raises(ValueError, match="limit must be positive"):
        backfill_sessions(sessions_dir=tmp_path, store=EvidenceStore(tmp_path / "e.sqlite"), limit=0)


def test_native_tool_identity_and_status_are_forwarded(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    payload = {
        "session_id": "native-session",
        "session_start": datetime.now(timezone.utc).isoformat(),
        "messages": [
            {"role": "assistant", "id": "message-1", "turn_id": "turn-1", "api_request_id": "request-1", "tool_calls": [{
                "id": "call-1", "tool_call_id": "native-call-1", "function": {"name": "terminal", "arguments": "{}"},
                "status": "blocked", "error_type": "policy",
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": "denied"},
        ],
    }
    (sessions / "session_native.json").write_text(json.dumps(payload), encoding="utf-8")
    store = EvidenceStore(tmp_path / "e.sqlite")
    captured = []
    original = store.record_tool_call

    def capture(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(store, "record_tool_call", capture)
    backfill_sessions(sessions_dir=sessions, store=store, days=30)

    assert captured[0]["tool_call_id"] == "native-call-1"
    assert captured[0]["turn_id"] == "turn-1"
    assert captured[0]["api_request_id"] == "request-1"
    assert captured[0]["status"] == "blocked"
    assert captured[0]["error_type"] == "policy"
