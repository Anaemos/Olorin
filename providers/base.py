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


class ProviderError(Exception):
    """Raised when a provider fails in a way callers need to react to
    (e.g. rate limit, timeout, connection refused). LLMClient catches this
    to decide whether to trip the circuit breaker and fall back."""

    def __init__(self, message: str, is_rate_limit: bool = False):
        super().__init__(message)
        self.is_rate_limit = is_rate_limit


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
