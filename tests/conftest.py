"""Shared test fixtures.

The fake provider answers every stage with schema-valid content, keyed off the
distinctive fields of each contract. That lets the full pipeline be exercised
end to end -- planning through the improvement loop -- without a network call or
a local model, so the orchestration logic is tested rather than a model's mood.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from supervisor_harness.config import (  # noqa: E402
    HarnessConfig,
    Policy,
    default_config,
)
from supervisor_harness.core.supervisor import Supervisor  # noqa: E402
from supervisor_harness.host.detect import HostInfo  # noqa: E402
from supervisor_harness.models import Backend, Usage  # noqa: E402
from supervisor_harness.providers.base import (  # noqa: E402
    CompletionRequest,
    CompletionResponse,
    Provider,
)
from supervisor_harness.providers.router import ModelRouter  # noqa: E402
from supervisor_harness.store.runstore import RunStore  # noqa: E402


class FakeProvider(Provider):
    """Answers each stage with plausible, schema-valid JSON."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.overrides: dict[str, dict[str, Any]] = {}
        # Answers consumed one per call, before `overrides` and the defaults.
        # A stage that must answer differently on successive visits -- a
        # checkpoint that fails and then passes, so remediation runs and the
        # run still terminates -- cannot be expressed by a fixed override.
        self.scripted: dict[str, list[Any]] = {}

    def script(self, stage: str, *payloads: Any) -> None:
        """Queue answers for `stage`, consumed in order, then fall through.

        An entry may be a payload dict, or a callable taking the request and
        returning one -- which is what a stage whose answer depends on the
        brief needs, since criterion ids are only knowable from the brief.
        """
        self.scripted.setdefault(stage, []).extend(payloads)

    def answer_for(self, stage: str, request: CompletionRequest) -> dict[str, Any]:
        """The payload this stage would return now, consuming any script."""
        queued = self.scripted.get(stage)
        if queued:
            entry = queued.pop(0)
            return dict(entry(request) if callable(entry) else entry)
        return self.overrides.get(stage) or getattr(self, f"_{stage}")(request)

    def failing_verification(self, request: CompletionRequest) -> dict[str, Any]:
        """Every mandatory criterion refused, with the ids the brief carries."""
        return {
            "results": [
                {"criterion_id": cid, "status": "fail",
                 "evidence": f"$ pytest -q\nexit=1\n1 failed (criterion {cid})"}
                for cid in _criterion_ids(request.messages[0].content)
            ],
            "summary": "The change does not do what the criteria require.",
            "regressions": ["the fence still lets an out-of-scope path through"],
        }

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        stage = self._classify(request)
        self.calls.append({"stage": stage, "system": request.system[:80]})
        payload = self.answer_for(stage, request)
        return CompletionResponse(
            text=json.dumps(payload),
            model="fake-1",
            provider=self.name,
            usage=Usage(input_tokens=100, output_tokens=50),
        )

    # -- stage classification ----------------------------------------------

    @staticmethod
    def _classify(request: CompletionRequest) -> str:
        props = set((request.json_schema or {}).get("properties", {}))
        if "lenses" in props:
            return "planning"
        if "recommended_mode" in props:
            return "synthesis"
        if "drift_score" in props:
            return "drift"
        if "results" in props:
            return "verification"
        if "scope_fidelity" in props:
            return "checkpoint"
        if "lessons" in props:
            return "improvement"
        if "criteria_progress" in props:
            return "execution"
        return "analysis"

    # -- canned answers ----------------------------------------------------

    def _planning(self, request: CompletionRequest) -> dict[str, Any]:
        return {
            "restated_goal": "Add rate limiting to the public login endpoint",
            "mode": "execute",
            "shared_context": "Python service, pytest suite, auth code under src/auth.",
            "lenses": [
                {
                    "role": "security",
                    "why": "The endpoint is unauthenticated and publicly reachable.",
                    "objectives": [
                        "Establish whether login is reachable without any rate limit",
                        "Identify the credential-stuffing path and its impact",
                    ],
                    "scope_paths": ["src/auth/**"],
                    "out_of_scope": ["billing"],
                },
                {
                    "role": "technical",
                    "why": "Needs a concrete implementation point.",
                    "objectives": [
                        "Identify where a limiter would attach in the request path",
                        "Determine the store needed for counters across processes",
                    ],
                    "scope_paths": ["src/**"],
                },
            ],
        }

    def _analysis(self, request: CompletionRequest) -> dict[str, Any]:
        security = "security" in request.messages[0].content.lower()[:400]
        if security:
            return {
                "output": (
                    "src/auth/login.py:34 accepts unlimited POST attempts with no counter. "
                    "An attacker can replay a credential list against it directly; the "
                    "trust boundary at the handler validates the password but not the rate. "
                    "Secrets are read from the environment and are not logged."
                ),
                "findings": [
                    {
                        "title": "Login endpoint has no rate limiting",
                        "detail": "src/auth/login.py:34 processes every POST with no counter, "
                                  "so credential stuffing is unconstrained.",
                        "severity": "high",
                        "evidence": ["src/auth/login.py:34"],
                        "recommendation": "Attach a per-IP and per-account limiter before the "
                                          "password check.",
                        "confidence": 0.9,
                    }
                ],
                "files_examined": ["src/auth/login.py"],
                "messages": [
                    {
                        "recipient": "*",
                        "kind": "finding",
                        "subject": "Login is unthrottled",
                        "content": "Whoever designs the limiter: it must key on account as "
                                   "well as IP, or rotation defeats it.",
                    }
                ],
                "status": "done",
                "self_assessment": "I did not check whether a WAF already throttles this "
                                   "upstream.",
            }
        return {
            "output": (
                "The request path is src/auth/login.py:34 behind the router in src/app.py:12. "
                "A limiter attaches cleanly as middleware there. Counters need a shared store "
                "because the service runs multiple workers; the existing Redis client in "
                "src/cache.py:8 is already available."
            ),
            "findings": [
                {
                    "title": "Limiter must use a shared counter store",
                    "detail": "The service runs multiple workers, so an in-process counter "
                              "would be trivially bypassed by hitting another worker.",
                    "severity": "medium",
                    "evidence": ["src/app.py:12", "src/cache.py:8"],
                    "recommendation": "Use the existing Redis client for counters.",
                    "confidence": 0.8,
                }
            ],
            "files_examined": ["src/app.py", "src/cache.py"],
            "status": "done",
        }

    def _synthesis(self, request: CompletionRequest) -> dict[str, Any]:
        return {
            "summary": "The login endpoint is unthrottled and reachable publicly. A "
                       "middleware limiter keyed on both IP and account, backed by the "
                       "existing Redis client, closes the credential-stuffing path.",
            "conflicts": [],
            "open_questions": ["Is there an upstream WAF already throttling this path?"],
            "recommended_mode": "execute",
            "tasks": [
                {
                    "title": "Add rate limiting to the login endpoint",
                    "action": "Add middleware in src/auth/login.py that limits login attempts "
                              "per IP and per account using the Redis client in src/cache.py.",
                    "motivation": "Credential stuffing against the login endpoint is currently "
                                  "unconstrained.",
                    # Named as the model names them: by title, because finding
                    # ids do not exist until the harness parses this answer.
                    "rationale_refs": [
                        "Login endpoint has no rate limiting",
                        "Limiter must use a shared counter store",
                    ],
                    "dod": [
                        {
                            "statement": "The login endpoint rejects the eleventh attempt from "
                                         "one IP within a minute with HTTP 429",
                            "method": "test",
                            "command": "pytest tests/test_rate_limit.py -q",
                            "expect": "0",
                            "mandatory": True,
                        },
                        {
                            "statement": "The limiter keys on account identifier as well as IP",
                            "method": "inspection",
                            "expect": "src/auth/login.py: account_key",
                            "mandatory": True,
                        },
                    ],
                    "scope_paths": ["src/auth/**", "tests/**"],
                    "suggested_role": "security-engineer",
                    "risk": "medium",
                    "effort": "medium",
                }
            ],
        }

    def _execution(self, request: CompletionRequest) -> dict[str, Any]:
        return {
            "output": "Added a Redis-backed limiter in src/auth/login.py keyed on account_key "
                      "and client IP, and covered it in tests/test_rate_limit.py.",
            "files_touched": ["src/auth/login.py", "tests/test_rate_limit.py"],
            "commands_run": ["pytest tests/test_rate_limit.py -q"],
            "criteria_progress": [{"criterion_id": "unknown", "claim": "met",
                                   "evidence": "suite passes locally"}],
            "status": "done",
        }

    def _verification(self, request: CompletionRequest) -> dict[str, Any]:
        ids = _criterion_ids(request.messages[0].content)
        return {
            "results": [
                {"criterion_id": cid, "status": "pass",
                 "evidence": f"$ pytest -q\nexit=0\n7 passed (criterion {cid})"}
                for cid in ids
            ],
            "summary": "All criteria verified against real output.",
            "regressions": [],
        }

    def _checkpoint(self, request: CompletionRequest) -> dict[str, Any]:
        """The passing checkpoint, so end-to-end tests reach completion.

        This default is the happy path on purpose, but it must not be the only
        reachable one: while it was, `_remediate`, the second-attempt verifier
        and the iteration bound had no coverage at all, and the guard that
        suppressed re-verification shipped broken. Use `script("checkpoint",
        FAILING_CHECKPOINT, ...)` to drive the failing branch.
        """
        return {
            "quality": 0.85,
            "scope_fidelity": 0.95,
            "completeness": 0.9,
            "passed": True,
            "gaps": [],
            "remediation": [],
            "avoidable_causes": [
                "The synthesis stage did not state which store to use, so the "
                "implementer had to infer it."
            ],
            "summary": "The limiter is implemented, tested and verified.",
        }

    def _improvement(self, request: CompletionRequest) -> dict[str, Any]:
        return {
            "lessons": [
                {
                    "category": "briefing",
                    "trigger": "The implementer had to infer the counter store.",
                    "statement": "Execution tasks must name the concrete dependency to use "
                                 "when analysis has already identified one",
                    "why": "Leaving the choice open invites an agent to introduce a second "
                           "mechanism alongside the existing one.",
                    "how_to_apply": "In the task action, name the module and symbol the "
                                    "implementer should reuse.",
                    "target": "implementer",
                    "confidence": 0.7,
                }
            ]
        }

    def _drift(self, request: CompletionRequest) -> dict[str, Any]:
        return {"on_task": True, "drift_score": 0.1, "signals": [],
                "summary": "on brief", "directive": "continue"}


def _criterion_ids(text: str) -> list[str]:
    import re

    return re.findall(r"`(dod_[A-Z0-9]+)`", text)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src" / "auth").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "auth" / "login.py").write_text(
        "def login(request):\n    # account_key is derived below\n"
        "    account_key = request.form['email']\n    return check(account_key)\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def config() -> HarnessConfig:
    cfg = default_config()
    cfg.backend = Backend.AUTONOMOUS
    cfg.routing = {k: "fake:fake-1" for k in cfg.routing}
    cfg.policy = Policy(
        default_max_turns=3,
        execution_max_turns=3,
        max_checkpoint_iterations=2,
        min_analysis_lenses=2,
        max_analysis_lenses=3,
    )
    return cfg


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def supervisor(workspace: Path, config: HarnessConfig, fake: FakeProvider) -> Supervisor:
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="test-host", workspace=str(workspace), confidence=1.0)
    router = ModelRouter(config, host_name=host.name)
    router.register("fake", fake)
    return Supervisor(workspace=workspace, config=config, store=store, host=host, router=router)
