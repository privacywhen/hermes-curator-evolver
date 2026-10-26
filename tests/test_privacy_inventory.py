"""Bounded synthetic inventory and review-list contracts; never a deletion plan."""
from datetime import datetime, timedelta, timezone

from hermes_curator_evolver.storage import EvidenceStore
from hermes_curator_evolver.review_queue import ReviewQueue
from hermes_curator_evolver.candidates import Candidate, CANDIDATE_TYPE_MEMORY


def test_retention_inventory_counts_old_rows_without_mutation(tmp_path):
    from hermes_curator_evolver.inventory import privacy_inventory

    db = tmp_path / "evidence.sqlite"
    store = EvidenceStore(db)
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    store.record_tool_call(tool_name="terminal", args={}, result="private", created_at=old)
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "snapshot.txt").write_text("private backup", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in (db, backups / "snapshot.txt")}
    result = privacy_inventory(db, backup_dir=backups, retention_days=30, file_limit=1)
    assert result["tables"]["tool_events"]["older_than_window"] == 1
    assert result["backups"]["files_observed"] == 1
    assert result["deletion_supported"] is False
    assert result["dry_run"] is True
    assert {p.name: p.read_bytes() for p in (db, backups / "snapshot.txt")} == before


def test_review_pages_are_bounded_and_metadata_is_honest(tmp_path):
    queue = ReviewQueue(tmp_path / "queue.sqlite")
    for index in range(3):
        queue.enqueue(Candidate(candidate_type=CANDIDATE_TYPE_MEMORY, title=str(index), rationale="synthetic",
                                confidence=0.9, evidence_refs=[str(index)]))
    before = queue.db_path.read_bytes()
    reader = ReviewQueue(queue.db_path, create=False)
    page = reader.list_page(limit=1, offset=1)
    assert len(page["candidates"]) == 1
    assert page["pagination"] == {"limit": 1, "offset": 1, "returned": 1, "has_more": True}
    assert reader.list_page(limit=0)["candidates"] == []
    assert queue.db_path.read_bytes() == before


def test_inventory_and_queue_pages_are_reachable_through_cli(tmp_path, monkeypatch, capsys):
    import json
    from hermes_curator_evolver.__main__ import main

    db = tmp_path / "evidence.sqlite"
    EvidenceStore(db)
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(db))
    assert main(["privacy-inventory", "--file-limit", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    queue = ReviewQueue(tmp_path / "queue.sqlite")
    assert main(["candidates-list", "--queue-db", str(queue.db_path), "--limit", "0", "--offset", "0", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["pagination"]["returned"] == 0


def test_report_discloses_sampling_and_preview_policy(tmp_path):
    from hermes_curator_evolver.reports import build_report

    store = EvidenceStore(tmp_path / "evidence.sqlite")
    for _ in range(21):
        store.record_tool_call(tool_name="terminal", args={}, result="omitted")
    report = build_report(EvidenceStore(store.db_path, read_only=True), days=7)
    assert report["sampling"]["tool_rows"] == {"limit": 20, "returned": 20, "truncated": True}
    assert report["privacy"]["content_previews"] is False


def test_queue_and_mining_export_omit_unshaped_content_by_default(tmp_path, monkeypatch):
    import json
    from hermes_curator_evolver.cli import _candidate_to_payload

    monkeypatch.delenv("HERMES_CURATOR_EVOLVER_COLLECT_PREVIEWS", raising=False)
    private = "opaque-candidate-content-from-a-private-session"
    candidate = Candidate(candidate_type=CANDIDATE_TYPE_MEMORY, title=private, rationale=private,
                          confidence=0.9, evidence_refs=["synthetic"], metadata={"payload": private})
    queue = ReviewQueue(tmp_path / "queue.sqlite")
    queue.enqueue(candidate)
    assert private.encode() not in queue.db_path.read_bytes()
    assert private not in json.dumps(_candidate_to_payload(candidate))
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_COLLECT_PREVIEWS", "1")
    assert _candidate_to_payload(candidate)["title"] == private
