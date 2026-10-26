"""Inspection must not initialize or repair the operator's evidence store."""
import pytest
import sqlite3
from pathlib import Path
import json
import os
import subprocess
import sys

from hermes_curator_evolver.reports import build_default_report
from hermes_curator_evolver.storage import EvidenceStore
from hermes_curator_evolver.__main__ import build_parser
from hermes_curator_evolver.cli import handle_cli
from hermes_curator_evolver import paths


def test_report_missing_store_is_explicit_and_leaves_parent_absent(tmp_path, monkeypatch):
    db = tmp_path / "missing" / "evidence.sqlite"
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(db))
    with pytest.raises(FileNotFoundError, match="evidence"):
        build_default_report(days=7)
    assert not db.parent.exists()


def test_resolving_default_paths_is_pure(tmp_path, monkeypatch):
    home = tmp_path / "not-created"
    monkeypatch.setattr(paths, "hermes_home", lambda: home)
    monkeypatch.delenv("HERMES_CURATOR_EVOLVER_DB", raising=False)
    assert paths.data_dir() == home / "plugin-data" / "curator-evolver"
    paths.default_db_path()
    paths.default_backup_dir()
    assert not home.exists()


def test_strict_reader_refuses_wal_format_without_creating_sidecars(tmp_path):
    db = tmp_path / "wal.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE fixture(value TEXT)")
    conn.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(RuntimeError, match="snapshot"):
        with EvidenceStore(db, read_only=True).connect() as reader:
            reader.execute("SELECT 1")
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_writer_connections_close_before_offline_move(tmp_path):
    db = tmp_path / "evidence.sqlite"
    store = EvidenceStore(db)
    with store.connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    db.rename(tmp_path / "moved.sqlite")


def test_drill_evidence_probe_refuses_wal_without_touching_source(tmp_path):
    from hermes_curator_evolver.restore_drill import _check_evidence_refs

    db = tmp_path / "wal.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE fixture(value TEXT)")
    conn.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    result = _check_evidence_refs({"db_path": str(db)})
    assert result["status"] == "fail"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("command", [
    ["status"], ["report", "--format", "json"],
    ["analyze", "--skill", "synthetic"], ["auto-run", "--skills-dir", "absent-skills"],
])
def test_inspection_cli_reports_missing_without_creating_storage(tmp_path, monkeypatch, command):
    db = tmp_path / "missing" / "evidence.sqlite"
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(db))
    assert handle_cli(build_parser().parse_args(command)) == 1
    assert not db.parent.exists()


def test_standalone_missing_report_exits_nonzero(tmp_path, monkeypatch):
    db = tmp_path / "missing" / "evidence.sqlite"
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(db))
    result = subprocess.run([sys.executable, "-m", "hermes_curator_evolver", "report", "--format", "json"],
                            capture_output=True, text=True, env=dict(os.environ))
    assert result.returncode == 1
    assert json.loads(result.stdout)["success"] is False
    assert not db.parent.exists()


def test_cli_rejects_zero_days_instead_of_substituting_default():
    from hermes_curator_evolver.cli import _bounded_days
    with pytest.raises(ValueError, match="days"):
        _bounded_days(0)


def test_zero_candidate_limit_never_loads_a_model(tmp_path, monkeypatch):
    from hermes_curator_evolver import semantic
    def forbidden():
        pytest.fail("limit=0 must not load a model")
    monkeypatch.setattr(semantic, "_load_embedding_backend", forbidden)
    result = semantic.find_skill_candidates(query="synthetic", skills_dir=tmp_path, semantic=True,
                                            load_models=True, limit=0)
    assert result["candidates"] == []
    assert result["model_executed"] is False


def test_zero_backfill_limit_is_rejected_before_initializing_writer(tmp_path, monkeypatch):
    db = tmp_path / "absent-parent" / "evidence.sqlite"
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(db))
    args = build_parser().parse_args(["backfill-sessions", "--sessions-dir", str(tmp_path), "--limit", "0", "--format", "json"])
    assert handle_cli(args) == 1
    assert not db.parent.exists()


@pytest.mark.parametrize("kind", ["corrupt", "old-schema"])
def test_bad_store_inspection_does_not_repair_or_quarantine(tmp_path, monkeypatch, kind):
    db = tmp_path / "evidence.sqlite"
    if kind == "corrupt":
        db.write_bytes(b"not a database")
    else:
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE previous_schema(value TEXT)")
        conn.close()
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "preserve.txt").write_text("synthetic backup", encoding="utf-8")
    def files():
        return {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    before = files()
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(db))
    assert handle_cli(build_parser().parse_args(["report", "--format", "json"])) == 1
    assert files() == before


def test_backfill_api_rejects_zero_limit_before_creating_storage(tmp_path, monkeypatch):
    from hermes_curator_evolver.backfill import backfill_sessions
    db = tmp_path / "absent" / "evidence.sqlite"
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(db))
    with pytest.raises(ValueError, match="limit"):
        backfill_sessions(sessions_dir=tmp_path, limit=0)
    assert not db.parent.exists()
