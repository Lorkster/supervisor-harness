"""Credential redaction: the shapes the 2026-11 review found going through."""

from __future__ import annotations

import time

import pytest

from supervisor_harness.store.redaction import PLACEHOLDER, redact, redact_text

# Built from pieces so that this file does not itself contain a credential-shaped run.
KEY = "sk-" + "a1" * 20
JWT = ".".join([
    "eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N",
])


# -- containers ----------------------------------------------------------------


def test_tuples_are_redacted_and_stay_tuples() -> None:
    out = redact({"a": (KEY, "fine")})
    assert out == {"a": (PLACEHOLDER, "fine")}
    assert isinstance(out["a"], tuple)


def test_sets_and_bytes_are_redacted() -> None:
    assert redact({KEY, "ok"}) == {PLACEHOLDER, "ok"}
    assert redact(frozenset({KEY})) == frozenset({PLACEHOLDER})
    assert redact(f"token {KEY}".encode()) == f"token {PLACEHOLDER}".encode()


def test_a_credential_in_a_dict_key_is_redacted() -> None:
    assert redact({KEY: 1}) == {PLACEHOLDER: 1}


def test_numbers_none_and_nesting_are_left_alone() -> None:
    value = {"n": 1, "f": 1.5, "b": True, "none": None, "deep": [{"x": (1, 2)}]}
    assert redact(value) == value


# -- shapes ---------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "glpat-" + "A" * 22,
    "npm_" + "a1" * 18,
    "hf_" + "aB3" * 11,
    "ya29." + "a0_-" * 8,
    JWT,
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
])
def test_standalone_credential_shapes(text: str) -> None:
    assert redact_text(f"before {text} after") == f"before {PLACEHOLDER} after"


def test_a_json_quoted_authorization_header() -> None:
    out = redact_text('{"Authorization": "Bearer abc.def-123_456", "n": 1}')
    assert out == '{"Authorization": "Bearer [redacted]", "n": 1}'


def test_the_unquoted_authorization_header_still_works() -> None:
    assert redact_text("Authorization: Bearer abc123") == "Authorization: Bearer [redacted]"


def test_x_api_key_header() -> None:
    assert redact_text("x-api-key: abcdef123456") == "x-api-key: [redacted]"
    assert redact_text('{"X-Api-Key": "abcdef123456"}') == '{"X-Api-Key": "[redacted]"}'


def test_a_password_in_a_url() -> None:
    out = redact_text("clone postgres://admin:s3cr3tpw@db.example.com:5432/app now")
    assert out == "clone postgres://admin:[redacted]@db.example.com:5432/app now"


@pytest.mark.parametrize("line, kept", [
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "AWS_SECRET_ACCESS_KEY="),
    ("OPENAI_API_KEY='abcd1234abcd1234abcd'", "OPENAI_API_KEY='"),
    ('"client_secret": "0123456789abcdef0123"', '"client_secret": "'),
])
def test_assignments_to_secret_named_variables(line: str, kept: str) -> None:
    out = redact_text(line)
    assert out.startswith(kept) and PLACEHOLDER in out
    assert "EXAMPLEKEY" not in out and "abcd1234abcd" not in out and "0123456789abcdef" not in out


def test_a_private_key_header_alone_is_redacted_with_what_follows() -> None:
    body = "b3BlbnNzaC1rZXk" * 400                      # 6,000 characters, cut before any END
    text = "key follows: -----BEGIN OPENSSH PRIVATE KEY-----\n" + body + "\nordinary text"
    out = redact_text(text)
    assert out.startswith("key follows: " + PLACEHOLDER)
    assert len(out) <= len(text) - 4000, "the next 4 KB went with the header"
    assert out.endswith("ordinary text"), "and what is past that is left alone"


# -- false positives: ordinary prose and code are left as they are ---------------


@pytest.mark.parametrize("text", [
    "API_KEY = os.environ['OPENAI_API_KEY']",
    "api_key = config.get('api_key')",
    "token = 'changeme'",
    "max_tokens = 4096",
    "password: str = Field(min_length=12)",
    "the hf_model_name and npm_config settings, a token bucket, a secret garden",
    "Authorization is required for this endpoint",
    "see https://example.com:8080/path and ssh://git@github.com/org/repo and user@host:path",
    "git clone https://github.com/org/repo.git",
    "eyJ is how base64 starts a JSON object",
    "-----BEGIN PUBLIC KEY----- is not secret",
    "SECRET_KEY = settings.SECRET_KEY",
    "header = {'x-api-key': api_key}",
])
def test_ordinary_text_is_not_touched(text: str) -> None:
    assert redact_text(text) == text


# -- scanning is linear -----------------------------------------------------------


def test_a_megabyte_of_unmatched_private_key_headers_finishes() -> None:
    payload = "-----BEGIN RSA PRIVATE KEY-----\n" * 33_000        # ~1 MB, no END anywhere
    started = time.perf_counter()
    out = redact_text(payload)
    elapsed = time.perf_counter() - started
    assert PLACEHOLDER in out and len(out) < len(payload)
    # 552 KB took 14.5 s before; the bound is generous so a slow runner cannot flake it.
    assert elapsed < 10, f"{elapsed:.1f}s"


def test_a_megabyte_of_ordinary_text_finishes() -> None:
    payload = "def handler(request):  # nothing secret here, just a long line of code\n" * 15_000
    started = time.perf_counter()
    assert redact_text(payload) == payload
    assert time.perf_counter() - started < 10
