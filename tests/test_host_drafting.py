"""P03 host-shaped fakes only; never contact a provider or execute model output."""
from hermes_curator_evolver.proposals import build_model_drafted_proposal
import json
import pytest


def test_markdown_shows_inert_draft_and_stage_not_an_unverified_safety_claim():
    from hermes_curator_evolver.proposals import format_proposal_markdown
    proposal = build_model_drafted_proposal({}, skill_name="synthetic", allow_evidence_egress=True,
                                          chat_backend=lambda prompt: "synthetic-inert-draft")
    text = format_proposal_markdown(proposal)
    assert "synthetic-inert-draft" in text
    assert "completed" in text
    assert "No files were changed" not in text


def test_injected_backend_without_explicit_egress_consent_is_never_called():
    calls = []
    result = build_model_drafted_proposal({}, skill_name="synthetic", chat_backend=lambda prompt: calls.append(prompt) or "synthetic draft")
    assert calls == []
    assert result["model_draft"]["executed"] is False


@pytest.mark.parametrize("response", [None, 3, {"text": ""}, "X" * 16_001])
def test_malformed_or_oversized_completion_fails_without_claiming_success(response):
    result = build_model_drafted_proposal({}, skill_name="synthetic", allow_evidence_egress=True,
                                         chat_backend=lambda prompt: response)["model_draft"]
    assert result["attempted"] and result["response_received"]
    assert result["completed"] is False and result["executed"] is False
    assert result["stage"] == "failed" and result["text"] == ""


@pytest.mark.parametrize("failure", [TimeoutError, PermissionError, RuntimeError])
def test_host_failure_records_stage_not_sensitive_exception_detail(failure):
    def backend(prompt):
        raise failure("synthetic-private-content-must-not-escape")
    result = build_model_drafted_proposal({}, skill_name="synthetic", allow_evidence_egress=True,
                                         chat_backend=backend)["model_draft"]
    assert result["attempted"] and not result["completed"] and not result["response_received"]
    assert result["error"] == failure.__name__
    assert "synthetic-private-content" not in json.dumps(result)


def test_draft_input_output_bounds_and_scrubbing_are_explicit():
    calls = []
    token = "ghp_" + "G" * 40
    def backend(prompt):
        calls.append(prompt)
        return "response " + token + "R" * 5000
    result = build_model_drafted_proposal({}, skill_name="x" * 20_000, allow_evidence_egress=True,
                                         chat_backend=backend)["model_draft"]
    assert len(calls[0]) == 12_000 and result["input_truncated"] is True
    assert len(result["text"]) <= 4000 and result["output_truncated"] is True
    assert token not in result["text"]


def test_received_route_is_retained_when_text_validation_fails():
    response = {"text": None, "actual_route": {"provider": "synthetic-provider", "model": "synthetic-model"}}
    result = build_model_drafted_proposal({}, skill_name="synthetic", allow_evidence_egress=True,
                                         chat_backend=lambda prompt: response)["model_draft"]
    assert result["completed"] is False
    assert result["actual_route"] == response["actual_route"]


@pytest.mark.parametrize("timeout", [0, -1, 301])
def test_native_backend_rejects_invalid_timeout_without_touching_context(timeout):
    from hermes_curator_evolver.proposals import native_chat_backend
    with pytest.raises(ValueError):
        native_chat_backend(object(), timeout_seconds=timeout)


def test_standalone_refuses_model_request_but_preserves_deterministic_proposal(tmp_path, monkeypatch, capsys):
    from hermes_curator_evolver.__main__ import main
    from hermes_curator_evolver.storage import EvidenceStore
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(tmp_path / "synthetic.sqlite"))
    EvidenceStore()
    assert main(["propose", "--skill", "synthetic", "--draft-with-model", "--format", "json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["proposed_actions"] and result["mutation_allowed"] is False
    assert result["model_draft"]["stage"] == "unavailable"
    assert result["model_draft"]["attempted"] is False
