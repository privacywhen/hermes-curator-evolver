"""Read-only catalog adapter. Offline identity is never write authorization."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import yaml
import sys
import json

from .hygiene import scrub
from .skill_sources import build_skill_source_context, classify_skill_source

_EXCLUDED = {".git", ".github", ".hub", ".archive", ".curator_backups", ".locks", ".venv", "venv", "node_modules", "site-packages", "__pycache__", ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_SUPPORT = {"references", "templates", "assets", "scripts"}
MAX_SKILL_CHARS = 100_000  # Existing skill hard cap, not a model input budget.


def _physical_files(root: Path):
    for path in sorted(root.rglob("SKILL.md")):
        parts = path.relative_to(root).parts
        if any(part in _EXCLUDED for part in parts):
            continue
        if any(part in _SUPPORT and (root.joinpath(*parts[:i]) / "SKILL.md").is_file() for i, part in enumerate(parts[:-1])):
            continue
        if path.is_file():
            yield path


def _metadata(text: str) -> dict[str, Any]:
    head = text[:4000].removeprefix("\ufeff")
    if not head.startswith("---\n"):
        return {}
    lines = head.splitlines()
    try:
        end = lines[1:].index("---") + 1
        data = yaml.safe_load("\n".join(lines[1:end]))
        return data if isinstance(data, dict) else {}
    except (ValueError, yaml.YAMLError):
        return {}


def _usage_snapshot(root: Path) -> tuple[str, dict[str, Any]]:
    try:
        with (root / ".usage.json").open(encoding="utf-8") as stream:
            text = stream.read(262_145)
        if len(text) > 262_144:
            return "oversized", {}
        data = json.loads(text)
        return ("valid", data) if isinstance(data, dict) else ("invalid", {})
    except FileNotFoundError:
        return "missing", {}
    except (json.JSONDecodeError, UnicodeError):
        return "invalid", {}
    except OSError:
        return "unreadable", {}


def read_skill_catalog(skills_dir: str | Path, *, native_api: Any = None,
                       disabled_names: set[str] | None = None,
                       roots: list[tuple[int, Path]] | None = None) -> dict[str, Any]:
    root = Path(skills_dir).resolve()
    scan_roots = [(tier, Path(folder).resolve()) for tier, folder in
                  (roots if roots is not None else [(1, root)]) if tier in (1, 2, 3)]
    # Never import registry/discovery modules to obtain a richer inventory.
    api = native_api if native_api is not None else sys.modules.get("agent.skill_utils")
    supported = all(callable(getattr(api, name, None)) for name in
                    ("iter_skill_index_files", "resolve_skill_catalog", "is_disabled_entry"))
    native_status, native_error = ("supported" if supported else "unavailable"), None
    try:
        paths = [(tier, folder, path) for tier, folder in scan_roots
                 for path in (api.iter_skill_index_files(folder, "SKILL.md") if supported else _physical_files(folder))]
    except Exception as exc:
        paths = [(tier, folder, path) for tier, folder in scan_roots for path in _physical_files(folder)]
        supported, native_status, native_error = False, "unsupported", type(exc).__name__
    entries = []
    for tier, folder, path in paths:
        path = Path(path)
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                text = stream.read(MAX_SKILL_CHARS + 1)
            state = "ok" if len(text) <= MAX_SKILL_CHARS else "oversized"
            metadata_text = scrub(text)
            text = metadata_text if state == "ok" else ""
        except OSError:
            text, metadata_text, state = "", "", "unreadable"
        meta = _metadata(metadata_text)
        name = meta.get("name")
        name = name if isinstance(name, str) and name else path.parent.name
        description = meta.get("description")
        entries.append({"name": name, "description": description if isinstance(description, str) else "",
                        "path": str(path), "root": str(folder), "tier": tier, "text": text,
                        "relative_path": path.parent.relative_to(folder).as_posix(),
                        "status": "unknown", "load_name": None, "visible": None,
                        "disabled": None, "read_status": state, "_metadata": meta,
                        "permissions": {"propose": state == "ok", "auto_apply": False}})
    if supported:
        try:
            if any(e["read_status"] != "ok" for e in entries):
                raise ValueError("incomplete physical identity inputs")
            resolved = api.resolve_skill_catalog(entries)
            if not isinstance(resolved, list) or len(resolved) != len(entries):
                raise ValueError("native catalog cardinality")
            if sorted(str(e["path"]) for e in resolved) != sorted(e["path"] for e in entries):
                raise ValueError("native physical identity mismatch")
            if any(e.get("status") not in {"unique", "ambiguous", "shadowed"} for e in resolved):
                raise ValueError("unsupported catalog status")
            entries = resolved
            for entry in entries:
                if disabled_names is not None:
                    entry["disabled"] = bool(api.is_disabled_entry(entry, disabled_names))
                platform = getattr(api, "skill_matches_platform", None)
                meta = entry["_metadata"]
                if callable(platform):
                    # Environment/app checks can import tools or probe installations.
                    # Strict inspection leaves those constraints unknown, not enabled.
                    entry["visible"] = (False if not platform(meta) else
                                        None if meta.get("environments") or meta.get("requires_apps") else True)
        except Exception as exc:
            native_status, native_error = "unsupported", type(exc).__name__
            for entry in entries:
                entry.update(status="unknown", load_name=None, visible=None, disabled=None)
    counts = Counter(e["name"] for e in entries)
    usage_status, usage = _usage_snapshot(root)
    # Arbitrary offline roots must not trigger ambient-profile configuration reads.
    source_context = build_skill_source_context(root) if root.name == "skills" else None
    for entry in entries:
        entry.pop("_metadata", None)
        # Resolution is identity metadata, never a single-writer handoff.
        entry["permissions"] = {"propose": entry["read_status"] == "ok", "auto_apply": False}
        entry["skill_name"] = entry.get("load_name") or (entry["relative_path"] if counts[entry["name"]] > 1 else entry["name"])
        entry["source"] = ("external-dir" if entry["tier"] == 3 else
            classify_skill_source(skill_name=entry["name"], skill_file=entry["path"], context=source_context).source
            if source_context is not None else "unknown")
        record = usage.get(entry.get("load_name")) if entry.get("load_name") else None
        entry.update(native_managed=None, native_pinned=None, authorship=None,
                     management_status=usage_status)
        if usage_status == "valid":
            if not entry.get("load_name"):
                entry["management_status"] = "unresolved-identity"
            elif not isinstance(record, dict):
                entry["management_status"] = "missing-record" if record is None else "invalid-record"
            else:
                # Native adoption markers express management opt-in, not authorship.
                entry["native_managed"] = record.get("created_by") == "agent" or record.get("agent_created") is True
                pinned = record.get("pinned")
                entry["native_pinned"] = pinned if isinstance(pinned, bool) else None
    identifiers = Counter(e["skill_name"] for e in entries)
    for entry in entries:
        if identifiers[entry["skill_name"]] > 1:
            entry["skill_name"] = f"physical:{entry['tier']}:{entry['path']}"
    return {"scope": "native-resolver-explicit-root" if native_status == "supported" else "standalone-physical",
            "adapter_contract": "resolver-v1", "native_status": native_status,
            "native_error": native_error, "entries": entries,
            "unavailable_scopes": ["trusted-project", "plugins"], "single_writer_handoff": False}


def review_rows(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Review candidates only; no lifecycle or write permission is conveyed."""
    return [e for e in catalog["entries"] if e["read_status"] == "ok"
            and e["visible"] is not False and e["disabled"] is not True
            and e["status"] != "shadowed"]
