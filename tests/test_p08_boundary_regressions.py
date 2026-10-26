"""Adversarial checks at the full hook/import boundary."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import threading

from hermes_curator_evolver import backfill, hooks
from hermes_curator_evolver.storage import EvidenceStore


def _source(tmp_path, messages, **metadata):
    folder = tmp_path / "sessions"
    folder.mkdir()
    payload = {"session_id": "s", "session_start": datetime.now(timezone.utc).isoformat(),
               "messages": messages, **metadata}
    (folder / "session_s.json").write_text(json.dumps(payload), encoding="utf-8")
    return folder


def test_native_replay_is_safe_when_backfill_precedes_live_hook(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path / "events.sqlite")
    folder = _source(tmp_path, [
        {"role": "assistant", "id": "m1", "turn_id": "t1", "api_request_id": "r1", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ])
    backfill.backfill_sessions(sessions_dir=folder, store=store)
    monkeypatch.setattr(hooks, "_store", lambda: store)
    hooks.on_post_tool_call(tool_name="terminal", args={}, result="ok", session_id="s",
        task_id="native-task", tool_call_id="c1", turn_id="t1", api_request_id="r1")
    assert store.summary(days=30)["tool_events"] == 1


def test_concurrent_full_import_does_not_duplicate_session_end(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path / "events.sqlite")
    folder = _source(tmp_path, [])
    gate = threading.Barrier(2)
    original = backfill._session_event_exists
    def synchronized_check(*args, **kwargs):
        exists = original(*args, **kwargs)
        gate.wait(timeout=10)
        return exists
    monkeypatch.setattr(backfill, "_session_event_exists", synchronized_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: backfill.backfill_sessions(sessions_dir=folder, store=store), range(2)))
    assert store.summary(days=30)["session_events"] == 1
    assert sum(r["events_imported"] for r in results) == 1
    assert sum(r["events_duplicates"] for r in results) == 1


def test_concurrent_hook_and_backfill_native_tool_replay_insert_one(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path / "events.sqlite")
    folder = _source(tmp_path, [
        {"role": "assistant", "id": "m1", "turn_id": "t1", "api_request_id": "r1", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
    ])
    monkeypatch.setattr(hooks, "_store", lambda: store)
    gate = threading.Barrier(2)
    original_record = store.record_tool_call
    def synchronized_record(**kwargs):
        gate.wait(timeout=10)
        return original_record(**kwargs)
    monkeypatch.setattr(store, "record_tool_call", synchronized_record)
    with ThreadPoolExecutor(max_workers=2) as pool:
        hook_future = pool.submit(hooks.on_post_tool_call, tool_name="terminal", args={}, result="ok",
            session_id="s", task_id="native-task", tool_call_id="c1", turn_id="t1", api_request_id="r1")
        import_future = pool.submit(backfill.backfill_sessions, sessions_dir=folder, store=store)
        hook_future.result(timeout=15)
        imported = import_future.result(timeout=15)
    assert store.summary(days=30)["tool_events"] == 1
    assert imported["tool_events_imported"] + imported["events_duplicates"] == 1


def test_explicit_event_id_cannot_collide_with_a_tagged_turn_id(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path / "events.sqlite")
    monkeypatch.setattr(hooks, "_store", lambda: store)
    hooks.on_post_llm_call(session_id="s", event_id="turn:turn-one", user_message="first",
                          assistant_response="first answer", model="model", platform="cli")
    folder = _source(tmp_path, [
        {"role": "user", "content": "second"},
        {"role": "assistant", "id": "message-two", "turn_id": "turn-one", "content": "second answer"},
    ], model="model", platform="cli")
    backfill.backfill_sessions(sessions_dir=folder, store=store)
    assert store.summary(days=30)["turn_events"] == 2
