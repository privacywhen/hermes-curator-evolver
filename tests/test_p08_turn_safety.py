"""Native turn replay remains symmetric without weakening identifier privacy."""
from datetime import datetime, timezone
import json
import pytest

from hermes_curator_evolver import backfill, hooks
from hermes_curator_evolver.storage import EvidenceStore


@pytest.mark.parametrize("hook_first", [True, False])
def test_native_turn_replay_is_symmetric(tmp_path, monkeypatch, hook_first):
    store = EvidenceStore(tmp_path / "events.sqlite", collect_previews=False)
    monkeypatch.setattr(hooks, "_store", lambda: store)
    folder = tmp_path / "sessions"
    folder.mkdir()
    (folder / "session_s.json").write_text(json.dumps({
        "session_id": "s", "session_start": datetime.now(timezone.utc).isoformat(),
        "messages": [{"role": "user", "content": "u"},
                     {"role": "assistant", "turn_id": "t", "id": "m", "content": "a"}]}), encoding="utf-8")
    def hook():
        hooks.on_post_llm_call(session_id="s", turn_id="t", user_message="u", assistant_response="a")
    def imported():
        backfill.backfill_sessions(sessions_dir=folder, store=store)
    for step in ((hook, imported) if hook_first else (imported, hook)):
        step()
    assert store.summary(days=30)["turn_events"] == 1


@pytest.mark.parametrize("event_id", ["ghp_" + "A" * 40, "opaque-" + "x" * 10000], ids=["sensitive", "oversized"])
def test_turn_replay_id_is_private_bounded_and_stable(tmp_path, event_id):
    store = EvidenceStore(tmp_path / "events.sqlite", collect_previews=False)
    args = dict(session_id="s", event_id=event_id, user_message="u", assistant_response="a", atomic_dedupe=True)
    assert store.record_turn(**args) is True
    assert store.record_turn(**args) is False
    with store.connect() as conn:
        stored = conn.execute("SELECT event_id FROM turn_events").fetchone()[0]
    assert len(stored) <= 512
    assert event_id.encode() not in store.db_path.read_bytes()
    assert store.summary(days=30)["turn_events"] == 1
