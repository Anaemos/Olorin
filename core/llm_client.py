"""
core/llm_client.py — Routing, circuit breaker, and provider selection.

This is the brain of the provider cascade described in OLORIN_PROJECT.md
Section 7. It does NOT know how to talk to Groq or Ollama directly — it
only holds BaseProvider instances and decides WHICH one gets a given
request, based on:

  1. Post-retrieval complexity scoring (query + retrieved context, NOT
     query alone — see score_complexity() below)
  2. Circuit breaker state (has Groq been rate-limited recently?)
  3. Explicit CLI override (--provider groq / --provider ollama / --force-local)

V1 SCOPE NOTE: the doc's Section 2 lore already assigns Faramir to
reasoning and Boromir to code/tools, but true specialist routing between
them is a V4 feature (LangGraph refactor). For V1, the automatic cascade
falls back to a single default local model — Boromir — since V1's tool
set (search_codebase, read_file, list_files) is entirely code-focused,
which is squarely Boromir's lane per the lore. Faramir is still reachable
via explicit --provider faramir for manual testing, just not part of the
automatic fallback path yet.
"""

import datetime

import config
from core.logging_config import get_logger
from memory import cache
from providers.base import BaseProvider, ProviderError, ProviderResponse
from providers.groq_provider import GroqProvider
from providers.ollama_provider import OllamaProvider

logger = get_logger("llm_client")

PLANNING_WORDS = (
    "design", "architecture", "compare", "tradeoffs",
    "approach", "structure", "optimize", "review", "why",
)


def score_complexity(query: str, retrieved_chunks: list[str] | None = None) -> int:
    """
    Post-retrieval complexity scorer (OLORIN_PROJECT.md Section 7).

    Deliberately scores AFTER retrieval, not on the raw query alone.
    "Explain auth" sounds trivial as a string, but if retrieval surfaces
    18 files and 8 middleware layers, the retrieved context is the real
    signal of difficulty — not the five words the user typed.

    Returns a raw score; caller compares against config.COMPLEXITY_THRESHOLD.
    """
    retrieved_chunks = retrieved_chunks or []

    score = 0
    score += len(retrieved_chunks) * 2  # more chunks = harder
    score += sum(len(c) for c in retrieved_chunks) // 500  # token volume proxy
    score += len(query.split()) // 10  # question length

    lowered = query.lower()
    score += sum(2 for w in PLANNING_WORDS if w in lowered)

    return score


class LLMClient:
    """
    Owns all provider instances and routes each request to the right one.

    Usage:
        client = LLMClient()
        response = client.chat(messages, query="...", retrieved_chunks=[...])
    """

    def __init__(self):
        self.groq: BaseProvider = GroqProvider()
        self.local_providers: dict[str, BaseProvider] = {
            # think=False: Boromir's role (Section 2) is fast, task-
            # focused tool execution, not deliberation. Now that its
            # base is qwen3:8b (moved 2026-07-09 for tool-call
            # reliability — see ENGINEERING_JOURNAL.md), it has a real
            # thinking mode that would otherwise add latency before every
            # tool call for no benefit here.
            "boromir": OllamaProvider(config.BOROMIR_MODEL, think=False),
            # think=True: Faramir IS the reasoning specialist (Section 2)
            # — deliberation is the point. Not yet reachable via automatic
            # routing (V1 SCOPE NOTE below), but set correctly now for
            # explicit --provider faramir use and for V4's specialist
            # routing.
            "faramir": OllamaProvider(config.FARAMIR_MODEL, think=True),
        }
        self.default_local = "boromir"  # see V1 SCOPE NOTE above

        # Circuit breaker state — see OLORIN_PROJECT.md Section 7.
        # None means "not tripped". A datetime means "skip Groq until this
        # time passes, go straight to local instead of retrying."
        self._groq_disabled_until: datetime.datetime | None = None

    def _groq_circuit_open(self) -> bool:
        if self._groq_disabled_until is None:
            return False
        return datetime.datetime.now() < self._groq_disabled_until

    def _trip_groq_circuit(self):
        cooldown = datetime.timedelta(minutes=config.CIRCUIT_BREAKER_COOLDOWN_MINUTES)
        self._groq_disabled_until = datetime.datetime.now() + cooldown

    def chat(
        self,
        messages: list,
        tools: list | None = None,
        query: str = "",
        retrieved_chunks: list[str] | None = None,
        force_provider: str | None = None,
        think_override: bool | None = None,
    ) -> ProviderResponse:
        """
        Route a chat request to the appropriate provider.

        Args:
            messages: OpenAI-format message list.
            tools: Optional tool schema list.
            query: The user's raw query — used for complexity scoring.
                   Only needed when force_provider is None.
            retrieved_chunks: Retrieved RAG context (strings) — used for
                   complexity scoring. Empty/None is fine before the
                   indexer exists (score will just be low, favoring local).
            force_provider: "groq" | "boromir" | "faramir" | None.
                   None means "let the complexity scorer decide" (the
                   --provider auto CLI default). Anything else bypasses
                   scoring entirely — this is what --force-local and
                   --provider <name> map to.
            think_override: Passed straight through to whichever provider
                   ends up handling the request — see BaseProvider.chat()'s
                   docstring. Extension point (added 2026-07-10) for a
                   future caller (e.g. Agent.run() on a task it knows is
                   heavy) to put a normally-fast persona into thinking
                   mode for just that one call — nothing in V1 uses this
                   yet, it exists so that need doesn't require touching
                   this routing layer again.

        Returns:
            ProviderResponse from whichever provider actually served the
            request (may differ from what was initially attempted, if a
            fallback occurred).

        Raises:
            ProviderError: only if EVERY eligible provider failed. In
                practice this should be rare — local Ollama is meant to
                always be available as the terminal fallback.
        """
        # Caching only applies to tool-free requests — a response that
        # involved tool calls is tied to live state (file contents, search
        # results) at that moment, and serving it stale later would be
        # actively wrong, not just outdated. See memory/cache.py docstring.
        cacheable = tools is None and query
        cache_key = None
        if cacheable:
            context_hash = cache.compute_context_hash(retrieved_chunks)
            cache_key = cache.compute_key(query, context_hash, force_provider or "auto")
            cached = cache.get(cache_key)
            if cached is not None:
                logger.info(
                    f"cache HIT | provider={cached['provider']} model={cached['model']} "
                    f"query={query[:60]!r}"
                )
                return ProviderResponse(
                    content=cached["answer"],
                    finish_reason="stop",
                    provider=cached["provider"],
                    model=cached["model"],
                    latency_ms=0,
                    input_tokens=cached["input_tokens"],
                    output_tokens=cached["output_tokens"],
                )

        response = self._route(messages, tools, query, retrieved_chunks, force_provider, think_override)

        logger.info(
            f"provider={response.provider} model={response.model} "
            f"latency_ms={response.latency_ms} query={query[:60]!r}"
        )

        if cacheable and cache_key:
            cache.set(
                key=cache_key, query=query, answer=response.content,
                provider=response.provider, model=response.model,
                latency_ms=response.latency_ms,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                retrieved_files=None,
                context_hash=cache.compute_context_hash(retrieved_chunks),
            )

        return response

    def _route(
        self,
        messages: list,
        tools: list | None,
        query: str,
        retrieved_chunks: list[str] | None,
        force_provider: str | None,
        think_override: bool | None = None,
    ) -> ProviderResponse:
        """Actual provider-selection logic, separated from chat() so
        caching/logging wraps this cleanly without duplicating routing
        rules."""
        if force_provider == "groq":
            return self.groq.chat(messages, tools=tools, think_override=think_override)

        if force_provider in self.local_providers:
            return self.local_providers[force_provider].chat(messages, tools=tools, think_override=think_override)

        # --- Auto mode: complexity scorer decides ---------------------
        score = score_complexity(query, retrieved_chunks)
        should_try_groq = score > config.COMPLEXITY_THRESHOLD

        if should_try_groq and not self._groq_circuit_open():
            try:
                return self.groq.chat(messages, tools=tools, think_override=think_override)
            except ProviderError as e:
                logger.warning(f"Groq failed, falling back to local: {e}")
                if e.is_rate_limit:
                    self._trip_groq_circuit()
                    logger.warning(
                        f"Groq circuit breaker tripped for "
                        f"{config.CIRCUIT_BREAKER_COOLDOWN_MINUTES} min"
                    )
                # Fall through to local on ANY Groq failure, not just
                # rate limits — a timeout or outage shouldn't block the
                # user when a local model is sitting right there.

        # Local fallback — either complexity was low, Groq's circuit is
        # open, or Groq just failed above.
        return self.local_providers[self.default_local].chat(messages, tools=tools, think_override=think_override)


if __name__ == "__main__":
    # Manual smoke test: python -m core.llm_client
    client = LLMClient()

    print("--- Low-complexity query (should route local / Boromir) ---")
    result = client.chat(
        messages=[{"role": "user", "content": "What does this function do?"}],
        query="What does this function do?",
        retrieved_chunks=["def foo(): return 1"],
    )
    print(f"Routed to: {result.provider} ({result.model}) | {result.latency_ms}ms")
    print(f"Response: {result.content}\n")

    print("--- High-complexity query (should route Groq) ---")
    result = client.chat(
        messages=[{"role": "user", "content": "Compare the architecture tradeoffs of this design"}],
        query="Compare the architecture tradeoffs of this design and why it was chosen over alternatives",
        retrieved_chunks=["chunk " * 100 for _ in range(10)],
    )
    print(f"Routed to: {result.provider} ({result.model}) | {result.latency_ms}ms")
    print(f"Response: {result.content}")
