"""Evidence report generation."""

from __future__ import annotations

import json
from typing import Any

from .storage import EvidenceStore
from .privacy import clean_report


def build_report(store: EvidenceStore, *, days: int, skill: str | None = None) -> dict[str, Any]:
    """Build a read-only evidence report from the local store."""
    summary = store.summary(days=days, skill=skill)
    evidence = store.recent_tool_events(days=days, skill=skill, limit=20)
    turns = store.recent_turns(days=days, limit=10)
    return {
        "title": "Hermes Curator Evolver evidence report",
        "read_only": True,
        "db_path": str(store.db_path),
        "filters": {"days": days, "skill": skill},
        "summary": summary,
        "skill_evidence": evidence,
        "recent_turns": turns,
        "privacy": {"content_previews": store.include_legacy_previews, "preview_character_cap": 2000,
                    "legacy_preview_consent": store.include_legacy_previews,
                    "sanitization": "best-effort; not an egress authorization"},
        "sampling": {
            "tool_rows": {"limit": 20, "returned": len(evidence), "truncated": summary["tool_events"] > len(evidence)},
            "turn_rows": {"limit": 10, "returned": len(turns), "truncated": summary["turn_events"] > len(turns)},
            "top_skills_limit": 20, "top_tools_limit": 20,
        },
        "recommendation_policy": {
            "v0_1": "report_only",
            "mutation": "not_supported",
            "next_steps": [
                "Use repeated tool errors as candidates for targeted skill improvements.",
                "Use skill event counts to identify active skills worth reviewing.",
                "Use semantic candidate generation only in a later explicit opt-in phase.",
            ],
        },
    }


def build_default_report(*, days: int, skill: str | None = None, include_legacy_previews: bool = False) -> dict[str, Any]:
    return build_report(EvidenceStore(read_only=True, include_legacy_previews=include_legacy_previews), days=days, skill=skill)


def format_json_report(report: dict[str, Any]) -> str:
    return json.dumps(clean_report(report), ensure_ascii=False, indent=2, sort_keys=True)


def format_markdown_report(report: dict[str, Any]) -> str:
    report = clean_report(report)
    summary = report["summary"]
    filters = report["filters"]
    skill = filters.get("skill")
    title = "# Hermes Curator Evolver — Read-only evidence report"
    lines = [
        title,
        "",
        f"- Window: last {filters['days']} day(s)",
        f"- Skill filter: `{skill}`" if skill else "- Skill filter: all skills",
        f"- DB: `{report['db_path']}`",
        "- Safety: No skill mutations were performed.",
        "",
        "## Summary",
        "",
        f"- Tool events: {summary['tool_events']}",
        f"- Skill-related events: {summary['skill_events']}",
        f"- Error-like tool events: {summary['error_events']}",
        f"- Turn events: {summary['turn_events']}",
        f"- Session-end events: {summary['session_events']}",
        "",
    ]
    lines.extend(["## Top skills", ""])
    if summary["skills"]:
        for item in summary["skills"]:
            lines.append(
                f"- `{item['skill_name']}` — {item['event_count']} event(s), "
                f"{item['errors']} error-like"
            )
    else:
        lines.append("- No skill-related events recorded in this window.")
    lines.extend(["", "## Top tools", ""])
    if summary["tools"]:
        for item in summary["tools"]:
            lines.append(
                f"- `{item['tool_name']}` — {item['event_count']} event(s), "
                f"{item['errors']} error-like"
            )
    else:
        lines.append("- No tool events recorded in this window.")
    lines.extend(["", "## Recent skill/tool evidence", ""])
    evidence = report["skill_evidence"]
    if evidence:
        for item in evidence[:10]:
            skill_label = item.get("skill_name") or "—"
            marker = "⚠️" if item.get("is_error") else "✓"
            lines.append(
                f"- {marker} `{item['created_at']}` tool=`{item['tool_name']}` "
                f"skill=`{skill_label}` session=`{item.get('session_id') or '—'}`"
            )
    else:
        lines.append("- No recent evidence rows matched the filters.")
    lines.extend(
        [
            "",
            "## Interpretation policy",
            "",
            "This report is evidence only. Repeated errors or repeated skill reads are candidates for proposal generation or low-risk auto-run, not proof that a destructive skill change is safe.",
            "",
            "No skill mutations were performed by this report command.",
        ]
    )
    return "\n".join(lines)
