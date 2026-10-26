"""Pure credential-shaped text minimization helpers.

Adapted selectively from codeo1io/hermes-curator-evolver, licensed under
MIT, source revision ``c5aac56a6a902bff3d56a16cb0cfca88c31e45e3``. Reused
families and fixture vocabulary also draw on source commits
``27e94c84d81a7f0162a1b90161aacb22e8b78c65``,
``fe6610ac269e71bd7fceae6207bbc7857fdb807b``, and
``2de240b3fb5f4492648444c79c0dc0e70ea7b694``.

This module is deliberately stateless: it performs no I/O, network access,
logging, or global counter updates. It recognizes common credential shapes
and label patterns, not arbitrary private information. Detection is a
best-effort regex control, not a privacy guarantee; unknown token formats,
encoded secrets, free-form personal data, and novel labels may pass through.
Callers must minimize/allowlist data and scrub before truncation or
serialization. Replacement counts are per matched replacement operation,
not unique credentials. ``count_credentials`` is detection-only.

Copyright (c) codeo1io contributors. Distributed under the MIT License.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_REDACT = "[REDACTED:{}]"

# Patterns are bounded/simple; avoid nested ambiguous repetition on untrusted
# text. Specific token classes precede generic assignment labels.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-fine-grained-pat", re.compile(r"github_pat_[A-Za-z0-9_]{36,}")),
    ("github-token", re.compile(r"ghp_[A-Za-z0-9]{36,}\b")),
    ("github-oauth", re.compile(r"\bgho_[A-Za-z0-9]{36,}\b")),
    ("github-user-token", re.compile(r"\bghu_[A-Za-z0-9]{36,}\b")),
    ("github-server-token", re.compile(r"\bghs_[A-Za-z0-9]{36,}\b")),
    ("github-app-token", re.compile(r"\bgha_[A-Za-z0-9]{36,}\b")),
    ("github-refresh-token", re.compile(r"\bghr_[A-Za-z0-9]{76}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35,}(?![0-9A-Za-z_-])")),
    (
        "aws-secret-key",
        re.compile(
            r"(?i)\baws.{0,30}?secret(?:[_ ]access)?[_ ]?key\b[\"' ]*[:=][ ]?[\"']?"
            r"[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"
        ),
    ),
    (
        "pem-private-key-block",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
            r"(?s:.*?)-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
        ),
    ),
    ("pem-private-key-header", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")),
    (
        "bearer-jwt",
        re.compile(r"\bBearer\s+[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "openai-or-anthropic-key",
        re.compile(r"\bsk-(?:ant-)?(?=[A-Za-z0-9]*(?:-[A-Za-z0-9]*)?\d)[A-Za-z0-9_-]{20,}\b"),
    ),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Preserve the assignment label and redact the value (including an
    # optional quote) so JSON/text structure remains usable.
    (
        "generic-token-assignment",
        re.compile(
            r"(?i)(\b(?:token|secret(?:[_-]?key)?|password[0-9]*|passwd|api[_-]?(?:key|token))\s*[=:]\s*)"
            r"(?!\[)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,\s;}]+)"
        ),
    ),
)

_JSON_SENSITIVE_VALUE = re.compile(
    r'(?i)("(?:[^"\\]|\\.)*?\b(?:password[0-9]*|passwd|token|secret(?:[_-]?key)?|api[_-]?(?:key|token)|authorization|proxy-authorization)\b(?:[^"\\]|\\.)*"\s*:\s*)"(?:[^"\\]|\\.)*"'
)
_HEADER_SECRET = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization|x-api-key|api-key|x-auth-token)\s*:\s*)[^\r\n,;]+"
)
_ALREADY_REDACTED = re.compile(r"[\"']?\[REDACTED:[^\]\r\n]+\][\"']?$")
_SENSITIVE_KEY = re.compile(
    r"(?i)^(?:password[0-9]*|passwd|(?:access|refresh|id)?[_-]?token|secret(?:[_-]?key)?|(?:x-)?api[_-]?(?:key|token)|x-auth-token|authorization|proxy-authorization|client[_-]?secret|access[_-]?key)$"
)


def scrub_text(text: str) -> tuple[str, int]:
    """Return text with recognized credential shapes replaced and hit count."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    # Sensitive quoted JSON values are replaced in-place, preserving valid
    # JSON quoting and all unrelated formatting before the generic patterns.
    hits = 0

    def json_replacement(match: re.Match[str]) -> str:
        nonlocal hits
        if _ALREADY_REDACTED.fullmatch(match.group(0)[len(match.group(1)) :]):
            return match.group(0)
        hits += 1
        return match.group(1) + '"[REDACTED:sensitive-field]"'

    text = _JSON_SENSITIVE_VALUE.sub(json_replacement, text)

    def header_replacement(match: re.Match[str]) -> str:
        nonlocal hits
        if _ALREADY_REDACTED.fullmatch(match.group(0)[len(match.group(1)) :]):
            return match.group(0)
        hits += 1
        return match.group(1) + "[REDACTED:sensitive-field]"

    text = _HEADER_SECRET.sub(header_replacement, text)
    for name, pattern in _PATTERNS:
        marker = _REDACT.format(name)
        if name == "generic-token-assignment":
            replaced = 0

            def replace_assignment(match: re.Match[str]) -> str:
                nonlocal replaced
                raw_value = match.group(2)
                value = raw_value[1:-1] if len(raw_value) >= 2 and raw_value[0] in ('"', "'") else raw_value
                if _ALREADY_REDACTED.fullmatch(value):
                    return match.group(0)
                label_is_explicit_credential = re.match(
                    r"(?i)\s*(?:password[0-9]*|passwd|secret(?:[_-]?key)?|api[_-]?(?:key|token))\b",
                    match.group(1),
                ) is not None
                if label_is_explicit_credential or (len(value) >= 8 and any(char.isdigit() for char in value)):
                    quoted = raw_value[0] if len(raw_value) >= 2 and raw_value[0] in ('"', "'") else ""
                    replaced += 1
                    return match.group(1) + quoted + marker + quoted
                return match.group(0)

            text = pattern.sub(replace_assignment, text)
            hits += replaced
        else:
            text, count = pattern.subn(marker, text)
            hits += count
    return text, hits


def scrub(text: str) -> str:
    """Convenience wrapper returning only sanitized text."""
    return scrub_text(text)[0]


def count_credentials(text: str) -> int:
    """Count matches without modifying the input or recording telemetry."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    return scrub_text(text)[1]


def scrub_value(value: Any) -> Any:
    """Recursively scrub values and redact values under sensitive mapping keys.

    Mapping keys and non-sensitive identifiers/error fields are retained.
    JSON-encoded object/array strings are parsed, sanitized, and serialized
    back to valid compact JSON; ordinary strings are scrubbed as text. Tuples
    are returned as tuples, while lists and mappings are copied recursively.
    """
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            key_name = str(key)
            if _SENSITIVE_KEY.search(key_name):
                result[key] = "[REDACTED:sensitive-field]"
            else:
                result[key] = scrub_value(item)
        return result
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(item) for item in value)
    if isinstance(value, str):
        # Scrub each JSON scalar value as structured data first. This avoids
        # replacing the serialized representation with invalid unquoted text.
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return scrub(value)
        if isinstance(parsed, (dict, list)):
            sanitized = scrub_value(parsed)
            return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
        return scrub(value)
    return value
