from hermes_curator_evolver import semantic
import json
import pytest


class RecordingEmbedding:
    def __init__(self, vectors=None):
        self.texts = None
        self.kwargs = None
        self.vectors = vectors

    def encode(self, texts, **kwargs):
        self.texts = list(texts)
        self.kwargs = kwargs
        if self.vectors is not None:
            return self.vectors
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.parametrize("component", ["embedding", "reranker"])
def test_model_load_failure_is_private_and_not_reported_as_execution(monkeypatch, component):
    _rows(monkeypatch, [{"skill_name": "a", "path": "a", "text": "body"}])
    def fail():
        raise RuntimeError("synthetic-private-detail")
    monkeypatch.setattr(semantic, "_load_embedding_backend" if component == "embedding" else "_load_reranker_backend", fail)
    backend = RecordingEmbedding() if component == "reranker" else None
    result = semantic.find_skill_candidates(query="body", skills_dir="unused", semantic=True,
        embedding_backend=backend, load_models=component == "embedding", load_reranker=component == "reranker")
    assert "synthetic-private-detail" not in json.dumps(result)
    assert result["stages"][component] == "failed"
    assert result["stage_details"][component]["loaded"] is False
    assert result["stage_details"][component]["attempted"] is False
    assert result["fallback"] == ("lexical" if component == "embedding" else "embedding")
    assert result["model_executed"] is (component == "reranker")
    assert result["candidates"][0]["skill_name"] == "a"


class RecordingReranker:
    def __init__(self, scores=None):
        self.pairs = None
        self.scores = scores

    def predict(self, pairs):
        self.pairs = list(pairs)
        return self.scores if self.scores is not None else [0.5 for _ in pairs]


def _rows(monkeypatch, rows):
    monkeypatch.setattr(semantic, "_read_skill_rows", lambda _directory: rows)


def test_semantic_texts_use_metadata_and_bounded_document_and_query(monkeypatch):
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_SEMANTIC_TEXT_LIMIT", "240")
    metadata = "meta" * 200
    body = "body-marker " + ("useful body " * 100)
    _rows(monkeypatch, [{
        "skill_name": "identity-marker",
        "description": "description-marker " + metadata,
        "path": "fixture/path",
        "text": "---\n" + "huge: " + metadata + "\n---\n" + body,
        "source": "catalog",
    }])
    backend = RecordingEmbedding()
    reranker = RecordingReranker()

    result = semantic.find_skill_candidates(
        query="query-marker " * 100,
        skills_dir="unused",
        semantic=True,
        limit=1,
        embedding_backend=backend,
        reranker_backend=reranker,
    )

    assert all(len(text) <= 240 for text in backend.texts)
    assert len(reranker.pairs) == 1
    assert all(len(part) <= 240 for pair in reranker.pairs for part in pair)
    document = backend.texts[1]
    assert "identity-marker" in document
    assert "description-marker" in document
    assert "body-marker" in document
    assert result["candidates"][0]["source"] == "catalog"


def test_bad_embedding_response_falls_back_with_truthful_failed_stage(monkeypatch):
    _rows(monkeypatch, [
        {"skill_name": "a", "path": "a", "text": "body"},
        {"skill_name": "b", "path": "b", "text": "body"},
    ])
    backend = RecordingEmbedding(vectors=[[1.0, 0.0]])

    result = semantic.find_skill_candidates(
        query="body", skills_dir="unused", semantic=True, limit=1,
        embedding_backend=backend,
    )

    assert result["mode"] == "semantic-fallback"
    assert [row["skill_name"] for row in result["candidates"]] == ["a"]
    assert result["stages"]["embedding"] == "failed"
    assert result["counts"] == {"eligible": 2, "considered": 2, "truncated": 1}
    assert result["batches"] == {"embedding": 1, "embedding_batches": 3, "embedding_texts": 3, "rerank_predict_calls": 0, "rerank_pairs": 0}
    assert "1 vectors" not in result.get("error", "")


def test_failed_reranker_keeps_embedding_ranking_and_discloses_failure(monkeypatch):
    _rows(monkeypatch, [
        {"skill_name": "b", "path": "b", "text": "body"},
        {"skill_name": "a", "path": "a", "text": "body"},
    ])
    reranker = RecordingReranker(scores=[float("nan")])

    result = semantic.find_skill_candidates(
        query="body", skills_dir="unused", semantic=True, limit=2,
        embedding_backend=RecordingEmbedding(), reranker_backend=reranker,
    )

    assert result["mode"] == "semantic-fallback"
    assert [row["skill_name"] for row in result["candidates"]] == ["a", "b"]
    assert result["stages"] == {"embedding": "completed", "reranker": "failed"}
    assert result["fallback"] == "embedding"
    assert result["batches"] == {"embedding": 1, "embedding_batches": 3, "embedding_texts": 3, "rerank_predict_calls": 1, "rerank_pairs": 2}


def test_zero_limit_does_not_call_models_and_reports_unattempted():
    backend = RecordingEmbedding()

    result = semantic.find_skill_candidates(
        query="body", skills_dir="unused", semantic=True, limit=0,
        embedding_backend=backend,
    )

    assert backend.texts is None
    assert result["stages"] == {"embedding": "not_attempted", "reranker": "not_attempted"}
    assert result["counts"] == {"eligible": 0, "considered": 0, "truncated": 0}


def test_semantic_cutoff_ties_are_stable_and_provenance_is_preserved(monkeypatch):
    _rows(monkeypatch, [
        {"skill_name": "z", "path": "z", "text": "same", "description": "desc", "source_id": "z-src"},
        {"skill_name": "a", "path": "a", "text": "same", "description": "desc", "source_id": "a-src"},
        {"skill_name": "m", "path": "m", "text": "same", "description": "desc", "source_id": "m-src"},
    ])
    result = semantic.find_skill_candidates(
        query="same", skills_dir="unused", semantic=True, limit=1,
        embedding_backend=RecordingEmbedding(),
    )

    assert result["counts"] == {"eligible": 3, "considered": 3, "truncated": 2}
    assert result["candidates"][0]["skill_name"] == "a"
    assert result["candidates"][0]["source_id"] == "a-src"
    assert result["stages"] == {"embedding": "completed", "reranker": "not_attempted"}


def test_injected_catalog_and_embedding_fallback_use_same_frozen_rows(tmp_path, monkeypatch):
    from hermes_curator_evolver.skill_catalog import read_skill_catalog
    path = tmp_path / "demo" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: original\n---\nbody", encoding="utf-8")
    catalog = read_skill_catalog(tmp_path)
    def forbidden(*args):
        raise AssertionError("must not rescan a resolved catalog")
    monkeypatch.setattr(semantic, "_read_skill_rows", forbidden)
    backend = RecordingEmbedding(vectors=[[1.0, 0.0]])
    result = semantic.find_skill_candidates(query="body", skills_dir=tmp_path,
        semantic=True, embedding_backend=backend, catalog=catalog)
    assert result["fallback"] == "lexical"
    assert result["stages"]["embedding"] == "failed"
    assert result["candidates"][0]["skill_name"] == "original"
