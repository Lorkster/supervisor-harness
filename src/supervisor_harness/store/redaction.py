"""Credential redaction for anything on its way into the event log.

The log records an agent's full output, and an agent reads files. A workspace
containing a ``.env``, a checked-in token or a config with a live key can put
that credential into a turn, and from there into ``events.jsonl``,
``state.json``, ``index.sqlite3`` and the run's report -- none of which anyone
thinks of as a place secrets live. Nothing filtered any of it.

What this is, exactly, because a redactor that is trusted for more than it does
is worse than none:

* It matches credential *shapes* that are unambiguous -- provider key prefixes
  that no other string uses, and ``Authorization`` headers. It does not guess
  from entropy or from variable names.
* It is a backstop, not containment. A credential in a shape not listed here
  goes into the log unchanged, and the answer to that is not to keep widening
  these patterns.

The narrowness is deliberate rather than lazy. This harness is routinely pointed
at code *about* credential handling, where an aggressive redactor would mangle
the analysis it was asked to produce -- rewriting the very lines a finding is
about. High-precision patterns keep the failure mode "missed a secret" rather
than "corrupted the work", and a finding is supposed to name a location rather
than reproduce the value anyway, so redacting a live-looking key costs the
report nothing it should have contained.
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "[redacted]"

#: How much of a private key is taken after its header when no END marker turns
#: up. A key is read through a size limit often enough that the END is simply not
#: in the text, and a header with the rest of the text left alone is a stored key.
_KEY_BODY = 4096

#: Each entry is a pattern and what replaces a match: a whole credential in a
#: shape nothing else uses, or a credential that follows a label worth keeping
#: (``\g<1>``, and ``\g<2>`` where something after it must survive). Keep
#: additions to that standard: a pattern that also matches ordinary prose is a
#: worse outcome than the secret it was added for.
#:
#: Every quantifier here is bounded or has a literal to stop on, and a header is
#: never allowed to scan to the end of the text: an unmatched private-key header
#: used to be tried against everything after it, which was quadratic in the number
#: of headers (138 KB took 0.6 s, 276 KB 2.6 s, 552 KB 14.5 s).
_REDACTED = PLACEHOLDER
_KEEP = r"\g<1>" + PLACEHOLDER
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI / Anthropic / OpenRouter style: a fixed prefix and a long tail.
    (re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}"), _REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"), _REDACTED),
    # GitHub: documented, fixed prefixes. GitLab, npm, Hugging Face, Google OAuth.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), _REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), _REDACTED),
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), _REDACTED),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), _REDACTED),
    (re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"), _REDACTED),
    # AWS access key ids, and Google API keys.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), _REDACTED),
    # Slack, Stripe.
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), _REDACTED),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), _REDACTED),
    # A JSON web token: three base64url segments, the first the encoding of `{"`.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), _REDACTED),
    # A private key block: from its header to its END marker, or -- when the text
    # was cut before the marker -- to the next few kilobytes. Bounded either way.
    (re.compile(
        r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----"
        rf"(?:[\s\S]{{0,{_KEY_BODY}}}?-----END [A-Z ]{{0,40}}PRIVATE KEY-----"
        rf"|[\s\S]{{0,{_KEY_BODY}}})"
    ), _REDACTED),
    # An Authorization header, keeping the scheme and dropping the credential.
    # Quoted too, so a header dumped as JSON (`"Authorization": "Bearer ..."`) goes.
    (re.compile(
        r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+)[^\s\"']+"
    ), _KEEP),
    # The value must look like a literal, as for assignments below, so that
    # `{'x-api-key': api_key}` in the code under review is left as code.
    (re.compile(
        r"(?i)\b(x-api-key[\"']?\s*[:=]\s*[\"']?)"
        r"(?=[A-Za-z0-9._~+/=-]{0,200}\d)[A-Za-z0-9._~+/=-]{12,}"
    ), _KEEP),
    # A password inside a URL: `scheme://user:password@host`.
    (re.compile(r"\b([a-z][a-z0-9+.-]{1,20}://[^\s:/@]{1,100}:)[^\s/@]{1,200}(@)"),
     _KEEP + r"\g<2>"),
    # A secret assigned to a variable named for it: `OPENAI_API_KEY=...`,
    # `AWS_SECRET_ACCESS_KEY: "..."`. The value must look like a literal secret --
    # sixteen or more token characters with a digit among them -- so that
    # `API_KEY = os.environ[...]` and `TOKEN = "changeme"` in the code under
    # review are left as the code they are.
    (re.compile(
        r"(?i)\b(\w{0,40}(?:secret_access_key|api_key|apikey|secret_key|access_key|secret"
        r"|password|passwd|auth_token|access_token|token)[\"']?\s*[:=]\s*[\"']?)"
        r"(?=[A-Za-z0-9/+_=-]{0,200}\d)[A-Za-z0-9/+_=-]{16,}"
    ), _KEEP),
)


def redact_text(value: str) -> str:
    """Replace any credential-shaped run in ``value``."""
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact(value: Any) -> Any:
    """Redact every string inside a JSON-shaped value, structure unchanged.

    Applied to an event payload before it is written, so what reaches the log is
    what every later reader sees -- the fold, the snapshot, the index and the
    report all derive from it, and filtering at one boundary is what makes that
    true of all four.

    Tuples, sets and bytes are walked as well as lists and dicts: a payload built
    by code rather than parsed from JSON can carry any of them, and the
    serialiser turns a tuple into a list on the way out -- after redaction had
    already skipped it.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {redact(k) if isinstance(k, str) else k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(redact(v) for v in value)
    if isinstance(value, (bytes, bytearray)):
        return type(value)(redact_text(bytes(value).decode("utf-8", "replace")).encode("utf-8"))
    return value
