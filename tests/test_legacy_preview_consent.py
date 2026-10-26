"""Collection preference must never become consent to publish old stored content."""
import json
from hermes_curator_evolver.storage import EvidenceStore
from hermes_curator_evolver.reports import build_report, format_json_report


def test_collection_opt_in_does_not_expose_legacy_previews(tmp_path, monkeypatch):
    db = tmp_path / "evidence.sqlite"
    store = EvidenceStore(db)
    store.record_tool_call(tool_name="terminal", args={}, result="ok")
    store.record_turn(session_id="synthetic", user_message="", assistant_response="")
    private = "old-private-content-with-no-credential-shape"
    with store.connect() as conn:
        conn.execute("UPDATE tool_events SET args_json=?, result_preview=?", (json.dumps({"command": private}), private))
        conn.execute("UPDATE turn_events SET user_preview=?, assistant_preview=?", (private, private))
    before = db.read_bytes()
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_COLLECT_PREVIEWS", "1")
    report = build_report(EvidenceStore(db, read_only=True), days=7)
    assert private not in format_json_report(report)
    assert report["privacy"]["content_previews"] is False
    assert db.read_bytes() == before


def test_cli_requires_separate_legacy_read_consent_and_still_scrubs(tmp_path, monkeypatch, capsys):
    from hermes_curator_evolver.__main__ import main

    db = tmp_path / "evidence.sqlite"
    store = EvidenceStore(db)
    store.record_tool_call(tool_name="terminal", args={}, result="ok")
    private = "deliberately-requested-local-diagnostic-text"
    token = "ghp_" + "G" * 40
    with store.connect() as conn:
        conn.execute("UPDATE tool_events SET result_preview=?", (private + " " + token,))
    before = db.read_bytes()
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(db))
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_COLLECT_PREVIEWS", "1")
    assert main(["report", "--format", "json"]) == 0
    assert private not in capsys.readouterr().out
    assert main(["report", "--include-legacy-previews", "--format", "json"]) == 0
    result = capsys.readouterr().out
    assert private in result and token not in result
    assert json.loads(result)["privacy"]["legacy_preview_consent"] is True
    assert db.read_bytes() == before


def test_collection_opt_in_does_not_expose_legacy_queue_prose(tmp_path, monkeypatch):
    from hermes_curator_evolver.candidates import Candidate, CANDIDATE_TYPE_MEMORY
    from hermes_curator_evolver.review_queue import ReviewQueue

    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_COLLECT_PREVIEWS", "1")
    private = "old-private-queue-rationale"
    queue = ReviewQueue(tmp_path / "queue.sqlite")
    queue.enqueue(Candidate(candidate_type=CANDIDATE_TYPE_MEMORY, title=private, rationale=private,
                            confidence=0.9, evidence_refs=["synthetic"]))
    before = queue.db_path.read_bytes()
    assert private not in json.dumps(ReviewQueue(queue.db_path, create=False).list_page())
    assert private in json.dumps(ReviewQueue(queue.db_path, create=False, include_legacy_previews=True).list_page())
    assert queue.db_path.read_bytes() == before
