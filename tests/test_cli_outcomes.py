"""CLI exit outcomes at synthetic effect boundaries and standalone subprocesses."""

import json
import os
import subprocess
import sys
from pathlib import Path

import hermes_curator_evolver
from hermes_curator_evolver.__main__ import build_parser
from hermes_curator_evolver.cli import handle_cli


class FakeCtx:
    def __init__(self):
        self.cli_commands = []

    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands.append((name, setup_fn, handler_fn))

    def register_command(self, *args, **kwargs):
        pass

    def register_skill(self, *args, **kwargs):
        pass


def _args(*argv):
    return build_parser().parse_args(list(argv))


def _json_stdout(capsys):
    return json.loads(capsys.readouterr().out)


def test_native_registered_handler_propagates_nonzero_integer(monkeypatch):
    ctx = FakeCtx()
    hermes_curator_evolver.register(ctx)
    assert len(ctx.cli_commands) == 1
    _, setup_fn, handler = ctx.cli_commands[0]
    assert setup_fn is not None
    monkeypatch.setattr("hermes_curator_evolver.cli._dispatch_cli", lambda args: 23)

    result = handler(_args("verify", "--proposal-file", "unused.json"))

    assert result == 23


def test_verify_missing_source_standalone_exits_nonzero(tmp_path):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "isolated-hermes")
    env["HERMES_CURATOR_EVOLVER_DB"] = str(tmp_path / "evidence.sqlite")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    missing = tmp_path / "absent-proposal.json"

    proc = subprocess.run(
        [sys.executable, "-m", "hermes_curator_evolver", "verify", "--proposal-file", str(missing)],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, check=False,
    )

    assert proc.returncode != 0
    payload = json.loads(proc.stdout)
    assert payload["success"] is False
    assert "error" in payload


def test_verify_corrupt_source_standalone_exits_nonzero(tmp_path):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "isolated-hermes")
    env["HERMES_CURATOR_EVOLVER_DB"] = str(tmp_path / "evidence.sqlite")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    corrupt = tmp_path / "corrupt-proposal.json"
    corrupt.write_text("{not valid json", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "hermes_curator_evolver", "verify", "--proposal-file", str(corrupt)],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, check=False,
    )

    assert proc.returncode != 0
    payload = json.loads(proc.stdout)
    assert payload["success"] is False
    assert "error" in payload


def test_apply_refusal_standalone_exits_nonzero_without_changing_target(tmp_path):
    target = tmp_path / "skill.md"
    target.write_text("original\n", encoding="utf-8")
    content = tmp_path / "proposed.md"
    content.write_text("new content\n", encoding="utf-8")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "isolated-hermes")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    proc = subprocess.run(
        [sys.executable, "-m", "hermes_curator_evolver", "apply", "--target", str(target),
         "--content-file", str(content), "--expected-sha256", "not-a-real-hash"],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, check=False,
    )

    assert proc.returncode != 0
    assert json.loads(proc.stdout)["applied"] is False
    assert target.read_text(encoding="utf-8") == "original\n"
    assert not (tmp_path / ".curator-evolver-backups").exists()


def test_verify_false_verdict_exits_nonzero(monkeypatch, capsys, tmp_path):
    proposal = tmp_path / "proposal.json"
    proposal.write_text('{"proposal": true}', encoding="utf-8")
    monkeypatch.setattr("hermes_curator_evolver.cli.build_default_report", lambda **kwargs: {})
    monkeypatch.setattr("hermes_curator_evolver.cli.verify_proposal", lambda *args: {"passed": False, "failures": ["synthetic"]})

    rc = handle_cli(_args("verify", "--proposal-file", str(proposal), "--format", "json"))

    assert rc != 0
    assert _json_stdout(capsys)["passed"] is False


def test_rollback_failure_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr("hermes_curator_evolver.cli.rollback_guarded_patch", lambda *a, **k: {"rolled_back": False, "error": "synthetic"})

    rc = handle_cli(_args("rollback", "--manifest", "synthetic-manifest.json"))

    assert rc != 0
    assert _json_stdout(capsys)["rolled_back"] is False


def test_restore_drill_failure_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr("hermes_curator_evolver.cli.run_restore_drill", lambda *a, **k: {"status": "fail", "error": "synthetic"})
    monkeypatch.setattr("hermes_curator_evolver.cli.format_drill_report", lambda report, **k: json.dumps(report))

    rc = handle_cli(_args("restore-drill", "--manifest", "synthetic-manifest.json"))

    assert rc != 0
    assert _json_stdout(capsys)["status"] == "fail"


def test_restore_drill_partial_warning_is_success(monkeypatch, capsys):
    monkeypatch.setattr("hermes_curator_evolver.cli.run_restore_drill", lambda *a, **k: {"status": "warning", "partial": True})
    monkeypatch.setattr("hermes_curator_evolver.cli.format_drill_report", lambda report, **k: json.dumps(report))

    rc = handle_cli(_args("restore-drill", "--manifest", "synthetic-manifest.json"))

    assert rc == 0
    assert _json_stdout(capsys)["status"] == "warning"


def test_backfill_missing_source_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr("hermes_curator_evolver.cli.backfill_sessions", lambda **k: {"missing": True, "source_path": "missing"})

    rc = handle_cli(_args("backfill-sessions", "--sessions-dir", "missing", "--format", "json"))

    assert rc != 0
    assert _json_stdout(capsys)["missing"] is True


def test_backfill_partial_files_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr("hermes_curator_evolver.cli.backfill_sessions", lambda **k: {"files_failed": 1, "sessions_imported": 2})

    rc = handle_cli(_args("backfill-sessions", "--sessions-dir", "synthetic", "--format", "json"))

    assert rc != 0
    assert _json_stdout(capsys)["files_failed"] == 1


def test_candidates_explicit_zero_limit_is_not_replaced_by_default(monkeypatch, capsys, tmp_path):
    observed = {}

    def fake_find_skill_candidates(**kwargs):
        observed.update(kwargs)
        return {"mode": "lexical", "models": {}, "candidates": []}

    monkeypatch.setattr("hermes_curator_evolver.cli.find_skill_candidates", fake_find_skill_candidates)

    rc = handle_cli(_args("candidates", "--query", "synthetic", "--skills-dir", str(tmp_path), "--limit", "0", "--format", "json"))

    assert rc == 0
    assert observed["limit"] == 0
    _json_stdout(capsys)
