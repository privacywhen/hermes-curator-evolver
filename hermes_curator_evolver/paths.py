"""Filesystem paths for Hermes Curator Evolver."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_NAME = "curator-evolver"


def _platform_default_hermes_home() -> Path:
    """Mirror Hermes' platform-native fallback when its constants are unavailable."""

    if sys.platform == "win32":
        local_appdata = os.getenv("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def hermes_home() -> Path:
    """Return Hermes home, profile-aware when running inside Hermes."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        env_home = os.getenv("HERMES_HOME")
        if env_home:
            return Path(env_home).expanduser()
        return _platform_default_hermes_home()
    return Path(get_hermes_home())


def data_dir(*, create: bool = False) -> Path:
    """Resolve plugin data, creating it only for an explicit writer call.

    State lives in ``<hermes home>/plugin-data/<plugin>/``, the location Hermes
    core reserves for plugin state. It must NOT live in ``<hermes home>/plugins/``:
    that is the install directory, which ``hermes plugins update`` git-pulls and
    ``hermes plugins remove`` deletes. Worse, core hashes every enabled plugin's
    source tree to decide whether the dependency environment is still in sync
    (``pm.workspace.members_stamp``), so writing an evidence database into the
    install directory makes that stamp change on every turn. Dependency syncs then
    fail with "Dependency inputs changed while preparing publication", the
    ``source-completion-pending`` marker is never cleared, and every launch
    re-runs a tail it can never finish.

    ``plugin_data_dir`` is the supported core API for this; fall back to the same
    path when Hermes is not importable (standalone CLI use).
    """
    if not create:
        path = hermes_home() / "plugin-data" / PLUGIN_NAME
    else:
        try:
            from plugins.plugin_storage import plugin_data_dir
        except ImportError:
            path = hermes_home() / "plugin-data" / PLUGIN_NAME
        else:
            path = plugin_data_dir(PLUGIN_NAME)
    _check_legacy_state(path)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _check_legacy_state(new_dir: Path) -> None:
    """Refuse to hide existing evidence or relocate a database under live writers.

    An old gateway, CLI or timer may still have the SQLite file open. Moving its
    DB/WAL/SHM files separately can split writers or attach another database's WAL.
    Migration therefore requires stopped producers; see docs/after-install.md.
    A legacy symlink already pointing at the canonical destination needs no move.
    """
    legacy_home = hermes_home() / "plugins" / PLUGIN_NAME
    for name, target in (("data", new_dir), ("backups", new_dir / "backups"),
                         ("logs", new_dir / "logs")):
        legacy = legacy_home / name
        if legacy.resolve() == target.resolve():
            continue
        # lexists includes broken operator symlinks: do not silently replace
        # missing external evidence with a fresh, empty store.
        if not os.path.lexists(legacy):
            continue
        if legacy.is_dir() and not any(legacy.iterdir()):
            continue
        raise RuntimeError(
            f"Legacy curator state at {legacy} must be migrated to {target}. "
            "Stop all curator writers (gateway, CLI and timers), back up the "
            "state, and follow docs/after-install.md#existing-installations. "
            "No legacy state was moved or overwritten."
        )


def default_db_path() -> Path:
    """Return the configured SQLite database path."""
    override = os.getenv("HERMES_CURATOR_EVOLVER_DB")
    if override:
        return Path(override).expanduser()
    return data_dir(create=False) / "evidence.sqlite"


def default_backup_dir(*, create: bool = False) -> Path:
    """Return the default guarded-apply backup directory.

    Like the evidence database, backups must stay out of the install directory:
    ``--apply-low-risk`` runs write them, so hashing them into the dependency
    stamp has the same failure mode as the database.
    """
    path = data_dir(create=create) / "backups"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path
