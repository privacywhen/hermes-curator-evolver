import math

import pytest

from hermes_curator_evolver.ranking_inputs import (
    bounded_text,
    cosine_similarity,
    ranking_text,
    validate_scores,
    validate_vectors,
)


def test_ranking_text_skips_long_nested_frontmatter_and_keeps_metadata_and_body():
    frontmatter = "---\nname: canonical-name\nmetadata:\n  nested:\n    values:\n" + "      - filler\n" * 500 + "---\n"
    body = "Useful body opening with gateway recovery.\n" + ("details " * 1000)

    result = ranking_text("canonical-name", "A useful description", frontmatter + body)

    assert len(result) <= 512
    assert "canonical-name" in result
    assert "A useful description" in result
    assert "Useful body opening with gateway recovery." in result
    assert "filler" not in result


def test_ranking_text_preserves_multilingual_snippets():
    result = ranking_text("技能", "Descripción útil", "本文には検索の例があります。\nMore text.")

    assert "技能" in result
    assert "Descripción útil" in result
    assert "本文には検索の例があります。" in result


def test_ranking_text_caps_query_and_document_inputs_without_suffix():
    query = ranking_text("", "", "q" * 2000, limit=73)
    document = ranking_text("id", "description", "body " * 400, limit=91)

    assert len(query) <= 73
    assert len(document) <= 91
    assert "...[truncated" not in query + document


def test_bounded_text_sanitizes_before_clipping():
    token = "ghp_" + "A" * 36

    assert bounded_text("prefix " + token + " tail", 20) == "prefix [REDACTED:git"


@pytest.mark.parametrize("limit", [0, 1, 2, 8])
def test_bounded_text_obeys_tiny_limits(limit):
    result = bounded_text("敏感token=12345678 more", limit)

    assert len(result) <= limit


def test_bounded_text_default_is_512_and_exactly_capped():
    result = bounded_text("x" * 900)

    assert len(result) == 512
    assert result == "x" * 512


@pytest.mark.parametrize("raw", [None, [], [[1.0]], [[1.0], [2.0], [3.0]]])
def test_validate_vectors_rejects_missing_or_extra_response_count(raw):
    with pytest.raises((TypeError, ValueError)):
        validate_vectors(raw, expected_count=2)


@pytest.mark.parametrize("raw", [[[1.0], [2.0, 3.0]], [[], []], [["bad"]], [[float("nan")]], [[float("inf")]]])
def test_validate_vectors_rejects_malformed_or_nonfinite_vectors(raw):
    with pytest.raises((TypeError, ValueError)):
        validate_vectors(raw, expected_count=len(raw))


def test_validate_vectors_converts_array_like_without_ml_imports():
    class ArrayLike:
        def __init__(self, value):
            self.value = value

        def tolist(self):
            return self.value

    class TensorLike(ArrayLike):
        def detach(self):
            return self

        def cpu(self):
            return self

    assert validate_vectors(TensorLike([[1, 2], [3.5, 4]]), expected_count=2) == [
        [1.0, 2.0],
        [3.5, 4.0],
    ]


def test_validate_vectors_allows_empty_response_only_for_zero_expected_count():
    assert validate_vectors([], expected_count=0) == []
    with pytest.raises(ValueError):
        validate_vectors([], expected_count=1)


@pytest.mark.parametrize("raw", [None, [], [1.0], [1.0, float("nan")], [float("inf"), 2]])
def test_validate_scores_rejects_wrong_count_and_nonfinite_values(raw):
    with pytest.raises((TypeError, ValueError)):
        validate_scores(raw, expected_count=2)


def test_validate_scores_preserves_equal_scores_for_caller_tie_breaking():
    assert validate_scores([3.0, 3.0], expected_count=2) == [3.0, 3.0]


def test_validate_scores_converts_array_like():
    class ArrayLike:
        def tolist(self):
            return [0, 1.5]

    assert validate_scores(ArrayLike(), expected_count=2) == [0.0, 1.5]


def test_cosine_similarity_rejects_empty_unequal_nonfinite_and_zero_vectors():
    for left, right in [([], []), ([1], [1, 2]), ([0, 0], [1, 0]), ([float("nan")], [1]), ([1], [float("inf")])]:
        with pytest.raises((TypeError, ValueError)):
            cosine_similarity(left, right)


def test_cosine_similarity_is_stable_for_large_finite_values_and_ties():
    scale = 1e308

    assert cosine_similarity([scale, scale], [scale, scale]) == pytest.approx(1.0)
    assert cosine_similarity([scale, 0], [0, scale]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert math.isfinite(cosine_similarity([scale, -scale], [scale, scale]))
