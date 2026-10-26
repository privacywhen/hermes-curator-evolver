# Installation and first-run safety

## Normal admission

Use Hermes' plugin manager and leave its install scanner enabled:

```text
hermes plugins install privacywhen/hermes-curator-evolver --enable
```

The manager resolves the active Hermes environment and dependency workspace. Review caution findings before accepting. A dangerous scanner verdict is a stop condition; do not disable scanning, force past it, or alter the installed Hermes source tree. If the install fails, retain the full resolver/scanner diagnostic and resolve that cause before enabling the plugin.

After install, inspect registration and status:

```text
hermes plugins list
hermes-curator-evolver status
```

A newly created state database may initially contain no collected evidence. Plugin registration, hook collection, historical backfill, and scheduled autorun are distinct states; do not infer one from another.

## Bootstrap is consequential

Bootstrap is not a required installation check and should not be run as an unquestioned copy-paste step. It can read historical session records, write persistent local plugin evidence, and install or enable recurring user-level scheduled work. Those are meaningful data and persistence effects even when skill writes are gated. Review the installed command's help and the current profile/configuration before opting in; run it only after deciding those effects are intended. Do not claim that running bootstrap is harmless or that it proves plugin activation is healthy.

For operator checks, data locations, stopped-writer migration, and rollback, see [after-install.md](after-install.md). For existing installations, do not resume writers until the documented migration procedure is completed.

## Platform and Python environment

Hermes resolves its active, profile-aware home; an explicit `HERMES_HOME` takes precedence. On Windows the default is under `%LOCALAPPDATA%\hermes`, not necessarily `~/.hermes`. In PowerShell, the path form is `$env:LOCALAPPDATA\hermes`; in Command Prompt it is `%LOCALAPPDATA%\hermes`; in Git Bash it is `/c/Users/<user>/AppData/Local/hermes`. Use the Python environment managed by Hermes for plugin dependencies. Do not install into global Python, directly modify the manager's installed source checkout, or bypass the manager's lock/sync process. The `semantic` extra is optional; base installation does not request it. It is supported on Windows, Linux, and macOS only.

## Preserve data

Evidence, backups, and logs are persistent user state. Removal of the plugin does not authorize deleting them. Before any manual cleanup, stop the gateway, CLI writers, and any plugin scheduler; resolve and verify the exact active profile path; make and verify an independent backup; and select only the specific data intended for deletion. Do not use a broad wildcard or copied recursive-delete command. The migration and rollback procedure in [after-install.md](after-install.md#existing-installations) requires stopped writers and integrity checks.
