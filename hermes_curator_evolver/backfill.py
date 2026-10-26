"""Import historical Hermes sessions into the curator evidence store."""

from __future__ import annotations

import importlib
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .paths import hermes_home
from .storage import EvidenceStore, _compact, bounded_identity_id, replay_key


def default_sessions_dir() -> Path:
    """Return the legacy JSON transcript directory under the active Hermes home."""

    return hermes_home() / "sessions"


def default_state_db_path() -> Path:
    """Return the current Hermes session database under the active Hermes home."""

    return hermes_home() / "state.db"


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return repr(value)


def _tool_call_id(call: dict[str, Any], index: int, message_index: int = 0) -> str:
    return str(call.get("id") or call.get("call_id") or call.get("tool_call_id") or f"tool-{message_index}-{index}")


def _tool_call_name_and_args(call: dict[str, Any]) -> tuple[str, Any]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(function.get("name") or call.get("name") or "")
    raw_args = function.get("arguments", call.get("arguments", {}))
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {"raw_arguments": raw_args}
    else:
        args = raw_args or {}
    return name, args


def _tool_results_by_call_id(messages: list[dict[str, Any]]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id") or message.get("id")
        if call_id:
            results[str(call_id)] = message.get("content")
    return results


def _tool_result_metadata_by_call_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id") or message.get("id")
        if not call_id:
            continue
        content = message.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = None
        if isinstance(content, dict):
            metadata[str(call_id)] = content
        else:
            metadata[str(call_id)] = message
    return metadata


def _tool_event_exists(store: EvidenceStore, *, session_id: str, task_id: str, tool_name: str) -> bool:
    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM tool_events
            WHERE session_id = ? AND task_id = ? AND tool_name = ?
            LIMIT 1
            """,
            (bounded_identity_id(session_id), bounded_identity_id(task_id), tool_name),
        ).fetchone()
    return row is not None


def _turn_event_exists(
    store: EvidenceStore,
    *,
    session_id: str,
    model: str,
    platform: str,
    user_message: str,
    assistant_response: str,
    event_id: str = "",
) -> bool:
    with store.connect() as conn:
        if event_id:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(turn_events)")}
            if "event_id" in columns:
                row = conn.execute(
                    "SELECT 1 FROM turn_events WHERE session_id = ? AND event_id = ? LIMIT 1",
                    (bounded_identity_id(session_id), str(event_id)),
                ).fetchone()
                if row is not None:
                    return True
                return False
        row = conn.execute(
            """
            SELECT 1 FROM turn_events
            WHERE session_id = ? AND model = ? AND platform = ?
              AND user_preview = ? AND assistant_preview = ?
            LIMIT 1
            """,
            (
                bounded_identity_id(session_id),
                _compact(model, 256),
                _compact(platform, 256),
                *store.turn_previews(user_message, assistant_response, event_id=event_id),
            ),
        ).fetchone()
    return row is not None


def _session_event_exists(store: EvidenceStore, *, session_id: str) -> bool:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM session_events WHERE session_id = ? LIMIT 1",
            (bounded_identity_id(session_id),),
        ).fetchone()
    return row is not None


def _iter_session_files(sessions_dir: Path, limit: int | None) -> list[Path]:
    files = sorted(sessions_dir.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if limit is not None and limit > 0:
        return files[:limit]
    return files


def _supports_compacted_history(get_messages: Any) -> bool:
    """Return whether a Hermes ``get_messages`` callable exposes compacted rows.

    Hermes added ``include_compacted`` in August 2026 so display-history rows
    archived by in-place compaction remain available to transcript consumers.
    Detect that capability before passing the keyword so older Hermes releases
    keep working without masking a ``TypeError`` raised inside ``get_messages``.
    """

    try:
        parameters = inspect.signature(get_messages).parameters
    except (TypeError, ValueError):
        return False
    return "include_compacted" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _collect_state_session_metadata(session_db: Any, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Read bounded metadata pages without opening any transcripts."""
    offset = 0
    page_size = 200
    sessions: dict[str, dict[str, Any]] = {}
    seen_pages: set[tuple[str, ...]] = set()
    for _ in range(1000):
        try:
            raw_rows = session_db.search_sessions(limit=page_size, offset=offset)
        except Exception as exc:
            result["source_failures"] += 1
            result["source_error"] = type(exc).__name__
            break
        if not isinstance(raw_rows, (list, tuple)):
            result["pagination_truncated"] = True
            result["pagination_error"] = "invalid session metadata page"
            break
        rows = list(raw_rows)
        if not rows:
            break
        page_ids: list[str] = []
        prior_count = len(sessions)
        for row in rows:
            if not isinstance(row, dict):
                result["pagination_truncated"] = True
                continue
            data = dict(row)
            session_id = str(data.get("id") or data.get("session_id") or "").strip()
            if not session_id:
                result["pagination_truncated"] = True
                continue
            page_ids.append(session_id)
            sessions.setdefault(session_id, data)
        signature = tuple(page_ids)
        if signature in seen_pages or (rows and not page_ids):
            result["pagination_truncated"] = True
            break
        seen_pages.add(signature)
        if len(sessions) == prior_count:
            result["pagination_truncated"] = True
            break
        if len(rows) < page_size:
            break
        previous_count = len(sessions)
        offset += len(rows)
        if len(sessions) == previous_count and len(set(page_ids)) < len(page_ids):
            result["pagination_truncated"] = True
            break
    else:
        result["pagination_truncated"] = True
    return list(sessions.values())


def _load_state_messages(session_db: Any, session_id: str) -> Any:
    get_messages = session_db.get_messages
    if _supports_compacted_history(get_messages):
        return get_messages(session_id, include_compacted=True)
    return get_messages(session_id)


def _session_time(data: dict[str, Any], fallback: datetime | None = None) -> datetime:
    return (
        _parse_dt(data.get("last_active"))
        or _parse_dt(data.get("ended_at"))
        or _parse_dt(data.get("last_updated"))
        or _parse_dt(data.get("started_at"))
        or _parse_dt(data.get("session_start"))
        or fallback
        or datetime.now(timezone.utc)
    )


def _import_session_data(
    data: dict[str, Any],
    *,
    evidence: EvidenceStore,
    result: dict[str, Any],
    session_dt: datetime,
    fallback_session_id: str,
    current_state_session: bool,
) -> None:
    session_id = str(data.get("session_id") or data.get("id") or fallback_session_id)
    model = str(data.get("model") or "")
    platform = str(data.get("platform") or data.get("source") or "")
    raw_messages = data.get("messages")
    messages = [message for message in raw_messages if isinstance(message, dict)] if isinstance(raw_messages, list) else []
    before = (
        result["tool_events_imported"],
        result["turn_events_imported"],
        result["session_events_imported"],
    )

    tool_results = _tool_results_by_call_id(messages)
    tool_metadata = _tool_result_metadata_by_call_id(messages)
    for message_index, message in enumerate(messages):
        raw_calls = message.get("tool_calls")
        calls = raw_calls if isinstance(raw_calls, list) else []
        for call_index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            tool_name, args = _tool_call_name_and_args(call)
            if not tool_name:
                continue
            call_id = _tool_call_id(call, call_index, message_index)
            turn_id = str(call.get("turn_id") or message.get("turn_id") or "")
            api_request_id = str(call.get("api_request_id") or message.get("api_request_id") or "")
            native_result = tool_metadata.get(call_id, {})
            message_id = str(message.get("id") or message.get("message_id") or message_index)
            task_id = bounded_identity_id(f"backfill:{bounded_identity_id(session_id)}:{bounded_identity_id(turn_id or api_request_id or message_id)}:{bounded_identity_id(call_id)}")
            result["events_attempted"] += 1
            try:
                inserted = evidence.record_tool_call(
                    tool_name=tool_name,
                    args=args,
                    result=tool_results.get(call_id, ""),
                    task_id=task_id,
                    session_id=session_id,
                    tool_call_id=call.get("tool_call_id") or call.get("id") or call.get("call_id"),
                    turn_id=turn_id or None,
                    api_request_id=api_request_id or None,
                    status=call.get("status") or message.get("status") or native_result.get("status"),
                    error_type=call.get("error_type") or message.get("error_type") or native_result.get("error_type"),
                    created_at=_iso(_parse_dt(message.get("timestamp")) or session_dt),
                    atomic_dedupe=True,
                )
            except Exception:
                result["events_failed"] += 1
                raise
            if inserted:
                result["tool_events_imported"] += 1
                result["events_imported"] += 1
            else:
                result["events_duplicates"] += 1

    pending_user: str | None = None
    for message_index, message in enumerate(messages):
        role = message.get("role")
        content = _content_text(message.get("content"))
        if role == "user" and content.strip():
            pending_user = content
        elif role == "assistant" and pending_user and content.strip():
            native_event_id = message.get("event_id")
            native_turn_id = message.get("turn_id")
            message_id = message.get("message_id") or message.get("id")
            event_id = (
                replay_key("event", native_event_id) if native_event_id else
                replay_key("turn", native_turn_id) if native_turn_id else
                replay_key("message", message_id) if message_id else replay_key("position", message_index)
            )
            result["events_attempted"] += 1
            try:
                duplicate = _turn_event_exists(
                    evidence,
                    session_id=session_id,
                    model=model,
                    platform=platform,
                    user_message=pending_user,
                    assistant_response=content,
                    event_id=event_id,
                )
            except Exception:
                result["events_failed"] += 1
                raise
            if duplicate:
                result["events_duplicates"] += 1
            else:
                try:
                    inserted = evidence.record_turn(
                        session_id=session_id,
                        user_message=pending_user,
                        assistant_response=content,
                        event_id=event_id,
                        model=model,
                        platform=platform,
                        created_at=_iso(_parse_dt(message.get("timestamp")) or session_dt),
                        atomic_dedupe=True,
                    )
                except Exception:
                    result["events_failed"] += 1
                    raise
                if inserted:
                    result["turn_events_imported"] += 1
                    result["events_imported"] += 1
                else:
                    result["events_duplicates"] += 1
            pending_user = None

    session_has_ended = not current_state_session or data.get("ended_at") is not None
    if session_has_ended:
        result["events_attempted"] += 1
    duplicate_session_end = False
    if session_has_ended:
        try:
            duplicate_session_end = _session_event_exists(evidence, session_id=session_id)
        except Exception:
            result["events_failed"] += 1
            raise
    if session_has_ended and duplicate_session_end:
        result["events_duplicates"] += 1
    elif session_has_ended:
        interrupted = str(data.get("end_reason") or "").casefold() in {
            "cancelled",
            "error",
            "interrupted",
            "timeout",
        }
        try:
            inserted = evidence.record_session_end(
                session_id=session_id,
                completed=not interrupted,
                interrupted=interrupted,
                model=model,
                platform=platform,
                created_at=_iso(
                    _parse_dt(data.get("ended_at"))
                    or _parse_dt(data.get("last_updated"))
                    or session_dt
                ),
                atomic_dedupe=True,
            )
        except Exception:
            result["events_failed"] += 1
            raise
        if inserted:
            result["session_events_imported"] += 1
            result["events_imported"] += 1
        else:
            result["events_duplicates"] += 1

    after = (
        result["tool_events_imported"],
        result["turn_events_imported"],
        result["session_events_imported"],
    )
    if after != before:
        result["sessions_imported"] += 1


def backfill_sessions(
    *,
    sessions_dir: str | Path | None = None,
    state_db: str | Path | None = None,
    store: EvidenceStore | None = None,
    days: int = 30,
    limit: int | None = None,
) -> dict[str, Any]:
    """Backfill evidence from current Hermes ``state.db`` or legacy JSON dumps.

    With no source override, the importer prefers the active Hermes profile's
    read-only ``state.db`` and falls back to legacy ``session_*.json`` files only
    when that database does not exist. ``request_dump_*.json`` files are debug
    request snapshots, not the durable session transcript contract, and are
    intentionally ignored.
    """

    if sessions_dir is not None and state_db is not None:
        raise ValueError("sessions_dir and state_db are mutually exclusive")
    if limit is not None and limit <= 0:
        raise ValueError("backfill limit must be positive; use None for an unbounded import")

    evidence = store or EvidenceStore()
    bounded_days = max(int(days or 1), 1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=bounded_days)
    legacy_dir = Path(sessions_dir).expanduser() if sessions_dir is not None else default_sessions_dir()
    state_path = Path(state_db).expanduser() if state_db is not None else default_state_db_path()
    use_state_db = sessions_dir is None and (state_db is not None or state_path.exists())
    source_type = "state_db" if use_state_db else "legacy_json"
    source_path = state_path if use_state_db else legacy_dir
    result: dict[str, Any] = {
        "source_type": source_type,
        "source_path": str(source_path),
        "state_db_path": str(state_path),
        "sessions_dir": str(legacy_dir),
        "db_path": str(evidence.db_path),
        "days": bounded_days,
        "limit": limit,
        "sessions_seen": 0,
        "sessions_selected": 0,
        "sessions_attempted": 0,
        "sessions_imported": 0,
        "sessions_duplicates": 0,
        "sessions_failed": 0,
        "sessions_skipped_old": 0,
        "files_failed": 0,
        "source_failures": 0,
        "source_error": None,
        "pagination_truncated": False,
        "events_attempted": 0,
        "events_imported": 0,
        "events_duplicates": 0,
        "events_failed": 0,
        "partial": False,
        "tool_events_imported": 0,
        "turn_events_imported": 0,
        "session_events_imported": 0,
    }
    if not source_path.exists():
        result["missing"] = True
        return result

    if use_state_db:
        session_db = None
        metadata = []
        try:
            SessionDB = getattr(importlib.import_module("hermes_state"), "SessionDB")
            session_db = SessionDB(db_path=state_path, read_only=True)
            metadata = _collect_state_session_metadata(session_db, result)
        except Exception as exc:
            result["source_failures"] += 1
            result["source_error"] = type(exc).__name__
        try:
            metadata = sorted(metadata, key=lambda item: _session_time(item), reverse=True)
            result["sessions_seen"] = len(metadata)
            recent = []
            for data in metadata:
                if _session_time(data) < cutoff:
                    result["sessions_skipped_old"] += 1
                else:
                    recent.append(data)
            selected = recent[:limit] if limit is not None else recent
            result["sessions_selected"] = len(selected)
            for data in selected:
                result["sessions_attempted"] += 1
                session_id = str(data.get("id") or data.get("session_id") or "")
                before = result["events_imported"]
                duplicates_before = result["events_duplicates"]
                try:
                    data["messages"] = _load_state_messages(session_db, session_id)
                    _import_session_data(
                        data,
                        evidence=evidence,
                        result=result,
                        session_dt=_session_time(data),
                        fallback_session_id="",
                        current_state_session=True,
                    )
                    if result["events_imported"] == before and result["events_duplicates"] > duplicates_before:
                        result["sessions_duplicates"] += 1
                except Exception:
                    result["sessions_failed"] += 1
                    result["partial"] = True
                    if result["events_imported"] > before:
                        result["sessions_imported"] += 1
        finally:
            if session_db is not None:
                try:
                    session_db.close()
                except Exception as exc:
                    result["source_failures"] += 1
                    result["source_error"] = type(exc).__name__
                    result["partial"] = True
        if result["source_failures"] or result["pagination_truncated"] or result["sessions_failed"]:
            result["partial"] = True
        return result

    for path in _iter_session_files(legacy_dir, limit):
        result["sessions_seen"] += 1
        result["sessions_attempted"] += 1
        try:
            data = _load_json(path)
        except (OSError, json.JSONDecodeError, UnicodeError):
            result["files_failed"] += 1
            result["sessions_failed"] += 1
            result["partial"] = True
            continue

        session_dt = _session_time(data, datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
        if session_dt < cutoff:
            result["sessions_skipped_old"] += 1
            result["sessions_attempted"] -= 1
            continue
        before = result["events_imported"]
        duplicates_before = result["events_duplicates"]
        try:
            _import_session_data(
                data,
                evidence=evidence,
                result=result,
                session_dt=session_dt,
                fallback_session_id=path.stem.replace("session_", ""),
                current_state_session=False,
            )
            if result["events_imported"] == before and result["events_duplicates"] > duplicates_before:
                result["sessions_duplicates"] += 1
        except Exception:
            result["sessions_failed"] += 1
            result["partial"] = True
            if result["events_imported"] > before and result["sessions_imported"] < result["sessions_attempted"]:
                result["sessions_imported"] += 1

    result["sessions_selected"] = result["sessions_attempted"]
    return result
