"""SQLite evidence storage for Hermes Curator Evolver."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SQLITE_HEADER = b"SQLite format 3\x00"

from .paths import default_db_path
from .sqlite_read import ClosingConnection, open_read_snapshot
from .privacy import clean_evidence_row, minimal_args, minimal_result
from .hygiene import scrub_value

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    duration_ms INTEGER,
    is_error INTEGER NOT NULL,
    skill_name TEXT,
    args_json TEXT NOT NULL,
    result_preview TEXT NOT NULL,
    tool_call_id TEXT,
    turn_id TEXT,
    api_request_id TEXT,
    status TEXT,
    error_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_events_created ON tool_events(created_at);
CREATE INDEX IF NOT EXISTS idx_tool_events_skill ON tool_events(skill_name);
CREATE INDEX IF NOT EXISTS idx_tool_events_backfill_key ON tool_events(session_id, task_id, tool_name);

CREATE TABLE IF NOT EXISTS tool_event_actions (
    tool_event_id INTEGER NOT NULL,
    action_index INTEGER NOT NULL,
    skill_name TEXT NOT NULL,
    action TEXT NOT NULL,
    succeeded INTEGER,
    PRIMARY KEY (tool_event_id, action_index)
);

CREATE TABLE IF NOT EXISTS turn_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    platform TEXT NOT NULL,
    user_preview TEXT NOT NULL,
    assistant_preview TEXT NOT NULL,
    event_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_turn_events_created ON turn_events(created_at);
CREATE INDEX IF NOT EXISTS idx_turn_events_session ON turn_events(session_id);

CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    completed INTEGER NOT NULL,
    interrupted INTEGER NOT NULL,
    model TEXT NOT NULL,
    platform TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_events_created ON session_events(created_at);
CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id);
"""

MANAGE_TOOL_NAME = "skill" + "_" + "manage"
SKILL_TOOL_NAMES = {"skill_view", MANAGE_TOOL_NAME}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(days, 0))).isoformat(
        timespec="seconds"
    )


def _compact(value: Any, limit: int) -> str:
    value = scrub_value(value)
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = "[omitted: unsupported payload]"
    text = text.replace("\\x00", "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _json_dumps(value: Any, limit: int) -> str:
    value = scrub_value(value)
    try:
        text = json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = json.dumps({"content_omitted": True}, sort_keys=True)
    if len(text) <= limit:
        return text
    return json.dumps({"preview": text[: max(limit - 1, 0)] + "…"}, ensure_ascii=False)


def _identity_digest(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()
    return json.dumps(["sha256", digest], separators=(",", ":"))


def bounded_identity_id(value: Any) -> str:
    """Keep ordinary IDs; fingerprint recognized-sensitive, reserved-marker, or long IDs."""
    from .hygiene import scrub
    text = str(value)
    # Generated marker strings are reserved for digests; raw input in that domain
    # is itself fingerprinted, so it cannot alias an encoded value.
    reserved_marker = text.startswith('["sha256","') and text.endswith('"]')
    if len(text) > 128 or scrub(text) != text or reserved_marker:
        return _identity_digest(text)
    return text


def bounded_event_id(value: Any) -> str:
    """Keep safe event IDs; fingerprint sensitive/oversized IDs collision-resistently."""
    from .hygiene import scrub
    text = str(value)
    if len(text) > 128 or scrub(text) != text:
        return _identity_digest(text)
    return text


def replay_key(namespace: str, value: Any) -> str:
    """Namespace an ID; preserve safe values, fingerprint private/oversized keys."""
    return bounded_event_id(json.dumps([namespace, str(value)], ensure_ascii=False, separators=(",", ":")))


def _outcome_payload(result: Any) -> Any:
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return None
    return result


def _has_failure(value: Any) -> bool:
    """Recognize explicit structured failure signals without truth-testing empty fields."""
    if isinstance(value, dict):
        if value.get("success") is False:
            return True
        exit_code = value.get("exit_code")
        if isinstance(exit_code, (int, float)) and not isinstance(exit_code, bool) and exit_code != 0:
            return True
        for key in ("failed", "failures"):
            count = value.get(key)
            if isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0:
                return True
        error = value.get("error")
        if _has_error_content(error):
            return True
        results = value.get("results")
        return isinstance(results, list) and any(_has_failure(item) for item in results)
    if isinstance(value, list):
        return any(_has_failure(item) for item in value)
    return False


def _has_error_content(value: Any) -> bool:
    if value is None or value is False or value == 0 or value == "":
        return False
    if isinstance(value, dict):
        return any(_has_error_content(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_error_content(item) for item in value)
    return True


def _looks_like_error(result: Any, error_type: Any = None) -> bool:
    payload = _outcome_payload(result)
    if _has_failure(payload):
        return True
    if error_type not in (None, "", False):
        return True
    # Preserve legacy plain-text diagnostics, but never infer from malformed JSON
    # or from prose attached to an explicit structured success.
    if isinstance(result, str) and payload is result and not result.lstrip().startswith(("{", "[")):
        text = result.lower()
        if any(success in text for success in ("success", "no error", "without error")):
            return False
        return any(marker in text for marker in ("traceback", "exception", "error", "failed", "exit_code"))
    return False


def _normalized_status(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip().casefold()
    return text if text in {"ok", "error", "blocked", "cancelled"} else "unknown"


def _skill_actions(
    tool_name: str, args: Any, result: Any, status: str | None, error_type: Any = None
) -> list[tuple[int, str, str, int | None]]:
    if tool_name not in SKILL_TOOL_NAMES or not isinstance(args, dict):
        return []
    operations = args.get("operations")
    if not isinstance(operations, list):
        return []
    payload = _outcome_payload(result)
    results = payload.get("results") if isinstance(payload, dict) else None
    whole_success = (
        isinstance(payload, dict)
        and status in (None, "ok")
        and (payload.get("success") is True or status == "ok")
        and not _has_failure(payload)
        and (status == "ok" or payload.get("success") is True)
        and not _looks_like_error(payload, error_type)
    )
    complete_success = (
        whole_success
        and isinstance(results, list)
        and len(results) == len(operations)
        and all(isinstance(item, dict) and item.get("success") is True for item in results)
    )
    actions = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            continue
        name = operation.get("name") or operation.get("skill") or operation.get("skill_name")
        action = operation.get("action")
        if not name or not action:
            continue
        actions.append((index, _compact(name, 256), _compact(action, 64), 1 if complete_success else None))
    return actions


def _extract_skill_name(tool_name: str, args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    if tool_name in SKILL_TOOL_NAMES:
        name = args.get("name") or args.get("skill") or args.get("skill_name")
        return str(name) if name else None
    skills = args.get("skills")
    if isinstance(skills, list) and skills:
        first = skills[0]
        return str(first) if first else None
    return None


class EvidenceStore:
    """Small SQLite repository for local curator evidence."""

    def __init__(self, db_path: str | Path | None = None, preview_chars: int = 500, *, read_only: bool = False, collect_previews: bool | None = None, include_legacy_previews: bool = False):
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.preview_chars = max(0, min(int(preview_chars), 2000))
        self.collect_previews = (os.getenv("HERMES_CURATOR_EVOLVER_COLLECT_PREVIEWS") == "1"
                                 if collect_previews is None else bool(collect_previews))
        self.read_only = read_only
        # No collection-time provenance exists in old rows. Reading their content
        # requires explicit per-reader consent, never a collection environment flag.
        self.include_legacy_previews = bool(include_legacy_previews)
        if read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"evidence store does not exist: {self.db_path}")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.init_db()

    def connect(self) -> sqlite3.Connection:
        if self.read_only:
            return open_read_snapshot(self.db_path)
        else:
            conn = sqlite3.connect(self.db_path, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        if self.read_only:
            raise PermissionError("inspection cannot initialize evidence storage")
        try:
            with self.connect() as conn:
                # executescript commits an existing transaction first; begin INSIDE
                # the script so schema creation and nullable additions roll back together.
                conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
                try:
                    additions = {
                        "tool_events": {
                            "tool_call_id": "TEXT", "turn_id": "TEXT",
                            "api_request_id": "TEXT", "status": "TEXT", "error_type": "TEXT",
                        },
                        "turn_events": {"event_id": "TEXT"},
                    }
                    for table, fields in additions.items():
                        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                        for name, declaration in fields.items():
                            if name not in columns:
                                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except sqlite3.DatabaseError:
            if not self._quarantine_corrupt_db():
                raise
            with self.connect() as conn:
                conn.executescript(_SCHEMA)

    def _quarantine_corrupt_db(self) -> bool:
        """Move a non-SQLite evidence file aside so collection can resume."""
        if not self.db_path.exists() or not self.db_path.is_file():
            return False
        try:
            header = self.db_path.read_bytes()[: len(_SQLITE_HEADER)]
        except OSError:
            return False
        if header == _SQLITE_HEADER:
            return False
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.db_path.with_name(f"{self.db_path.name}.corrupt.{stamp}")
        suffix = 1
        while backup.exists():
            backup = self.db_path.with_name(
                f"{self.db_path.name}.corrupt.{stamp}.{suffix}"
            )
            suffix += 1
        self.db_path.replace(backup)
        return True

    def record_tool_call(
        self,
        *,
        tool_name: str,
        args: Any,
        result: Any,
        task_id: str = "",
        session_id: str = "",
        duration_ms: int | None = None,
        created_at: str | None = None,
        tool_call_id: str | None = None,
        turn_id: str | None = None,
        api_request_id: str | None = None,
        status: str | None = None,
        error_type: str | None = None,
        atomic_dedupe: bool = False,
    ) -> bool | None:
        skill_name = _extract_skill_name(tool_name, args)
        normalized_status = _normalized_status(status)
        is_error = (
            normalized_status in {"error", "blocked", "cancelled"}
            if normalized_status is not None
            else _looks_like_error(result, error_type)
        )
        actions = _skill_actions(tool_name, args, result, normalized_status, error_type)
        with self.connect() as conn:
            if atomic_dedupe:
                identity = {
                    "tool_call_id": tool_call_id,
                    "turn_id": turn_id,
                    "api_request_id": api_request_id,
                }
                # Turn/request IDs group invocations; they cannot identify one.
                has_invocation_id = bool(tool_call_id and session_id)
                conn.execute("BEGIN IMMEDIATE")
                if has_invocation_id:
                    where = "session_id = ? AND tool_name = ? AND " + " AND ".join(f"{key} IS ?" for key in identity)
                    params = [
                        bounded_identity_id(session_id), _compact(tool_name, 256),
                        *(bounded_identity_id(value) if value else None for value in identity.values()),
                    ]
                    if conn.execute(f"SELECT 1 FROM tool_events WHERE {where} LIMIT 1", params).fetchone():
                        return False
                elif conn.execute(
                    "SELECT 1 FROM tool_events WHERE session_id = ? AND task_id = ? AND tool_name = ? LIMIT 1",
                    (bounded_identity_id(session_id), bounded_identity_id(task_id), _compact(tool_name, 256)),
                ).fetchone():
                    return False
            cursor = conn.execute(
                """
                INSERT INTO tool_events (
                    created_at, session_id, task_id, tool_name, duration_ms,
                    is_error, skill_name, args_json, result_preview,
                    tool_call_id, turn_id, api_request_id, status, error_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _compact(created_at or utc_now(), 64),
                    bounded_identity_id(session_id),
                    bounded_identity_id(task_id),
                    _compact(tool_name, 256),
                    duration_ms,
                    1 if is_error else 0,
                    _compact(skill_name, 256) if skill_name else None,
                    _json_dumps(args if self.collect_previews else minimal_args(tool_name, args), self.preview_chars * 2),
                    _compact(result if self.collect_previews else minimal_result(result), self.preview_chars),
                    bounded_identity_id(tool_call_id) if tool_call_id else None,
                    bounded_identity_id(turn_id) if turn_id else None,
                    bounded_identity_id(api_request_id) if api_request_id else None,
                    normalized_status,
                    _compact(error_type, 128) if error_type else None,
                ),
            )
            conn.executemany(
                "INSERT INTO tool_event_actions (tool_event_id, action_index, skill_name, action, succeeded) VALUES (?, ?, ?, ?, ?)",
                [(cursor.lastrowid, *action) for action in actions],
            )
            return True if atomic_dedupe else None

    def record_turn(
        self,
        *,
        session_id: str,
        user_message: str,
        assistant_response: str,
        event_id: str = "",
        model: str = "",
        platform: str = "",
        created_at: str | None = None,
        atomic_dedupe: bool = False,
    ) -> bool | None:
        event_id = bounded_event_id(event_id) if event_id else ""
        with self.connect() as conn:
            if atomic_dedupe and event_id:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                    "SELECT 1 FROM turn_events WHERE session_id = ? AND event_id = ? LIMIT 1",
                    (bounded_identity_id(session_id), str(event_id)),
                ).fetchone():
                    return False
            conn.execute(
                """
                INSERT INTO turn_events (
                    created_at, session_id, model, platform,
                    user_preview, assistant_preview, event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _compact(created_at or utc_now(), 64),
                    bounded_identity_id(session_id),
                    _compact(model, 256),
                    _compact(platform, 256),
                    *self.turn_previews(user_message, assistant_response, event_id=event_id),
                    str(event_id) if event_id else None,
                ),
            )
            return True if atomic_dedupe else None

    def turn_previews(self, user_message: str, assistant_response: str, *, event_id: str = "") -> tuple[str, str]:
        """Use the same minimized representation for collection and replay checks."""
        if not self.collect_previews:
            return (json.dumps({"event_id": _compact(event_id, 128)}) if event_id else ""), ""
        return _compact(user_message, self.preview_chars), _compact(assistant_response, self.preview_chars)

    def record_session_end(
        self,
        *,
        session_id: str,
        completed: bool,
        interrupted: bool,
        model: str = "",
        platform: str = "",
        created_at: str | None = None,
        atomic_dedupe: bool = False,
    ) -> bool | None:
        with self.connect() as conn:
            if atomic_dedupe:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                    "SELECT 1 FROM session_events WHERE session_id = ? LIMIT 1",
                    (bounded_identity_id(session_id),),
                ).fetchone():
                    return False
            conn.execute(
                """
                INSERT INTO session_events (
                    created_at, session_id, completed, interrupted, model, platform
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _compact(created_at or utc_now(), 64),
                    bounded_identity_id(session_id),
                    1 if completed else 0,
                    1 if interrupted else 0,
                    _compact(model, 256),
                    _compact(platform, 256),
                ),
            )
            return True if atomic_dedupe else None

    def summary(self, *, days: int, skill: str | None = None) -> dict[str, Any]:
        where = "created_at >= ?"
        params: list[Any] = [cutoff_iso(days)]
        if skill:
            where += " AND skill_name = ?"
            params.append(skill)
        with self.connect() as conn:
            has_actions = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_event_actions'").fetchone())
            attributed = "skill_name IS NOT NULL"
            if has_actions:
                attributed += " OR EXISTS (SELECT 1 FROM tool_event_actions a WHERE a.tool_event_id=tool_events.id)"
                if skill:
                    where = "created_at >= ? AND (skill_name = ? OR id IN (SELECT tool_event_id FROM tool_event_actions WHERE skill_name = ?))"
                    params = [cutoff_iso(days), skill, skill]
            tool_counts = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS tool_events,
                    COALESCE(SUM(is_error), 0) AS error_events,
                    COALESCE(SUM(CASE WHEN {attributed} THEN 1 ELSE 0 END), 0)
                        AS skill_events
                FROM tool_events
                WHERE {where}
                """,
                params,
            ).fetchone()
            action_count = 0
            if has_actions:
                action_where = "t.created_at >= ?" + (" AND a.skill_name = ?" if skill else "")
                action_params = [cutoff_iso(days), skill] if skill else [cutoff_iso(days)]
                action_count = conn.execute(
                    "SELECT COUNT(*) FROM tool_event_actions a JOIN tool_events t ON t.id=a.tool_event_id WHERE " + action_where,
                    action_params,
                ).fetchone()[0]
            turn_events = conn.execute(
                "SELECT COUNT(*) AS count FROM turn_events WHERE created_at >= ?",
                [cutoff_iso(days)],
            ).fetchone()["count"]
            session_events = conn.execute(
                "SELECT COUNT(*) AS count FROM session_events WHERE created_at >= ?",
                [cutoff_iso(days)],
            ).fetchone()["count"]
            links = "SELECT id, skill_name FROM tool_events WHERE skill_name IS NOT NULL"
            if has_actions:
                # Query-time UNION: deduplicate associations, never stored evidence.
                links += " UNION SELECT tool_event_id AS id, skill_name FROM tool_event_actions WHERE skill_name IS NOT NULL"
            skill_where = "t.created_at >= ?" + (" AND links.skill_name = ?" if skill else "")
            skill_params = [cutoff_iso(days), skill] if skill else [cutoff_iso(days)]
            skills = conn.execute(
                f"""
                SELECT links.skill_name, COUNT(*) AS event_count, COALESCE(SUM(t.is_error), 0) AS errors
                FROM ({links}) links JOIN tool_events t ON t.id=links.id
                WHERE {skill_where}
                GROUP BY links.skill_name
                ORDER BY event_count DESC, links.skill_name ASC
                LIMIT 20
                """,
                skill_params,
            ).fetchall()
            tools = conn.execute(
                f"""
                SELECT tool_name, COUNT(*) AS event_count, COALESCE(SUM(is_error), 0) AS errors
                FROM tool_events
                WHERE {where}
                GROUP BY tool_name
                ORDER BY event_count DESC, tool_name ASC
                LIMIT 20
                """,
                params,
            ).fetchall()
        return {
            "tool_events": int(tool_counts["tool_events"]),
            "error_events": int(tool_counts["error_events"]),
            "skill_events": int(tool_counts["skill_events"]),
            "tool_actions": int(action_count),
            "turn_events": int(turn_events),
            "session_events": int(session_events),
            "skills": [dict(row) for row in skills],
            "tools": [dict(row) for row in tools],
        }

    def recent_tool_events(
        self, *, days: int, skill: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_events)")}
            optional = [name for name in ("tool_call_id", "turn_id", "api_request_id", "status", "error_type") if name in columns]
            optional_sql = ", " + ", ".join(optional) if optional else ""
            has_actions = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_event_actions'").fetchone())
            if skill and has_actions:
                where = "created_at >= ? AND (skill_name = ? OR id IN (SELECT tool_event_id FROM tool_event_actions WHERE skill_name = ?))"
                params = [cutoff_iso(days), skill, skill]
            else:
                where = "created_at >= ?"
                params = [cutoff_iso(days)]
                if skill:
                    where += " AND skill_name = ?"
                    params.append(skill)
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT id, created_at, session_id, task_id, tool_name, duration_ms,
                       is_error, skill_name, args_json, result_preview{optional_sql}
                FROM tool_events
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            action_rows = {}
            if has_actions and rows:
                event_ids = [row["id"] for row in rows]
                action_rows = {
                    event_id: conn.execute(
                        "SELECT skill_name, action, succeeded FROM tool_event_actions WHERE tool_event_id = ?" + (" AND skill_name = ?" if skill else "") + " ORDER BY action_index",
                        (event_id, skill) if skill else (event_id,),
                    ).fetchall()
                    for event_id in event_ids
                }
        output = []
        for row in rows:
            data = dict(row)
            data.pop("id", None)
            for name in ("tool_call_id", "turn_id", "api_request_id", "status", "error_type"):
                data.setdefault(name, None)
            data["outcome_known"] = data["status"] in {"ok", "error", "blocked", "cancelled"}
            cleaned = clean_evidence_row(data, include_previews=self.include_legacy_previews)
            for name in ("tool_call_id", "turn_id", "api_request_id"):
                cleaned[name] = bounded_event_id(data[name]) if data[name] is not None else None
            for name in ("status", "error_type"):
                cleaned[name] = _compact(data[name], 128) if data[name] is not None else None
            cleaned["outcome_known"] = data["outcome_known"]
            cleaned["actions"] = [dict(action) for action in action_rows.get(row["id"], [])]
            output.append(cleaned)
        return output

    def recent_turns(self, *, days: int, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(turn_events)")}
            event_id_select = "event_id" if "event_id" in columns else "NULL AS event_id"
            rows = conn.execute(
                """
                SELECT created_at, session_id, model, platform, user_preview, assistant_preview, """ + event_id_select + """
                FROM turn_events
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [cutoff_iso(days), limit],
            ).fetchall()
        output = []
        for row in rows:
            data = clean_evidence_row(dict(row), include_previews=self.include_legacy_previews)
            data["event_id"] = bounded_event_id(row["event_id"]) if row["event_id"] is not None else None
            output.append(data)
        return output
