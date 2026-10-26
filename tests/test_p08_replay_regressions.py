import json
import threading
from pathlib import Path

from hermes_curator_evolver.backfill import backfill_sessions
from hermes_curator_evolver.hooks import on_post_llm_call, on_post_tool_call
from hermes_curator_evolver.storage import EvidenceStore


def test_invalid_utf8_is_counted_and_later_legacy_session_imports(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "session_bad.json").write_bytes(b"\xff\xfe")
    (sessions / "session_good.json").write_text(json.dumps({
        "session_id": "good", "session_start": "2026-05-01T10:00:00Z",
        "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "id": "a1", "content": "hello"}],
    }), encoding="utf-8")
    store = EvidenceStore(tmp_path / "evidence.sqlite")

    result = backfill_sessions(sessions_dir=sessions, store=store, days=365)

    assert result["files_failed"] == result["sessions_failed"] == 1
    assert result["partial"] is True
    assert result["turn_events_imported"] == 1
    assert result["sessions_imported"] == 1


def test_hook_native_tool_identity_dedupes_backfill_even_when_task_ids_differ(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    store = EvidenceStore(tmp_path / "evidence.sqlite")
    monkeypatch.setattr("hermes_curator_evolver.hooks._store", lambda: store)
    on_post_tool_call(tool_name="terminal", args={"command": "pwd"}, result="ok",
                      task_id="native-task", session_id="s", tool_call_id="c1", turn_id="t1", api_request_id="r1")
    payload = {"session_id": "s", "session_start": "2026-05-01T10:00:00Z", "messages": [
        {"role": "assistant", "id": "msg1", "turn_id": "t1", "api_request_id": "r1", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{\"command\": \"pwd\"}"}}
        ]}
    ]}
    (sessions / "session_s.json").write_text(json.dumps(payload), encoding="utf-8")

    result = backfill_sessions(sessions_dir=sessions, store=store, days=365)
    assert result["tool_events_imported"] == 0
    assert result["events_duplicates"] == 1
    assert store.summary(days=365)["tool_events"] == 1


def test_hook_turn_identity_is_forwarded_and_backfill_matches_without_previews(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    store = EvidenceStore(tmp_path / "evidence.sqlite", collect_previews=False)
    monkeypatch.setattr("hermes_curator_evolver.hooks._store", lambda: store)
    on_post_llm_call(session_id="s", user_message="u", assistant_response="a", model="m", platform="p", turn_id="turn-1")
    payload = {"session_id": "s", "session_start": "2026-05-01T10:00:00Z", "messages": [
        {"role": "user", "content": "u"}, {"role": "assistant", "id": "msg-1", "turn_id": "turn-1", "content": "a"},
        {"role": "user", "content": "u2"}, {"role": "assistant", "id": "msg-2", "turn_id": "turn-2", "content": "a2"}
    ]}
    (sessions / "session_s.json").write_text(json.dumps(payload), encoding="utf-8")

    result = backfill_sessions(sessions_dir=sessions, store=store, days=365)
    assert result["turn_events_imported"] == 1
    assert result["events_duplicates"] == 1
    assert store.summary(days=365)["turn_events"] == 2


def test_concurrent_native_tool_replay_inserts_one_row(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.sqlite")
    barrier = threading.Barrier(2)
    outcomes = []
    def record(task):
        barrier.wait()
        outcomes.append(store.record_tool_call(tool_name="terminal", args={}, result="ok", task_id=task,
            session_id="s", tool_call_id="c", turn_id="t", api_request_id="r", atomic_dedupe=True))
    threads = [threading.Thread(target=record, args=(f"task-{i}",)) for i in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes) == [False, True]
    assert store.summary(days=365)["tool_events"] == 1


def test_native_tool_identity_keeps_distinct_correlations_and_turn_ids(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.sqlite")
    base = dict(tool_name="terminal", args={}, result="ok", session_id="s", tool_call_id="c")
    assert store.record_tool_call(**base, task_id="task-a", turn_id="t1", api_request_id="r1", atomic_dedupe=True)
    assert not store.record_tool_call(**base, task_id="task-b", turn_id="t1", api_request_id="r1", atomic_dedupe=True)
    assert store.record_tool_call(**base, task_id="task-c", turn_id="t2", api_request_id="r1", atomic_dedupe=True)
    assert store.record_tool_call(**base, task_id="task-d", turn_id="t1", api_request_id="r2", atomic_dedupe=True)
    assert store.summary(days=365)["tool_events"] == 3
