"""
providers/base.py — The interface every LLM provider implements.

This is the contract that makes LLMClient provider-agnostic. Groq, Ollama,
and (later) Together AI / Cerebras all speak this same shape, so the agent
loop and routing logic never need to know which provider actually answered.

Design principle (OLORIN_PROJECT.md Section 7): provider abstraction via
dependency inversion — high-level code (LLMClient, the agent loop) depends
on this abstract interface, not on any concrete provider's SDK quirks.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderResponse:
    """
    Normalized response shape returned by every provider, regardless of
    whether the underlying API is Groq, Ollama, or anything else added later.
    """

    content: str
    finish_reason: str  # "stop" | "tool_calls"
    provider: str  # e.g. "groq", "ollama"
    model: str  # e.g. "llama-3.3-70b-versatile", "boromir"
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list = field(default_factory=list)
    # Post-retrieval complexity score (core/llm_client.py's
    # score_complexity()) that drove the routing decision for this
    # response — set by LLMClient._route() in auto mode, AFTER the
    # provider itself constructs this object (providers have no
    # visibility into routing, only into answering). None whenever the
    # scorer never ran: forced-provider calls (--provider X,
    # --force-local) and cache hits. Added 2026-07-10 for episodic memory
    # (Section 10) — logging the real score, or an honest None, rather
    # than guessing.
    complexity_score: Optional[int] = None


class ProviderError(Exception):
    """Raised when a provider fails in a way callers need to react to
    (e.g. rate limit, timeout, connection refused). LLMClient catches this
    to decide whether to trip the circuit breaker and fall back."""

    def __init__(self, message: str, is_rate_limit: bool = False, is_daily_quota: bool = False):
        super().__init__(message)
        self.is_rate_limit = is_rate_limit
        # is_daily_quota (added 2026-07-19): a real gap found live — Groq
        # returns HTTP 413 (not 429) for "request would exceed remaining
        # daily token budget," which the openai SDK maps to a generic
        # APIStatusError, not RateLimitError, so this condition was
        # previously falling into a provider's catch-all exception
        # handler with is_rate_limit=False entirely, meaning it NEVER
        # tripped the circuit breaker. A daily quota also doesn't recover
        # in the standard 5-minute cooldown a per-minute limit would, so
        # even once correctly classified as a rate limit, it needs a
        # different (much longer) cooldown — see classify_quota_error()
        # below and config.DAILY_QUOTA_COOLDOWN_MINUTES.
        self.is_daily_quota = is_daily_quota


def classify_quota_error(message: str) -> tuple[bool, bool]:
    """
    Inspects a provider error message and returns (is_rate_limit,
    is_daily_quota) — shared between groq_provider.py and
    cerebras_provider.py so both classify quota-related failures the
    same way, rather than duplicating (and potentially drifting) the
    same keyword logic in two places.

    Content-based, not status-code-based, deliberately: the same
    underlying condition (a token/request budget exceeded) can surface
    as different HTTP status codes depending on provider and specific
    limit type — Groq's daily-budget case is a 413, its per-minute case
    is a 429; relying on status code alone would miss the 413 case
    exactly the way this project's code originally did, live, in
    production, before this function existed. Matched against Groq's and
    Cerebras's REAL observed error message shapes (both logged verbatim
    in OLORIN_PROJECT.md Section 13), not guessed at.
    """
    lowered = message.lower()
    is_quota_related = (
        "rate_limit_exceeded" in lowered
        or "token_quota_exceeded" in lowered
        or "quota" in lowered
        or "rate limit" in lowered
    )
    is_daily = (
        "tokens per day" in lowered
        or "requests per day" in lowered
        or " tpd" in lowered
        or " rpd" in lowered
    )
    return is_quota_related, is_daily


class BaseProvider(ABC):
    """
    Abstract interface for any LLM backend Olorin can route to.

    Every concrete provider (GroqProvider, OllamaProvider, ...) must
    implement both methods below. Nothing outside this file should
    import a provider's underlying SDK directly (e.g. `openai.OpenAI`) —
    that stays encapsulated inside the provider implementation.
    """

    @abstractmethod
    def chat(
        self,
        messages: list,
        tools: Optional[list] = None,
        think_override: Optional[bool] = None,
        options_override: Optional[dict] = None,
    ) -> ProviderResponse:
        """
        Send a chat completion request.

        Args:
            messages: OpenAI-format message list,
                      e.g. [{"role": "user", "content": "..."}]
            tools: Optional OpenAI-format tool/function schema list.
            think_override: Per-call override of a provider's default
                thinking-mode setting (see OllamaProvider's `think`
                constructor param). None means "use the provider's own
                default" — this exists as a deliberate extension point
                (added 2026-07-10) so a future caller can flip a
                normally-fast persona (e.g. Boromir) into thinking mode
                for a specific heavy task, without restructuring the
                provider/routing layers when that need actually arrives.
                Providers that have no concept of thinking mode (Groq)
                accept and ignore this.
            options_override: Per-call sampling parameters (temperature,
                top_p, num_predict, repeat_penalty, etc.) — added
                2026-07-21 alongside core/llm_client.py's per-persona
                _PERSONA_PARAMS split (Boromir: low temperature/short
                num_predict for fast, decisive execution; Faramir:
                higher temperature/longer num_predict for genuine
                exploration — see _call_local()'s docstring). Same
                extension-point philosophy as think_override: providers
                with no concept of per-call sampling overrides (Groq,
                Cerebras — both cloud, single-persona, no local-model
                notion of "which persona is this") accept and ignore
                this.

        Returns:
            ProviderResponse — normalized regardless of backend.

        Raises:
            ProviderError: on failure (rate limit, timeout, connection error).
                LLMClient is responsible for catching this and falling back
                to the next provider in the cascade.
        """
        raise NotImplementedError

    @abstractmethod
    def is_available(self) -> bool:
        """
        Cheap health check — does NOT count against rate limits or
        require a full chat completion. Used by LLMClient before
        attempting a real request, and by the circuit breaker logic.

        Returns:
            True if the provider is reachable and ready to serve requests.
        """
        raise NotImplementedError
