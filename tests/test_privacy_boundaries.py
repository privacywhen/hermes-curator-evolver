"""Synthetic-only end-to-end privacy boundary regressions."""
import json
import pytest

from hermes_curator_evolver.storage import EvidenceStore


def test_default_collection_omits_unknown_private_payloads(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_CURATOR_EVOLVER_COLLECT_PREVIEWS", raising=False)
    store = EvidenceStore(tmp_path / "evidence.sqlite")
    opaque = "private-client-project-without-any-secret-shaped-pattern"
    store.record_tool_call(tool_name="terminal", args={"command": opaque},
                           result={"success": False, "output": opaque})
    store.record_turn(session_id="synthetic", user_message=opaque, assistant_response=opaque)
    with store.connect() as conn:
        tool = dict(conn.execute("SELECT * FROM tool_events").fetchone())
        turn = dict(conn.execute("SELECT * FROM turn_events").fetchone())
    assert opaque not in json.dumps([tool, turn])
    assert tool["is_error"] == 1
    assert json.loads(tool["result_preview"])["success"] is False
    assert turn["user_preview"] == turn["assistant_preview"] == ""


def test_opted_in_storage_scrubs_before_cut_and_serialization(tmp_path):
    secret = "ghp_" + "A" * 40
    store = EvidenceStore(tmp_path / "evidence.sqlite", preview_chars=50, collect_previews=True)
    store.record_tool_call(tool_name="terminal", args={"password": "opaque-secret"},
                           result="x" * 40 + secret)
    store.record_turn(session_id="synthetic", user_message=secret, assistant_response=secret)
    with store.connect() as conn:
        tool = dict(conn.execute("SELECT * FROM tool_events").fetchone())
        turn = dict(conn.execute("SELECT * FROM turn_events").fetchone())
    rendered = json.dumps([tool, turn])
    assert "opaque-secret" not in rendered
    assert "ghp_" not in rendered
    assert "[REDACTED" in rendered
    assert len(tool["result_preview"]) <= 50


def test_legacy_rows_are_minimized_at_read_without_rewriting_history(tmp_path):
    from hermes_curator_evolver.reports import build_report, format_json_report

    db = tmp_path / "evidence.sqlite"
    store = EvidenceStore(db)
    store.record_tool_call(tool_name="skill_view", args={"name": "synthetic"}, result="ok")
    store.record_turn(session_id="synthetic", user_message="", assistant_response="")
    private = "legacy-private-project-name"
    token = "ghp_" + "B" * 40
    with store.connect() as conn:
        conn.execute("UPDATE tool_events SET args_json=?, result_preview=?", (json.dumps({"password": token}), private + token))
        conn.execute("UPDATE turn_events SET user_preview=?, assistant_preview=?", (private, token))
    before = db.read_bytes()
    rendered = format_json_report(build_report(EvidenceStore(db, read_only=True), days=7))
    assert private not in rendered
    assert token not in rendered
    assert db.read_bytes() == before


@pytest.mark.parametrize("sink", ["report", "proposal", "query", "note", "spillover"])
def test_raw_legacy_input_is_sanitized_again_at_each_publication_boundary(sink):
    from hermes_curator_evolver import auto_evolve, proposals, reports

    secret = "ghp_" + "C" * 40
    private = "opaque-legacy-content-not-for-publication"
    row = {"created_at": "synthetic", "skill_name": "synthetic", "tool_name": "terminal",
           "result_preview": private + " " + secret, "is_error": 1}
    report = {"summary": {"skills": [{"skill_name": "synthetic", "event_count": 1}]},
              "skill_evidence": [row]}
    if sink == "report":
        output = reports.format_json_report(report)
    elif sink == "proposal":
        output = proposals.format_proposal_json(proposals.build_skill_proposal(report, skill_name="synthetic", skill_text=secret))
    elif sink == "query":
        output = auto_evolve._build_semantic_query(report, eligible_names={"synthetic"})
    elif sink == "note":
        output = "\n".join(auto_evolve._format_evidence_rows([row]))
    else:
        output = auto_evolve._format_evidence_reference(skill_name="synthetic", generated_at="synthetic", days=7,
                                                      summary={}, evidence_rows=[row])
    assert secret not in output
    assert private not in output


def test_diagnostic_boundaries_do_not_repeat_secrets(monkeypatch, caplog, capsys):
    from hermes_curator_evolver import hooks, cli
    from hermes_curator_evolver.__main__ import build_parser

    secret = "ghp_" + "D" * 40
    def fail(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(hooks, "_store", fail)
    hooks.on_post_tool_call(tool_name="terminal")
    assert secret not in caplog.text
    monkeypatch.setattr(cli, "_dispatch_cli", fail)
    assert cli.handle_cli(build_parser().parse_args(["status"])) == 1
    assert secret not in capsys.readouterr().out


def test_ranking_sanitizes_query_and_skill_before_fake_backend(tmp_path):
    from hermes_curator_evolver.semantic import find_skill_candidates

    secret = "ghp_" + "E" * 40
    skill = tmp_path / "synthetic" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text(secret, encoding="utf-8")
    class FakeEncoder:
        def encode(self, texts, **kwargs):
            assert secret not in " ".join(texts)
            return [[1.0] for _ in texts]
    result = find_skill_candidates(query=secret, skills_dir=tmp_path, semantic=True, embedding_backend=FakeEncoder())
    assert result["model_executed"] is True
    assert secret not in json.dumps(result)


def test_required_sanitization_failure_blocks_publication(monkeypatch):
    from hermes_curator_evolver import privacy, reports

    def fail(value):
        raise RuntimeError("synthetic sanitizer failure")
    monkeypatch.setattr(privacy, "scrub_value", fail)
    with pytest.raises(RuntimeError, match="sanitizer failure"):
        reports.format_json_report({"skill_evidence": [{"result_preview": "private"}]})


def test_credential_shaped_metadata_is_sanitized_before_persistence(tmp_path):
    secret = "ghp_" + "F" * 40
    store = EvidenceStore(tmp_path / "evidence.sqlite")
    store.record_tool_call(tool_name=secret, args={"skills": [secret]}, result="ok",
                           task_id=secret, session_id=secret)
    store.record_turn(session_id=secret, model=secret, platform=secret, user_message="", assistant_response="")
    store.record_session_end(session_id=secret, completed=True, interrupted=False, model=secret, platform=secret)
    assert secret.encode() not in store.db_path.read_bytes()


def test_metadata_only_backfill_preserves_distinct_turn_counts_and_replay(tmp_path):
    from hermes_curator_evolver.backfill import backfill_sessions

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "session_synthetic.json").write_text(json.dumps({
        "session_id": "synthetic", "messages": [
            {"role": "user", "content": "private first"}, {"role": "assistant", "content": "private reply one"},
            {"role": "user", "content": "private second"}, {"role": "assistant", "content": "private reply two"},
        ]}), encoding="utf-8")
    store = EvidenceStore(tmp_path / "evidence.sqlite")
    assert backfill_sessions(sessions_dir=sessions, store=store)["turn_events_imported"] == 2
    assert backfill_sessions(sessions_dir=sessions, store=store)["turn_events_imported"] == 0
    assert b"private first" not in store.db_path.read_bytes()
