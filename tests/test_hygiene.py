"""Synthetic-only tests for the pure credential hygiene primitives."""

from hermes_curator_evolver.hygiene import (
    count_credentials,
    scrub,
    scrub_text,
    scrub_value,
)


GHP = "ghp_" + "x" * 40


def test_scrub_text_replaces_known_credential_families_and_counts_operations():
    raw = (
        f"PAT={GHP}; github_pat_{'A' * 58}; AKIA{'A' * 16}; "
        f"sk-proj-{'a' * 25}2; xoxb-{'a' * 14}"
    )
    cleaned, count = scrub_text(raw)
    assert count == 5
    assert GHP not in cleaned
    assert "github_pat_" not in cleaned
    assert "AKIA" not in cleaned
    assert "sk-proj-" not in cleaned
    assert "xoxb-" not in cleaned
    assert scrub_text(cleaned) == (cleaned, 0)
    assert scrub(raw) == cleaned


def test_count_credentials_counts_matching_occurrences_without_changing_input():
    text = f"{GHP} {GHP}"
    assert count_credentials(text) == 2
    assert text == f"{GHP} {GHP}"


def test_multiline_pem_and_authorization_headers_are_scrubbed():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nsynthetic-key-body\n-----END RSA PRIVATE KEY-----"
    header = "Authorization: opaque-auth-with-no-digits"
    cleaned, count = scrub_text(pem + "\n" + header)
    assert count == 2
    assert "synthetic-key-body" not in cleaned
    assert "opaque-auth-with-no-digits" not in cleaned


def test_generic_assignment_and_token_tails_scrub_but_benign_text_survives():
    raw = "password=opaque-pass-2026 token: abcdef12345678"
    cleaned, count = scrub_text(raw)
    assert count == 2
    assert "opaque-pass-2026" not in cleaned
    assert "abcdef12345678" not in cleaned

    benign = "password rotation task; token = see-below; id wJalrXtnUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert scrub_text(benign) == (benign, 0)


def test_explicit_credential_labels_redact_digit_free_values_without_recounting_markers():
    raw = 'password=hunter passwd: short secret="open sesame" api-key=abc'
    cleaned, count = scrub_text(raw)
    assert count == 4
    assert "hunter" not in cleaned
    assert "short" not in cleaned
    assert "open sesame" not in cleaned
    assert "api-key=abc" not in cleaned
    assert scrub_text(cleaned) == (cleaned, 0)


def test_marker_with_extra_secret_is_redacted_in_header_json_and_assignment():
    raw = (
        "Authorization: [REDACTED:old] opaque-auth [REDACTED:old]\n"
        'payload={"password":"prefix [REDACTED:old] opaque-json [REDACTED:old]"}\n'
        'password="prefix [REDACTED:old] opaque-assignment [REDACTED:old]"'
    )
    cleaned, count = scrub_text(raw)
    assert count == 3
    assert "opaque-auth" not in cleaned
    assert "opaque-json" not in cleaned
    assert "opaque-assignment" not in cleaned


def test_existing_redaction_markers_are_not_counted_again():
    for marker in (
        'password="[REDACTED:sensitive-field]"',
        "password='[REDACTED:generic-token-assignment]'",
        '{"password":"[REDACTED:sensitive-field]"}',
        "Authorization: [REDACTED:sensitive-field]",
    ):
        cleaned, count = scrub_text(marker)
        assert cleaned == marker
        assert count == 0


def test_scrub_value_recurses_and_redacts_sensitive_keys_and_headers():
    payload = {
        "session_id": "session-123",
        "token_usage": 12,
        "is_error": True,
        "message": "operation failed",
        "credentials": {"password": "shortOpaque", "api_key": "not-shaped"},
        "headers": {"Authorization": "Basic synthetic-auth", "X-Api-Key": "clearOpaque", "X-Trace-ID": "trace-42"},
        "events": [{"error": {"code": 7, "detail": f"leaked {GHP}"}}],
    }
    cleaned = scrub_value(payload)
    assert cleaned["session_id"] == "session-123"
    assert cleaned["token_usage"] == 12
    assert cleaned["is_error"] is True
    assert cleaned["message"] == "operation failed"
    assert cleaned["credentials"]["password"] == "[REDACTED:sensitive-field]"
    assert cleaned["credentials"]["api_key"] == "[REDACTED:sensitive-field]"
    assert cleaned["headers"]["Authorization"] == "[REDACTED:sensitive-field]"
    assert cleaned["headers"]["X-Api-Key"] == "[REDACTED:sensitive-field]"
    assert cleaned["headers"]["X-Trace-ID"] == "trace-42"
    assert GHP not in cleaned["events"][0]["error"]["detail"]
    assert cleaned["events"][0]["error"]["code"] == 7


def test_scrub_value_sanitizes_quoted_json_values_without_mutating_input():
    original = {"arguments": '{"password":"opaque", "name":"demo"}', "error": "denied"}
    cleaned = scrub_value(original)
    assert '"opaque"' not in cleaned["arguments"]
    assert '"password":"[REDACTED:sensitive-field]"' in cleaned["arguments"]
    assert cleaned["error"] == "denied"
    assert original["arguments"] == '{"password":"opaque", "name":"demo"}'


def test_scrub_text_handles_long_adversarial_input_linearly_and_preserves_unknown_prose():
    raw = ("ordinary prose " * 10000) + "password=secret-value-9"
    cleaned, count = scrub_text(raw)
    assert count == 1
    assert cleaned.endswith("password=[REDACTED:generic-token-assignment]")
    assert "ordinary prose " * 10000 in cleaned
