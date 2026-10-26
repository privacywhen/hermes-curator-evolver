"""Pure, bounded helpers for preparing ranking inputs and checking responses."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .hygiene import scrub

_DEFAULT_TEXT_LIMIT = 512


def _text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    return scrub(value).strip()


def _body_without_frontmatter(text: str) -> str:
    """Skip a leading YAML-style frontmatter block without parsing its contents."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text.strip()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[index + 1 :]).strip()
    # An unterminated block has no reliable body boundary; don't guess.
    return ""


def bounded_text(text: str, limit: int = _DEFAULT_TEXT_LIMIT) -> str:
    """Scrub text, then return at most ``limit`` characters (without a suffix)."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    return scrub(text)[:limit]


def ranking_text(identity: str, description: str, text: str, *, limit: int = _DEFAULT_TEXT_LIMIT) -> str:
    """Build bounded identity/description/body text, excluding frontmatter noise."""
    identity = _text(identity, "identity")
    description = _text(description, "description")
    text = _text(text, "text")
    body = _body_without_frontmatter(text)

    # Keep useful document content when metadata or frontmatter is very large.
    labels = ("Identity: ", "\nDescription: ", "\nBody: ")
    overhead = sum(map(len, labels))
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    if limit <= overhead:
        return bounded_text(f"{identity}\n{description}\n{body}", limit)

    available = limit - overhead
    body_budget = available // 2
    metadata_budget = available - body_budget
    identity_budget = metadata_budget // 2
    description_budget = metadata_budget - identity_budget
    result = (
        labels[0] + identity[:identity_budget]
        + labels[1] + description[:description_budget]
        + labels[2] + body[:body_budget]
    )
    return bounded_text(result, limit)


def _array_list(raw: Any) -> Any:
    if hasattr(raw, "detach"):
        raw = raw.detach()
    if hasattr(raw, "cpu"):
        raw = raw.cpu()
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    return raw


def _sequence(raw: Any, name: str) -> list[Any]:
    raw = _array_list(raw)
    if isinstance(raw, (str, bytes, bytearray, Mapping)):
        raise TypeError(f"{name} must be a sequence")
    try:
        return list(raw)
    except TypeError as exc:
        raise TypeError(f"{name} must be a sequence") from exc


def _number(value: Any, name: str) -> float:
    if isinstance(value, (bool, str, bytes, bytearray)):
        raise TypeError(f"{name} must contain numeric values")
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError(f"{name} must contain numeric values") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{name} values must be finite")
    return converted


def _expected_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected_count must be an integer")
    if value < 0:
        raise ValueError("expected_count must be nonnegative")
    return value


def validate_vectors(raw: Any, *, expected_count: int) -> list[list[float]]:
    """Convert array-like vectors and enforce exact count, shape, and finiteness."""
    expected_count = _expected_count(expected_count)
    vectors = _sequence(raw, "vectors")
    if len(vectors) != expected_count:
        raise ValueError(f"expected {expected_count} vectors, got {len(vectors)}")
    result: list[list[float]] = []
    dimension: int | None = None
    for index, raw_vector in enumerate(vectors):
        vector = _sequence(raw_vector, f"vector {index}")
        if not vector:
            raise ValueError("vectors must have nonempty dimensions")
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise ValueError("vectors must have equal dimensions")
        converted = [_number(value, f"vector {index}") for value in vector]
        if not any(converted):
            raise ValueError("zero vectors are not valid ranking responses")
        result.append(converted)
    return result


def validate_scores(raw: Any, *, expected_count: int) -> list[float]:
    """Convert array-like scores and enforce exact finite cardinality."""
    expected_count = _expected_count(expected_count)
    values = _sequence(raw, "scores")
    if len(values) != expected_count:
        raise ValueError(f"expected {expected_count} scores, got {len(values)}")
    return [_number(value, "scores") for value in values]


def cosine_similarity(left: Any, right: Any) -> float:
    """Return cosine similarity using scale-first arithmetic to avoid overflow."""
    vectors = validate_vectors([left, right], expected_count=2)
    left_values, right_values = vectors
    left_scale = max(abs(value) for value in left_values)
    right_scale = max(abs(value) for value in right_values)
    left_scaled = [value / left_scale for value in left_values]
    right_scaled = [value / right_scale for value in right_values]
    dot = math.fsum(a * b for a, b in zip(left_scaled, right_scaled))
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left_scaled, right_scaled):
        left_norm = math.hypot(left_norm, left_value)
        right_norm = math.hypot(right_norm, right_value)
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))
