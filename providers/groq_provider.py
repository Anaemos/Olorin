"""
providers/groq_provider.py — Groq backend implementation.

Groq exposes an OpenAI-compatible endpoint, so we reuse the `openai` SDK
pointed at Groq's base URL instead of writing a bespoke HTTP client.
This keeps GroqProvider and OllamaProvider almost identical in shape —
proof that the BaseProvider abstraction (Section 7) is doing its job.
"""

import time

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

import config
from providers.base import BaseProvider, ProviderError, ProviderResponse

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


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

    def chat(self, messages: list, tools: list | None = None, think_override: bool | None = None) -> ProviderResponse:
        # think_override accepted for BaseProvider interface compatibility
        # but has no effect here — Groq's llama-3.3-70b-versatile has no
        # thinking-mode concept to toggle. See base.py's chat() docstring.
        start = time.monotonic()
        try:
            kwargs = {"model": self.model, "messages": messages}
            if tools:
                kwargs["tools"] = tools

            response = self.client.chat.completions.create(**kwargs)

        except RateLimitError as e:
            raise ProviderError(f"Groq rate limit hit: {e}", is_rate_limit=True)
        except (APIConnectionError, APITimeoutError) as e:
            raise ProviderError(f"Groq unreachable: {e}", is_rate_limit=False)
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
