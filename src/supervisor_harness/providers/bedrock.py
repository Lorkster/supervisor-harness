"""Amazon Bedrock provider, for autonomous runs against Bedrock-hosted models.

Only the **autonomous** backend needs this. In host-delegated mode -- the
default -- the harness is not in the model path at all: Claude Code reads
``CLAUDE_CODE_USE_BEDROCK`` and the AWS credentials and runs every packet
itself, and nothing here is imported or constructed. That is pinned by
``tests/test_bedrock_routing.py`` and stated in the README, because it is the
answer to most of what issue #31 asked for.

## Why this is not the Anthropic provider with a different ``base_url``

:class:`~supervisor_harness.providers.anthropic.AnthropicProvider` is a
hand-rolled ``httpx`` client that sends ``x-api-key``. Bedrock authenticates
with AWS SigV4 over a different URL shape, resolves credentials from the whole
AWS chain (environment, shared config, SSO, IMDS, assumed roles), and takes the
model id in the path rather than the body. None of that is a ``base_url`` swap.

## Why the SDK, and why it is an extra

Two options were costed: signing requests here with ``botocore``, or using the
Anthropic SDK's ``AsyncAnthropicBedrock``. The SDK was chosen so that the
request and response mapping and the credential chain are the vendor's to
maintain rather than ours -- the cost being that it pulls the SDK *and* boto3
*and* botocore.

That cost is why it is an optional extra. The package otherwise has exactly one
runtime dependency, which is a feature worth keeping for everyone not using
Bedrock. **Nothing in this module imports the SDK at module level**: importing
``providers.router`` on a default install must not fail, so the import happens
inside :meth:`BedrockProvider._sdk` and a missing dependency is reported as a
``ProviderError`` naming the extra to install rather than as an ``ImportError``
from somewhere in the middle of a run.
"""

from __future__ import annotations

import os
from typing import Any

from ..models import Usage
from .base import (
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
    schema_instruction,
)

# An inference-profile id rather than a bare model id: cross-region profiles are
# what most accounts are actually entitled to invoke, and the `:0` suffix is part
# of the identifier -- see `_parse_ref`, which splits a route on its first colon
# only so that this survives whole.
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

INSTALL_HINT = "pip install 'supervisor-harness[bedrock]'"

# Status codes worth a second attempt. Mirrors the Anthropic provider's list;
# 529 is Anthropic's own "overloaded".
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


class BedrockProvider(Provider):
    """Anthropic models on Bedrock, through the vendor SDK's Bedrock client."""

    name = "bedrock"

    def __init__(
        self,
        region: str = "",
        default_model: str = DEFAULT_BEDROCK_MODEL,
        profile: str = "",
        base_url: str = "",
    ) -> None:
        # AWS_REGION is what Claude Code's own Bedrock setup uses, so a user who
        # has already configured that host has usually set it. AWS_DEFAULT_REGION
        # is the older spelling and is still what many CI images set.
        self.region = (
            region
            or os.environ.get("AWS_REGION", "")
            or os.environ.get("AWS_DEFAULT_REGION", "")
        )
        # Only what was *configured*. `AWS_PROFILE` is deliberately not read
        # here: the SDK resolves the whole chain itself, so inferring it added
        # nothing -- and it broke the one setup it was most likely to meet.
        # `AsyncAnthropicBedrock` reads `AWS_BEARER_TOKEN_BEDROCK` as its
        # `api_key` and then refuses to be given AWS credentials as well:
        #
        #   ValueError: Cannot specify both `api_key` and AWS credentials
        #   (`aws_access_key`, `aws_secret_key`, `aws_session_token`, `aws_profile`)
        #
        # A machine with Claude Code's Bedrock token *and* an ordinary corporate
        # `AWS_PROFILE` therefore could not construct this provider at all. An
        # explicitly configured profile is still passed, and still conflicts --
        # but that is a choice the user made in a file, with a message naming it.
        self.profile = profile
        self.default_model = default_model or DEFAULT_BEDROCK_MODEL
        self.base_url = base_url
        self._client: Any = None

    # -- the dependency ----------------------------------------------------

    def _sdk(self) -> Any:
        """Return ``AsyncAnthropicBedrock``, or fail naming the extra.

        Imported here rather than at module scope so that a default install can
        import ``providers.router`` -- and therefore run at all -- without the
        SDK present.
        """
        try:
            from anthropic import AsyncAnthropicBedrock
        except ImportError as exc:  # pragma: no cover - exercised by a stubbed import
            raise ProviderError(
                self.name,
                "the bedrock provider needs an optional dependency that is not "
                f"installed: {INSTALL_HINT}",
            ) from exc
        return AsyncAnthropicBedrock

    def _bedrock(self) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {"aws_region": self.region}
            if self.profile:
                kwargs["aws_profile"] = self.profile
            if self.base_url:
                kwargs["base_url"] = self.base_url
            # The router already owns retries and the backoff between them
            # (`ModelRouter.complete`, `retries=`). Leaving the SDK's own retry
            # loop on would multiply the two, so an attempt here is one attempt.
            self._client = self._sdk()(max_retries=0, **kwargs)
        return self._client

    # -- Provider ----------------------------------------------------------

    async def available(self) -> bool:
        """Whether a call could be made, without making one.

        Both halves are ordinary configuration mistakes with unhelpful failure
        modes deep in a run, so `supervisor providers` reports them up front.
        """
        if not self.region:
            return False
        try:
            self._sdk()
        except ProviderError:
            return False
        return True

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not self.region:
            raise ProviderError(
                self.name,
                "no AWS region: set AWS_REGION, or the provider's `region` in a "
                "trusted config file",
            )

        system = request.system
        if request.json_schema:
            system = f"{system}\n\n{schema_instruction(request.json_schema)}".strip()

        # Same normalisation as the Anthropic provider: the Messages API wants
        # alternating turns beginning with "user", and empty content is rejected.
        messages = [
            {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
            for m in request.messages
            if m.content.strip()
        ]
        if not messages:
            messages = [{"role": "user", "content": "Proceed."}]
        if messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "Proceed."})

        kwargs: dict[str, Any] = {
            "model": request.model or self.default_model,
            "max_tokens": request.max_tokens or 4096,
            "temperature": request.temperature,
            "messages": messages,
            "timeout": request.timeout,
        }
        if system:
            kwargs["system"] = system
        if request.stop:
            kwargs["stop_sequences"] = request.stop
        # Provider params reach here as `extra`, exactly as they do for the
        # other providers. `params` is in PROTECTED_PROVIDER_KEYS, so an
        # untrusted workspace config cannot use this to replace `messages`.
        kwargs.update(request.extra)

        try:
            message = await self._bedrock().messages.create(**kwargs)
        except ProviderError:
            raise
        # Deliberately broad: the exception hierarchy here belongs to the SDK,
        # and an unrecognised one must still reach the router as a ProviderError
        # rather than escaping as itself and killing the run.
        except Exception as exc:
            raise self._as_provider_error(exc) from exc

        return self._to_response(message, kwargs["model"])

    # -- mapping -----------------------------------------------------------

    def _as_provider_error(self, exc: Exception) -> ProviderError:
        """Translate an SDK exception into the harness's own, preserving retryability.

        The router's fallback chain only retries what is marked retryable, so
        collapsing everything to non-retryable would silently disable fallback
        for the cases it exists to cover -- throttling above all, which is the
        single most likely Bedrock failure.
        """
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return ProviderError(
                self.name,
                f"HTTP {status}: {str(exc)[:300]}",
                retryable=status in RETRYABLE_STATUS,
            )
        # Connection and timeout errors carry no status and are always worth
        # another attempt; anything else is reported as-is and not retried.
        name = type(exc).__name__
        connection = name in {"APIConnectionError", "APITimeoutError"} or any(
            base.__name__ == "APIConnectionError" for base in type(exc).__mro__
        )
        return ProviderError(self.name, f"{name}: {str(exc)[:300]}", retryable=connection)

    def _to_response(self, message: Any, requested_model: str) -> CompletionResponse:
        blocks = list(getattr(message, "content", None) or [])
        text = "".join(
            getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text"
        )
        reasoning = "".join(
            getattr(b, "thinking", "") for b in blocks if getattr(b, "type", "") == "thinking"
        )
        usage = getattr(message, "usage", None)
        return CompletionResponse(
            text=text,
            reasoning=reasoning,
            model=getattr(message, "model", "") or requested_model,
            provider=self.name,
            usage=Usage(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            ),
            finish_reason=getattr(message, "stop_reason", "") or "",
        )

    def describe(self) -> dict[str, Any]:
        try:
            self._sdk()
            installed = True
        except ProviderError:
            installed = False
        return {
            "name": self.name,
            "region": self.region,
            "default_model": self.default_model,
            "dependency_installed": installed,
            "configured": bool(self.region) and installed,
            **({"profile": self.profile} if self.profile else {}),
            **({"base_url": self.base_url} if self.base_url else {}),
        }

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            finally:
                self._client = None


__all__ = ["DEFAULT_BEDROCK_MODEL", "INSTALL_HINT", "BedrockProvider"]
