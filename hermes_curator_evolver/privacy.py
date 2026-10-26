"""Small metadata allowlists; content collection is a separate, explicit opt-in."""
from __future__ import annotations

import json
import os
from typing import Any
from .hygiene import scrub_value


def minimal_args(tool_name: str, value: Any) -> dict[str, Any]:
    """Keep skill attribution, never arbitrary commands, paths, or skill contents."""
    value = scrub_value(value)
    if not isinstance(value, dict):
        return {}
    result = {}
    if tool_name in {"skill_view", "skill" + "_" + "manage"}:
        for key in ("name", "skill", "skill_name", "action"):
            if isinstance(value.get(key), str):
                result[key] = value[key][:256]
    if isinstance(value.get("skills"), list):
        result["skills"] = [item[:256] for item in value["skills"][:20] if isinstance(item, str)]
    return result


def minimal_result(value: Any) -> dict[str, Any]:
    """Retain typed outcome signals, not free-form error/output payloads."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = {}
    result: dict[str, Any] = {"content_omitted": True}
    if not isinstance(value, dict):
        return result
    if isinstance(value.get("success"), bool):
        result["success"] = value["success"]
    if "error" in value:
        result["error"] = bool(value["error"])
    if type(value.get("exit_code")) is int:
        result["exit_code"] = value["exit_code"]
    if value.get("status") in ("success", "error", "failed", "ok", "completed", "interrupted", "running"):
        result["status"] = value["status"]
    return result


def clean_evidence_row(row: dict[str, Any], *, include_previews: bool | None = None) -> dict[str, Any]:
    """Minimize legacy rows too; the opt-in marker is attached by store readers."""
    include = row.get("_previews_opt_in") is True if include_previews is None else include_previews
    fields = {"id", "created_at", "session_id", "task_id", "tool_name", "duration_ms",
              "is_error", "skill_name", "args_json", "result_preview", "model", "platform",
              "user_preview", "assistant_preview"}
    result = scrub_value({key: value for key, value in row.items() if key in fields})
    if "args_json" in result:
        try:
            args = json.loads(result["args_json"])
        except (ValueError, TypeError):
            args = {}
        args = args if include else minimal_args(str(result.get("tool_name", "")), args)
        text = json.dumps(args, ensure_ascii=False, sort_keys=True)
        result["args_json"] = text if len(text) <= 2000 else '{"content_omitted":true}'
    if "result_preview" in result:
        result["result_preview"] = (str(result["result_preview"])[:2000] if include else
                                    json.dumps(minimal_result(result["result_preview"]), sort_keys=True))
    for key in ("user_preview", "assistant_preview"):
        if key in result:
            result[key] = str(result[key])[:2000] if include else ""
    for key, value in result.items():
        if isinstance(value, str) and key not in {"args_json", "result_preview", "user_preview", "assistant_preview"}:
            result[key] = value[:256]
    result["_previews_opt_in"] = bool(include)
    return result


def clean_report(report: dict[str, Any]) -> dict[str, Any]:
    """Sanitize at publication as well as collection; never rewrite source rows."""
    result = scrub_value(report)
    for key in ("skill_evidence", "recent_turns"):
        if key in result:
            result[key] = [clean_evidence_row(row) for row in result[key]][:20]
    return result


def clean_candidate(payload: dict[str, Any], *, include_previews: bool | None = None) -> dict[str, Any]:
    """Persist/export classification metadata unless content is explicitly opted in."""
    result = scrub_value(payload)
    include = os.getenv("HERMES_CURATOR_EVOLVER_COLLECT_PREVIEWS") == "1" if include_previews is None else include_previews
    if include:
        result["title"] = str(result.get("title", ""))[:500]
        result["rationale"] = str(result.get("rationale", ""))[:500]
    else:
        result["title"] = str(result.get("candidate_type", "review")) + " candidate"
        result["rationale"] = "Content omitted by privacy policy; review the local input with explicit content consent."
    result["metadata"] = {}
    if "evidence_refs" in result:
        result["evidence_refs"] = [str(ref)[:256] for ref in result["evidence_refs"][:100]]
    return result
