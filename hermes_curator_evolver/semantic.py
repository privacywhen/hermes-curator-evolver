"""Candidate generation for skill evolution review."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from .hygiene import scrub
from .ranking_inputs import bounded_text, cosine_similarity, ranking_text, validate_scores, validate_vectors

_EMBEDDING_MODEL = "Qwen3-Embedding-0.6B"
_EMBEDDING_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
_RERANKER_MODEL = "bge-reranker-v2-m3"
_RERANKER_MODEL_ID = "BAAI/bge-reranker-v2-m3"
_DEFAULT_SEMANTIC_DEVICE = "auto"
_DEFAULT_SEMANTIC_TEXT_LIMIT = 512


def semantic_model_plan() -> dict[str, str]:
    return {
        "embedding": _EMBEDDING_MODEL,
        "embedding_model_id": _EMBEDDING_MODEL_ID,
        "reranker": _RERANKER_MODEL,
        "reranker_model_id": _RERANKER_MODEL_ID,
        "purpose": "Candidate generation only: embed skill/evidence text, then rerank likely related skills before human/model review.",
        "default": "off; no model download is performed unless semantic execution is explicitly requested.",
        "runtime_device": _semantic_device() or "auto",
        "text_limit_chars": str(_semantic_text_limit()),
    }


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w-]+", text, flags=re.UNICODE) if len(token) > 1}


def _semantic_device() -> str | None:
    value = os.getenv("HERMES_CURATOR_EVOLVER_SEMANTIC_DEVICE", _DEFAULT_SEMANTIC_DEVICE).strip()
    if not value or value.lower() == "auto":
        return None
    return value


def _semantic_text_limit() -> int:
    raw = os.getenv("HERMES_CURATOR_EVOLVER_SEMANTIC_TEXT_LIMIT", str(_DEFAULT_SEMANTIC_TEXT_LIMIT)).strip()
    try:
        return max(200, int(raw))
    except ValueError:
        return _DEFAULT_SEMANTIC_TEXT_LIMIT


def _semantic_text(text: str) -> str:
    return bounded_text(text, _semantic_text_limit())


def _skill_files(skills_dir: Path) -> list[Path]:
    if not skills_dir.exists():
        return []
    return sorted(path for path in skills_dir.rglob("SKILL.md") if path.is_file())


def _skill_name(path: Path, skills_dir: Path) -> str:
    try:
        rel = path.parent.relative_to(skills_dir)
        if str(rel) != ".":
            return rel.parts[-1]
    except ValueError:
        pass
    return path.parent.name


def _read_skill_rows(skills_dir: Path) -> list[dict[str, Any]]:
    from .skill_catalog import read_skill_catalog, review_rows
    return review_rows(read_skill_catalog(skills_dir))


def _lexical_candidates(query: str, skills_dir: Path, limit: int, *, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    query_terms = _tokens(query)
    source_rows = _read_skill_rows(skills_dir) if rows is None else rows
    candidates: list[dict[str, Any]] = []
    for item in source_rows:
        terms = _tokens(str(item["text"]))
        overlap = sorted(query_terms & terms)
        score = len(overlap) / max(len(query_terms), 1)
        candidate = {key: value for key, value in item.items() if key != "text"}
        candidate.update({"score": round(score, 4), "reasons": [f"matched `{term}`" for term in overlap[:8]]})
        candidates.append(candidate)
    candidates.sort(key=lambda row: (-float(row["score"]), row["skill_name"], row["path"]))
    return candidates[:limit]


def _cosine(left: list[float], right: list[float]) -> float:
    return cosine_similarity(left, right)


def _encode(backend: Any, texts: list[str]) -> list[list[float]]:
    raw = backend.encode(
        texts,
        normalize_embeddings=False,
        show_progress_bar=False,
        batch_size=1,
        convert_to_tensor=False,
    )
    return validate_vectors(raw, expected_count=len(texts))


def _predict_rerank(backend: Any, pairs: list[tuple[str, str]]) -> list[float]:
    if hasattr(backend, "predict"):
        raw = backend.predict(pairs)
    elif callable(backend):
        raw = backend(pairs)
    else:
        raise TypeError("reranker backend must expose predict(pairs) or be callable")
    if hasattr(raw, "detach"):
        raw = raw.detach().cpu().tolist()
    elif hasattr(raw, "tolist"):
        raw = raw.tolist()
    return validate_scores(raw, expected_count=len(pairs))


def _load_embedding_backend() -> Any:
    from sentence_transformers import SentenceTransformer

    device = _semantic_device()
    return SentenceTransformer(_EMBEDDING_MODEL_ID, device=device)


def _load_reranker_backend() -> Any:
    from sentence_transformers import CrossEncoder

    device = _semantic_device()
    return CrossEncoder(_RERANKER_MODEL_ID, device=device)


def _semantic_candidates(
    *,
    query: str,
    skills_dir: Path,
    limit: int,
    embedding_backend: Any,
    reranker_backend: Any | None = None,
    model_downloaded: bool | str = False,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = _read_skill_rows(skills_dir) if rows is None else rows
    limit_chars = _semantic_text_limit()
    stages = {"embedding": "not_attempted", "reranker": "not_attempted"}
    details = {
        "embedding": {"loaded": True, "attempted": False, "completed": False, "failed": False},
        "reranker": {"loaded": reranker_backend is not None, "attempted": False, "completed": False, "failed": False},
    }
    base = {
        "query": query, "models": {"embedding": _EMBEDDING_MODEL, "reranker": _RERANKER_MODEL},
        "runtime": {"device": _semantic_device() or "auto", "text_limit_chars": limit_chars},
        "model_downloaded": model_downloaded, "model_executed": False, "reranker_executed": False,
        "stages": stages, "stage_details": details,
        "counts": {"eligible": len(rows), "considered": 0, "truncated": len(rows)},
        "batches": {"embedding": 0, "embedding_batches": 0, "embedding_texts": 0,
                    "rerank_predict_calls": 0, "rerank_pairs": 0},
        "candidates": [],
    }
    if not rows:
        base["counts"]["truncated"] = 0
        return {**base, "mode": "semantic"}

    documents = [ranking_text(str(row.get("skill_name", "")), str(row.get("description", "")),
                              str(row.get("text", "")), limit=limit_chars) for row in rows]
    texts = [bounded_text(query, limit_chars), *documents]
    stages["embedding"] = "attempted"
    details["embedding"]["attempted"] = True
    base["batches"].update({"embedding": 1, "embedding_batches": len(texts), "embedding_texts": len(texts)})
    try:
        vectors = _encode(embedding_backend, texts)
    except Exception:
        stages["embedding"] = "failed"
        details["embedding"]["failed"] = True
        candidates = _lexical_candidates(query, skills_dir, limit, rows=rows)
        base.update({"mode": "semantic-fallback", "fallback": "lexical", "model_executed": True,
                     "counts": {"eligible": len(rows), "considered": len(rows),
                                "truncated": max(0, len(rows) - len(candidates))}, "candidates": candidates})
        return base
    stages["embedding"] = "completed"
    details["embedding"]["completed"] = True
    base["model_executed"] = True
    query_vector, skill_vectors = vectors[0], vectors[1:]
    candidates = []
    for row, document, vector in zip(rows, documents, skill_vectors):
        candidate = {key: value for key, value in row.items() if key != "text"}
        candidate.update({"score": round(_cosine(query_vector, vector), 6),
                          "reasons": ["semantic embedding similarity"], "_ranking_text": document})
        candidates.append(candidate)
    candidates.sort(key=lambda row: (-float(row["score"]), str(row.get("skill_name", "")),
                                     str(row.get("path", ""))))
    candidates = candidates[:limit]
    base["counts"] = {"eligible": len(rows), "considered": len(rows), "truncated": len(rows) - len(candidates)}
    mode = "semantic"
    if reranker_backend is not None and candidates:
        pairs = [(bounded_text(query, limit_chars), str(item["_ranking_text"])) for item in candidates]
        stages["reranker"] = "attempted"
        details["reranker"]["attempted"] = True
        base["batches"].update({"rerank_predict_calls": 1, "rerank_pairs": len(pairs)})
        try:
            scores = _predict_rerank(reranker_backend, pairs)
        except Exception:
            stages["reranker"] = "failed"
            details["reranker"]["failed"] = True
            base["fallback"] = "embedding"
            mode = "semantic-fallback"
        else:
            stages["reranker"] = "completed"
            details["reranker"]["completed"] = True
            for item, score in zip(candidates, scores):
                item["embedding_score"] = item["score"]
                item["score"] = round(score, 6)
                item["reasons"].append("reranker relevance score (uncalibrated)")
            candidates.sort(key=lambda row: (-float(row["score"]), str(row.get("skill_name", "")),
                                             str(row.get("path", ""))))
            mode = "semantic-reranked"
            base["reranker_executed"] = True
    for item in candidates:
        item.pop("_ranking_text", None)
    base.update({"mode": mode, "candidates": candidates})
    return base


def find_skill_candidates(
    *,
    query: str,
    skills_dir: str | Path,
    semantic: bool = False,
    limit: int = 10,
    embedding_backend: Any | None = None,
    reranker_backend: Any | None = None,
    load_models: bool = False,
    load_reranker: bool = False,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Find candidate skills for review.

    Lexical mode is dependency-free. Semantic mode remains safe by default: it
    returns the execution plan unless a backend is injected by tests/callers or
    model loading is explicitly requested.
    """

    query = scrub(query)
    plan = semantic_model_plan()
    path = Path(skills_dir)
    bounded_limit = int(limit) if limit is not None else 10
    if bounded_limit < 0:
        raise ValueError("candidate limit must be nonnegative")
    if bounded_limit == 0:
        return {"mode": "semantic-plan" if semantic else "lexical", "query": query,
                "models": {"embedding": "not-used", "reranker": "not-used"},
                "model_downloaded": False, "model_executed": False, "candidates": [],
                "stages": {"embedding": "not_attempted", "reranker": "not_attempted"},
                "stage_details": {name: {"loaded": False, "attempted": False, "completed": False, "failed": False}
                                  for name in ("embedding", "reranker")},
                "counts": {"eligible": 0, "considered": 0, "truncated": 0},
                "batches": {"embedding": 0, "embedding_batches": 0, "embedding_texts": 0,
                            "rerank_predict_calls": 0, "rerank_pairs": 0}}
    if catalog is None:
        rows = _read_skill_rows(path)
    else:
        from .skill_catalog import review_rows
        rows = review_rows(catalog)
    idle = {
        "query": query,
        "models": {"embedding": plan["embedding"] if semantic else "not-used",
                   "reranker": plan["reranker"] if semantic else "not-used"},
        "runtime": {"device": _semantic_device() or "auto", "text_limit_chars": _semantic_text_limit()},
        "model_downloaded": False, "model_executed": False, "reranker_executed": False,
        "stages": {"embedding": "not_attempted", "reranker": "not_attempted"},
        "stage_details": {name: {"loaded": backend is not None, "attempted": False, "completed": False, "failed": False}
                          for name, backend in (("embedding", embedding_backend), ("reranker", reranker_backend))},
        "counts": {"eligible": len(rows), "considered": 0, "truncated": len(rows)},
        "batches": {"embedding": 0, "embedding_batches": 0, "embedding_texts": 0,
                    "rerank_predict_calls": 0, "rerank_pairs": 0}, "candidates": [],
    }
    if not semantic:
        candidates = _lexical_candidates(query, path, bounded_limit, rows=rows)
        return {**idle, "mode": "lexical", "candidates": candidates,
                "counts": {"eligible": len(rows), "considered": len(rows), "truncated": len(rows) - len(candidates)}}
    if embedding_backend is None and not load_models:
        return {**idle, "mode": "semantic-plan",
                "note": "Semantic execution is opt-in; no model was downloaded or run."}
    if not rows:
        return {**idle, "mode": "semantic"}

    embedding_load_attempted = embedding_backend is None and load_models
    if embedding_load_attempted:
        try:
            embedding_backend = _load_embedding_backend()
        except Exception as exc:
            candidates = _lexical_candidates(query, path, bounded_limit, rows=rows)
            idle["stages"]["embedding"] = "failed"
            idle["stage_details"]["embedding"].update(loaded=False, load_attempted=True, failed=True)
            return {**idle, "mode": "semantic-fallback", "fallback": "lexical",
                    "model_downloaded": "unknown", "error": type(exc).__name__, "candidates": candidates,
                    "counts": {"eligible": len(rows), "considered": len(rows), "truncated": len(rows) - len(candidates)}}
    reranker_load_attempted = reranker_backend is None and load_reranker
    reranker_load_error = None
    if reranker_load_attempted:
        try:
            reranker_backend = _load_reranker_backend()
        except Exception as exc:
            reranker_load_error = type(exc).__name__
    result = _semantic_candidates(query=query, skills_dir=path, limit=bounded_limit,
        embedding_backend=embedding_backend, reranker_backend=reranker_backend,
        model_downloaded="unknown" if embedding_load_attempted or reranker_load_attempted else False, rows=rows)
    result["stage_details"]["embedding"]["load_attempted"] = bool(embedding_load_attempted)
    result["stage_details"]["reranker"]["load_attempted"] = bool(reranker_load_attempted)
    if reranker_load_error is not None:
        result["stages"]["reranker"] = "failed"
        result["stage_details"]["reranker"].update(loaded=False, attempted=False, completed=False, failed=True)
        result["error"] = reranker_load_error
        if result["stages"]["embedding"] == "completed":
            result.update(mode="semantic-fallback", fallback="embedding")
    return result
