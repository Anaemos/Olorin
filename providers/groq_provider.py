"""
providers/groq_provider.py — Groq backend implementation.

Groq exposes an OpenAI-compatible endpoint, so we reuse the `openai` SDK
pointed at Groq's base URL instead of writing a bespoke HTTP client.
This keeps GroqProvider and OllamaProvider almost identical in shape —
proof that the BaseProvider abstraction (Section 7) is doing its job.
"""

import time

from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError, OpenAI, RateLimitError

import config
from core.logging_config import get_logger
from providers.base import BaseProvider, ProviderError, ProviderResponse, classify_quota_error

logger = get_logger("groq_provider")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Real, observed failure mode (first logged as part of bug #10's story,
# OLORIN_PROJECT.md Section 13; hit again live on 2026-07-19 during
# cross-repo querying verification): llama-3.3-70b-versatile sometimes
# emits its tool call as literal text, e.g.
# '<function=search_codebase{"query": "..."}></function>', instead of
# using Groq's structured tool-calling output. Groq's OWN server-side
# validation then mis-parses that text into a "tool name" that includes
# the glued-on JSON arguments (e.g. 'search_codebase={"query": "..."}'),
# fails to find a matching registered tool, and rejects the request with
# a 400 carrying error.code == "tool_use_failed". This happens entirely
# inside Groq's own generation + validation — there's no malformed
# response for us to intercept and repair client-side the way
# OllamaProvider's fallback parser can for a different (Ollama-specific)
# quirk; by the time we see anything, Groq has already rejected the
# request outright.
#
# What IS fixable from here: GroqProvider.chat() never sets `temperature`
# in its request, so Groq's non-zero server default applies — the exact
# same fact memory/entities.py's extraction-variance finding already
# established for this project (2026-07-16: an identical prompt can
# legitimately return different output across separate calls). That
# means a plain retry of the SAME request has a real, non-trivial chance
# of drawing a clean structured tool call on the second attempt, for
# free — no schema change, no prompt change, nothing to get wrong.
# Bounded to exactly one retry, deliberately: this project's stated
# preference for circuit breakers over blanket retries (Section 13) is
# about not hammering a systemically rate-limited provider, which this
# isn't — a single, targeted retry for one specific, known-transient
# error code is a different, much narrower thing.
_GROQ_MAX_ATTEMPTS = 2


class GroqProvider(BaseProvider):
    """
    Cloud provider — Groq's llama-3.3-70b-versatile.

    This is the "Olorin" persona in the lore: the frontier brain, called
    only when the post-retrieval complexity scorer decides a query is
    hard enough to warrant it (or when explicitly forced via CLI flag).
    """

    def __init__(self):
        self.client = OpenAI(api_key=config.GROQ_API_KEY, base_url=GROQ_BASE_URL)
        self.model = config.GROQ_MODEL

    def chat(
        self,
        messages: list,
        tools: list | None = None,
        think_override: bool | None = None,
        options_override: dict | None = None,
    ) -> ProviderResponse:
        # think_override/options_override accepted for BaseProvider
        # interface compatibility but have no effect here — Groq's
        # llama-3.3-70b-versatile has no thinking-mode or per-persona
        # sampling-override concept to apply (options_override exists for
        # core/llm_client.py's per-persona _PERSONA_PARAMS, which only
        # ever targets the local provider). See base.py's chat() docstring.
        start = time.monotonic()
        kwargs = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools

        response = None
        for attempt in range(_GROQ_MAX_ATTEMPTS):
            try:
                response = self.client.chat.completions.create(**kwargs)
                break

            except RateLimitError as e:
                _, is_daily = classify_quota_error(str(e))
                raise ProviderError(f"Groq rate limit hit: {e}", is_rate_limit=True, is_daily_quota=is_daily)
            except (APIConnectionError, APITimeoutError) as e:
                raise ProviderError(f"Groq unreachable: {e}", is_rate_limit=False)
            except BadRequestError as e:
                # See _GROQ_MAX_ATTEMPTS' module-level comment for the
                # full story on this specific error shape and why a
                # single retry is the right response to it specifically
                # — every other BadRequestError (a genuinely malformed
                # request on OUR end, request-too-large, etc.) still
                # fails immediately, no retry, exactly as before.
                is_malformed_tool_call = "tool_use_failed" in str(e)
                if is_malformed_tool_call and attempt < _GROQ_MAX_ATTEMPTS - 1:
                    logger.info(
                        "Groq emitted a malformed tool call (known "
                        "llama-3.3-70b-versatile quirk, see "
                        "OLORIN_PROJECT.md Section 13) — retrying the "
                        "same request once before falling through to "
                        "the cascade."
                    )
                    continue
                raise ProviderError(f"Groq request failed: {e}", is_rate_limit=False)
            except APIStatusError as e:
                # Real gap found live (2026-07-19): Groq returns HTTP 413
                # (not 429) for "request would exceed remaining DAILY
                # token budget" — the openai SDK maps 413 to this generic
                # APIStatusError, not RateLimitError (confirmed directly
                # by reading the SDK's own status-code mapping, not
                # assumed), so this case was previously falling into the
                # catch-all Exception handler below with is_rate_limit=
                # False, meaning it NEVER tripped the circuit breaker.
                # Every subsequent complex query would re-attempt Groq,
                # hit the identical 413, and pay that latency again — for
                # the rest of the day, since a daily token budget doesn't
                # recover in the standard 5-minute cooldown a per-minute
                # limit would. classify_quota_error() (providers/base.py)
                # inspects the message content (not just relying on this
                # being an APIStatusError at all, which could also mean
                # something unrelated) to decide both whether this counts
                # as a rate limit AND whether it's specifically a daily
                # one — core/llm_client.py uses is_daily_quota to pick a
                # much longer cooldown (config.DAILY_QUOTA_COOLDOWN_MINUTES)
                # instead of the standard one.
                is_quota, is_daily = classify_quota_error(str(e))
                raise ProviderError(f"Groq request failed: {e}", is_rate_limit=is_quota, is_daily_quota=is_daily)
            except Exception as e:
                # Catch-all so a Groq-specific SDK exception never leaks past
                # the provider boundary — LLMClient only ever needs to know
                # about ProviderError.
                raise ProviderError(f"Groq request failed: {e}", is_rate_limit=False)

        latency_ms = int((time.monotonic() - start) * 1000)
        choice = response.choices[0]

        tool_calls = []
        if choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in choice.message.tool_calls
            ]

        return ProviderResponse(
            content=choice.message.content or "",
            finish_reason="tool_calls" if tool_calls else "stop",
            provider="groq",
            model=self.model,
            latency_ms=latency_ms,
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            tool_calls=tool_calls,
        )

    def is_available(self) -> bool:
        """
        Cheap check — does NOT send a chat completion (that would cost a
        real request against the daily/minute rate limit). Just confirms
        the API key is present and the client constructed successfully.

        A full outage or an exhausted quota will only surface on the real
        `chat()` call, which LLMClient catches via ProviderError and routes
        around. This method is a pre-flight sanity check, not a guarantee.
        """
        return bool(config.GROQ_API_KEY) and self.client is not None


if __name__ == "__main__":
    # Manual smoke test: python -m providers.groq_provider
    provider = GroqProvider()
    print(f"Groq available: {provider.is_available()}")

    result = provider.chat([{"role": "user", "content": "Say 'hello from Groq' and nothing else."}])
    print(f"Response: {result.content}")
    print(f"Provider: {result.provider} | Model: {result.model} | Latency: {result.latency_ms}ms")
    print(f"Tokens: {result.input_tokens} in / {result.output_tokens} out")
