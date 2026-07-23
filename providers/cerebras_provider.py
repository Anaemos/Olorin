"""
providers/cerebras_provider.py — Cerebras backend implementation.

Cerebras exposes an OpenAI-compatible endpoint (https://api.cerebras.ai/v1),
so this mirrors groq_provider.py almost exactly — the same proof-of-
abstraction point that file's own docstring makes. The interesting
differences are documented inline below, not in the request/response
shape, which is identical.

UNLIKE Groq, Cerebras is OPTIONAL. Groq's key is required at config load
(config.py fails fast without it — Section 7's "fail fast, not mid-
request" principle). Cerebras is a second-tier cloud fallback a user may
or may not have signed up for, so this provider must construct cleanly
with no key present and simply report itself unavailable — never raise
at import/construction time the way config.py's Groq check does.

REAL, VERIFIED CAVEATS (researched 2026-07-13, not assumed from stale
training data — see OLORIN_PROJECT.md Section 13 for the full account):
  - Free-tier context is capped at 8,192 tokens — meaningfully tighter
    than Groq's 12,000 TPM budget that already caused bug #10's 413.
    No pre-emptive truncation is added here; a too-large request simply
    raises ProviderError like any other failure and LLMClient's existing
    cascade (Groq -> Cerebras -> local) falls through to the next
    provider, same mechanism bug #10 already generalized. Adding
    speculative size-guarding here before it's ever been measured to be
    a problem would be exactly the kind of premature complexity this
    project has consistently avoided (daemon deferral, capsule
    relevance-filtering deferral, Section 13).
  - The free-tier model catalog churns — confirmed via multiple
    independent sources within weeks of each other reporting different
    available models. Don't assume CEREBRAS_MODEL's default stays valid
    indefinitely; if it starts failing, check
    https://inference-docs.cerebras.ai/models/overview and update
    CEREBRAS_MODEL in .env — no code change needed, same pattern as
    GROQ_MODEL/BOROMIR_MODEL/FARAMIR_MODEL already being env-overridable.
"""

import time

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

import config
from providers.base import BaseProvider, ProviderError, ProviderResponse, classify_quota_error

CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"


class CerebrasProvider(BaseProvider):
    """
    Second-tier cloud fallback — free, fast (Cerebras's wafer-scale
    inference), but context-capped and catalog-volatile (see module
    docstring). Sits between Groq and local in the cascade
    (core/llm_client.py's _route()): tried only when Groq itself failed
    or its circuit breaker is open, on queries the complexity scorer
    already judged worth escalating to cloud in the first place.

    Together AI was the other originally-planned V2 cloud fallback
    (OLORIN_PROJECT.md Section 5/7) but is deliberately NOT implemented —
    its free signup credit was retired industry-side in July 2025, and it
    now requires a $5 minimum purchase. Decided 2026-07-13 to stay
    free-tier-only rather than build a provider class for a paid service;
    see Section 13 for the full reasoning. `providers/together_provider.py`
    remains an unbuilt stub for the same reason the doc's Section 6
    already marked it as one.
    """

    def __init__(self):
        # Constructed even with no key present — unlike Groq, this must
        # never raise here. is_available() is what callers check before
        # relying on this provider; an empty api_key just means any real
        # request will 401, which chat() below converts to a normal
        # ProviderError like everything else, letting the cascade fall
        # through cleanly if a caller ever tries anyway.
        self.client = OpenAI(
            api_key=config.CEREBRAS_API_KEY or "not-configured",
            base_url=CEREBRAS_BASE_URL,
        )
        self.model = config.CEREBRAS_MODEL

    def chat(
        self,
        messages: list,
        tools: list | None = None,
        think_override: bool | None = None,
        options_override: dict | None = None,
    ) -> ProviderResponse:
        # think_override/options_override accepted for BaseProvider
        # interface compatibility, no effect — Cerebras's hosted models
        # have no thinking-mode or per-persona sampling-override concept
        # to apply here (options_override exists for core/llm_client.py's
        # per-persona _PERSONA_PARAMS, which only ever targets the local
        # provider; same as groq_provider.py, see that file).
        start = time.monotonic()
        try:
            kwargs = {"model": self.model, "messages": messages}
            if tools:
                kwargs["tools"] = tools

            response = self.client.chat.completions.create(**kwargs)

        except RateLimitError as e:
            # Covers both a real per-minute 429 and (per Cerebras's own
            # docs) daily-token-quota exhaustion, which can surface the
            # same way. classify_quota_error() (providers/base.py, added
            # 2026-07-19 after finding Groq never distinguished this
            # either) inspects the message content to tell which one this
            # actually is — core/llm_client.py uses is_daily_quota to pick
            # a much longer circuit-breaker cooldown for the daily case,
            # since it won't recover in the standard 5-minute window a
            # per-minute limit would.
            _, is_daily = classify_quota_error(str(e))
            raise ProviderError(f"Cerebras rate limit/quota hit: {e}", is_rate_limit=True, is_daily_quota=is_daily)
        except (APIConnectionError, APITimeoutError) as e:
            raise ProviderError(f"Cerebras unreachable: {e}", is_rate_limit=False)
        except Exception as e:
            # Catches the 8K-context-exceeded case (a plain 400, not a
            # rate limit) along with anything else — same catch-all
            # philosophy as groq_provider.py, so a Cerebras-specific SDK
            # exception never leaks past the provider boundary.
            raise ProviderError(f"Cerebras request failed: {e}", is_rate_limit=False)

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
            provider="cerebras",
            model=self.model,
            latency_ms=latency_ms,
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            tool_calls=tool_calls,
        )

    def is_available(self) -> bool:
        """
        Cheap check, same philosophy as GroqProvider.is_available() — no
        real request sent. Returns False (not an error) when
        CEREBRAS_API_KEY simply isn't set, which is the normal state for
        anyone who hasn't opted into this optional fallback yet.
        """
        return bool(config.CEREBRAS_API_KEY) and self.client is not None


if __name__ == "__main__":
    # Manual smoke test: python -m providers.cerebras_provider
    provider = CerebrasProvider()
    print(f"Cerebras available: {provider.is_available()}")

    if not provider.is_available():
        print("No CEREBRAS_API_KEY set in .env — skipping live call.")
        print("Get a free key at https://cloud.cerebras.ai (no card required).")
    else:
        result = provider.chat([{"role": "user", "content": "Say 'hello from Cerebras' and nothing else."}])
        print(f"Response: {result.content}")
        print(f"Provider: {result.provider} | Model: {result.model} | Latency: {result.latency_ms}ms")
        print(f"Tokens: {result.input_tokens} in / {result.output_tokens} out")
