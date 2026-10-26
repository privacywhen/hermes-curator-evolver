"""State must never be written into the plugin install directory.

Hermes hashes every enabled plugin's source tree to decide whether the dependency
environment is still in sync (``pm.workspace.members_stamp``). A plugin that writes
runtime state into that tree changes the hash on every turn, so dependency syncs fail
with "Dependency inputs changed while preparing publication" and the
``source-completion-pending`` marker is never cleared.

These tests pin the paths so a future refactor cannot quietly move state back.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from hermes_curator_evolver import auto_evolve, paths


def test_data_dir_is_outside_the_install_dir(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "plugins" / "curator-evolver").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_CURATOR_EVOLVER_DB", raising=False)

    resolved = paths.data_dir().resolve()

    assert "plugin-data" in resolved.parts, resolved
    assert resolved == (home / "plugin-data" / "curator-evolver").resolve(), resolved
    assert home / "plugins" not in resolved.parents, resolved


def test_default_backup_dir_is_outside_the_install_dir(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "plugins" / "curator-evolver").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    resolved = auto_evolve._default_backup_dir().resolve()

    assert resolved == (home / "plugin-data" / "curator-evolver" / "backups").resolve()
    assert home / "plugins" not in resolved.parents, resolved


def test_writing_the_database_does_not_touch_the_install_dir(tmp_path, monkeypatch):
    """The regression that mattered: a write under the old default moved the stamp."""
    home = tmp_path / ".hermes"
    install = home / "plugins" / "curator-evolver"
    install.mkdir(parents=True)
    (install / "pyproject.toml").write_text('[project]\nname = "curator"\n')
    (install / "plugin.py").write_text("VERSION = 1\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_CURATOR_EVOLVER_DB", raising=False)

    import hashlib
    from hermes_curator_evolver.storage import EvidenceStore

    def source_fingerprint():
        digest = hashlib.sha256()
        for source in sorted(install.rglob("*")):
            if source.is_file():
                digest.update(str(source.relative_to(install)).encode())
                digest.update(source.read_bytes())
        return digest.digest()

    before = source_fingerprint()
    store = EvidenceStore(paths.default_db_path())
    store.record_tool_call(tool_name="terminal", args={"cmd": "true"}, result="ok",
                           session_id="s1")
    (paths.default_backup_dir(create=True) / "skill.md").write_text("backup")
    logs = paths.data_dir() / "logs"
    logs.mkdir()
    (logs / "run.log").write_text("runtime log")

    assert paths.default_db_path().is_file()
    assert not (install / "data").exists()
    assert source_fingerprint() == before
    (install / "plugin.py").write_text("VERSION = 2\n")
    assert source_fingerprint() != before
    after_code = source_fingerprint()
    with (install / "pyproject.toml").open("a") as handle:
        handle.write('dependencies = ["PyYAML>=6"]\n')
    assert source_fingerprint() != after_code


def test_env_override_still_wins(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "custom.sqlite"
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(target))
    with patch.object(Path, "home", return_value=tmp_path):
        assert paths.default_db_path() == target
    assert not target.parent.exists()  # Resolution is not a writer operation.


def test_falls_back_to_home_plugin_data_without_hermes_installed(tmp_path, monkeypatch):
    """The ImportError branch must produce the same location, not the install dir."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_CURATOR_EVOLVER_DB", raising=False)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def blocked(name, *args, **kwargs):
        if name == "plugins.plugin_storage":
            raise ImportError("hermes not installed")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=blocked):
        resolved = paths.data_dir().resolve()

    assert resolved == (home / "plugin-data" / "curator-evolver").resolve()
    assert home / "plugins" not in resolved.parents


def test_no_stray_reference_to_the_old_path():
    """Guard the source itself: the old literal must not reappear."""
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for source in (root / "hermes_curator_evolver").glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for needle in ('"plugins", PLUGIN_NAME', '"plugins" / "curator-evolver"',
                       '"plugins", "curator-evolver"'):
            if needle in text:
                offenders.append(f"{source.name}: {needle}")
    assert offenders == [], offenders


def test_plugin_data_dir_api_is_used_when_available(tmp_path, monkeypatch):
    """Inside Hermes, core's own helper is the source of truth for the location."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_CURATOR_EVOLVER_DB", raising=False)

    expected = home / "plugin-data" / "curator-evolver"

    # Stand in for `plugins.plugin_storage.plugin_data_dir`, which only exists when
    # Hermes itself is importable. The plugin must go through it rather than
    # reimplementing the path, so core stays the single source of truth.
    stub = ModuleType("plugins")
    stub.__path__ = []  # mark as a package so the submodule import resolves
    storage = ModuleType("plugins.plugin_storage")
    calls: list[str] = []
    storage.plugin_data_dir = lambda name: (calls.append(name) or expected)
    stub.plugin_storage = storage

    with patch.dict(sys.modules, {"plugins": stub, "plugins.plugin_storage": storage}):
        resolved = paths.data_dir(create=True).resolve()

    assert calls == ["curator-evolver"], calls
    assert resolved == expected.resolve()
    assert home / "plugins" not in resolved.parents


@pytest.mark.parametrize("name", ["data", "backups", "logs"])
def test_legacy_state_requires_offline_migration(tmp_path, monkeypatch, name):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    legacy = tmp_path / "plugins" / "curator-evolver" / name
    legacy.mkdir(parents=True)
    evidence = legacy / ("evidence.sqlite" if name == "data" else "original.txt")
    evidence.write_bytes(b"history")
    target = tmp_path / "plugin-data" / "curator-evolver"
    # Reproduce the empty-destination case that used to nest backups/backups.
    (target / "backups").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="Stop all curator writers"):
        paths.data_dir()
    assert evidence.read_bytes() == b"history"
    assert not (target / "evidence.sqlite").exists()
    assert not (target / "backups" / "backups").exists()


def test_conflicting_databases_and_wal_are_not_mixed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    legacy = tmp_path / "plugins" / "curator-evolver" / "data"
    target = tmp_path / "plugin-data" / "curator-evolver"
    legacy.mkdir(parents=True)
    target.mkdir(parents=True)
    (legacy / "evidence.sqlite").write_bytes(b"old")
    (legacy / "evidence.sqlite-wal").write_bytes(b"old-wal")
    (target / "evidence.sqlite").write_bytes(b"new")
    with pytest.raises(RuntimeError, match="Legacy curator state"):
        paths.default_db_path()
    assert (legacy / "evidence.sqlite").read_bytes() == b"old"
    assert (legacy / "evidence.sqlite-wal").read_bytes() == b"old-wal"
    assert (target / "evidence.sqlite").read_bytes() == b"new"
    assert not (target / "evidence.sqlite-wal").exists()


@pytest.mark.parametrize("broken", [False, True])
def test_operator_symlink_is_not_consumed(tmp_path, monkeypatch, broken):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    legacy = tmp_path / "plugins" / "curator-evolver" / "data"
    legacy.parent.mkdir(parents=True)
    external = tmp_path / "local" / "curator-evolver" / "data"
    if not broken:
        external.mkdir(parents=True)
        (external / "evidence.sqlite").write_bytes(b"history")
    legacy.symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="Legacy curator state"):
        paths.data_dir()
    assert legacy.is_symlink()
    if not broken:
        assert (external / "evidence.sqlite").read_bytes() == b"history"


def test_live_wal_database_is_not_relocated(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    legacy = tmp_path / "plugins" / "curator-evolver" / "data"
    legacy.mkdir(parents=True)
    db = legacy / "evidence.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute("pragma journal_mode=wal")
        conn.execute("create table evidence(value text)")
        conn.execute("insert into evidence values ('before')")
        conn.commit()
        with pytest.raises(RuntimeError, match="Stop all curator writers"):
            paths.data_dir()
        conn.execute("insert into evidence values ('after')")
        conn.commit()
        assert conn.execute("select value from evidence order by rowid").fetchall() == [
            ("before",), ("after",)]
        assert db.is_file()
        assert not (tmp_path / "plugin-data/curator-evolver/evidence.sqlite").exists()
    finally:
        conn.close()


def test_offline_directory_migration_preserves_real_evidence(tmp_path, monkeypatch):
    import sqlite3
    from hermes_curator_evolver.storage import EvidenceStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CURATOR_EVOLVER_DB", raising=False)
    legacy = tmp_path / "plugins" / "curator-evolver" / "data"
    store = EvidenceStore(legacy / "evidence.sqlite", collect_previews=True)
    store.record_tool_call(tool_name="terminal", args={}, result="before")
    # Model the documented offline step after every writer has closed its DB.
    target = tmp_path / "plugin-data" / "curator-evolver"
    target.parent.mkdir(parents=True)
    legacy.rename(target)
    legacy.symlink_to(target, target_is_directory=True)
    for _ in range(2):
        assert paths.data_dir() == target
    migrated = EvidenceStore(collect_previews=True)
    migrated.record_tool_call(tool_name="terminal", args={}, result="after")
    with sqlite3.connect(migrated.db_path) as conn:
        assert conn.execute("select result_preview from tool_events order by id").fetchall() == [
            ("before",), ("after",)]
        assert conn.execute("pragma quick_check").fetchone() == ("ok",)


def test_active_profiles_keep_separate_state(tmp_path, monkeypatch):
    homes = [tmp_path / "default", tmp_path / "profiles" / "other"]
    resolved = []
    for home in homes:
        monkeypatch.setenv("HERMES_HOME", str(home))
        resolved.append(paths.data_dir())
    assert resolved == [h / "plugin-data" / "curator-evolver" for h in homes]


def test_empty_legacy_directories_do_not_block_fresh_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("data", "backups", "logs"):
        (tmp_path / "plugins" / "curator-evolver" / name).mkdir(parents=True)
    assert paths.data_dir() == tmp_path / "plugin-data" / "curator-evolver"
