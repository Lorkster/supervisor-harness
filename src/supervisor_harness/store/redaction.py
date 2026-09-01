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

#: Each pattern matches a whole credential, in a shape nothing else uses. Keep
#: additions to that standard: a pattern that also matches ordinary prose is a
#: worse outcome than the secret it was added for.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI / Anthropic / OpenRouter style: a fixed prefix and a long tail.
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    # GitHub: documented, fixed prefixes.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    # AWS access key ids, and Google API keys.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    # Slack, Stripe.
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    # A private key block, from its header to the end of that line onward.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    # An Authorization header, keeping the scheme and dropping the credential.
    re.compile(r"(?i)\b(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)\S+"),
)


def redact_text(value: str) -> str:
    """Replace any credential-shaped run in ``value``."""
    for pattern in _PATTERNS:
        if pattern.groups:
            value = pattern.sub(lambda m: m.group(1) + PLACEHOLDER, value)
        else:
            value = pattern.sub(PLACEHOLDER, value)
    return value


def redact(value: Any) -> Any:
    """Redact every string inside a JSON-shaped value, structure unchanged.

    Applied to an event payload before it is written, so what reaches the log is
    what every later reader sees -- the fold, the snapshot, the index and the
    report all derive from it, and filtering at one boundary is what makes that
    true of all four.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
