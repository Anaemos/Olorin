"""
core/llm_client.py — Routing, circuit breaker, and provider selection.

This is the brain of the provider cascade described in OLORIN_PROJECT.md
Section 7. It does NOT know how to talk to Groq or Ollama directly — it
only holds BaseProvider instances and decides WHICH one gets a given
request, based on:

  1. Post-retrieval complexity scoring (query + retrieved context, NOT
     query alone — see score_complexity() below)
  2. Circuit breaker state (has Groq/Cerebras been rate-limited recently?)
  3. Explicit CLI override (--provider groq/cerebras/boromir/faramir or --force-local)

CASCADE (auto mode, Section 7): Groq -> Cerebras -> local (Boromir).
Cerebras (added 2026-07-13) is a second, optional cloud tier — tried
only when Groq fails or its circuit is open, and only if a
CEREBRAS_API_KEY is actually configured. Together AI was the other
originally-planned V2 cloud fallback but is deliberately not
implemented (its free tier was retired industry-side; see
providers/cerebras_provider.py and OLORIN_PROJECT.md Section 13).

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
from providers.cerebras_provider import CerebrasProvider
from providers.groq_provider import GroqProvider
from providers.ollama_provider import OllamaProvider

logger = get_logger("llm_client")

PLANNING_WORDS = (
    "design", "architecture", "compare", "tradeoffs",
    "approach", "structure", "optimize", "review", "why",
)

# Per-distinct-file-beyond-the-first weight for the multi-file detection
# term (added 2026-07-15, closing the half of the V2 "better complexity
# scoring" item deliberately deferred on 2026-07-14 — see
# score_complexity()'s docstring below and OLORIN_PROJECT.md Section 13).
# Same tunable-without-code-changes philosophy as COMPLEXITY_THRESHOLD,
# but kept as a module constant rather than env-configurable since it's
# an internal scoring weight, not an operational knob anyone's expected
# to tune per-deployment the way the threshold is.
MULTI_FILE_WEIGHT = 3


def score_complexity(
    query: str,
    retrieved_chunks: list[str] | None = None,
    retrieved_files: set[str] | list[str] | None = None,
) -> int:
    """
    Post-retrieval complexity scorer (OLORIN_PROJECT.md Section 7).

    Deliberately scores AFTER retrieval, not on the raw query alone.
    "Explain auth" sounds trivial as a string, but if retrieval surfaces
    18 files and 8 middleware layers, the retrieved context is the real
    signal of difficulty — not the five words the user typed.

    retrieved_files (added 2026-07-15): distinct file/module identifiers
    the retrieved_chunks actually came from — e.g. {"core/agent.py",
    "tools/search.py"} for a search_codebase result spanning two files,
    or capsule module names on step 1 (Agent.run() seeds both lists from
    the same capsule fetch, same as it already did for retrieved_chunks
    alone). This is a genuinely different signal than chunk COUNT: five
    chunks from one file (a single function explained five different
    ways by the retriever) is meaningfully less complex than five chunks
    spanning five files (a question that actually requires understanding
    how separate parts of the system relate) — raw chunk count alone
    can't distinguish those two cases, only file attribution can. Scored
    as (distinct_files - 1) * MULTI_FILE_WEIGHT rather than distinct_files
    directly, deliberately: touching exactly one file isn't a "spans
    multiple files" situation at all, so it should contribute zero here,
    not a baseline amount that then just stacks on top of the existing
    chunk-count term for no new information.

    Deferred narrower than this on 2026-07-14 alongside the capsule-
    seeding fix specifically because it needed retrieved_chunks to carry
    file attribution, which it didn't yet — this is that follow-up,
    scoped to exactly the gap that was named at the time (see
    OLORIN_PROJECT.md Section 13's 2026-07-14 entry).

    Returns a raw score; caller compares against config.COMPLEXITY_THRESHOLD.
    """
    retrieved_chunks = retrieved_chunks or []
    retrieved_files = retrieved_files or []

    score = 0
    score += len(retrieved_chunks) * 2  # more chunks = harder
    score += sum(len(c) for c in retrieved_chunks) // 500  # token volume proxy
    score += len(query.split()) // 10  # question length

    lowered = query.lower()
    score += sum(2 for w in PLANNING_WORDS if w in lowered)

    distinct_files = len(set(retrieved_files))
    if distinct_files > 1:
        score += (distinct_files - 1) * MULTI_FILE_WEIGHT

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
        # Optional second-tier cloud fallback (2026-07-13) — constructed
        # unconditionally (never fails without a key, unlike Groq), tried
        # in the cascade only when Groq itself failed/circuit-open AND
        # a real key is configured (self.cerebras.is_available()).
        # Together AI was the other originally-planned V2 fallback but is
        # deliberately not implemented — see providers/cerebras_provider.py's
        # module docstring and OLORIN_PROJECT.md Section 13.
        self.cerebras: BaseProvider = CerebrasProvider()
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
        # None means "not tripped". A datetime means "skip this provider
        # until this time passes". Cerebras gets its own independent
        # breaker (added 2026-07-13) — a Cerebras 429 says nothing about
        # Groq's health or vice versa, so a shared flag would be wrong.
        self._groq_disabled_until: datetime.datetime | None = None
        self._cerebras_disabled_until: datetime.datetime | None = None

    def _groq_circuit_open(self) -> bool:
        if self._groq_disabled_until is None:
            return False
        return datetime.datetime.now() < self._groq_disabled_until

    def _trip_groq_circuit(self):
        cooldown = datetime.timedelta(minutes=config.CIRCUIT_BREAKER_COOLDOWN_MINUTES)
        self._groq_disabled_until = datetime.datetime.now() + cooldown

    def _cerebras_circuit_open(self) -> bool:
        if self._cerebras_disabled_until is None:
            return False
        return datetime.datetime.now() < self._cerebras_disabled_until

    def _trip_cerebras_circuit(self):
        cooldown = datetime.timedelta(minutes=config.CIRCUIT_BREAKER_COOLDOWN_MINUTES)
        self._cerebras_disabled_until = datetime.datetime.now() + cooldown

    def chat(
        self,
        messages: list,
        tools: list | None = None,
        query: str = "",
        retrieved_chunks: list[str] | None = None,
        retrieved_files: set[str] | list[str] | None = None,
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
            retrieved_files: Distinct file/module identifiers the
                   retrieved_chunks came from (added 2026-07-15, see
                   score_complexity()'s docstring) — used for the
                   multi-file-detection scoring term. Empty/None just
                   means that term contributes zero, same graceful-
                   degradation shape as retrieved_chunks.
            force_provider: "groq" | "cerebras" | "boromir" | "faramir" | None.
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

        response = self._route(messages, tools, query, retrieved_chunks, retrieved_files, force_provider, think_override)

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
        retrieved_files: set[str] | list[str] | None,
        force_provider: str | None,
        think_override: bool | None = None,
    ) -> ProviderResponse:
        """Actual provider-selection logic, separated from chat() so
        caching/logging wraps this cleanly without duplicating routing
        rules."""
        if force_provider == "cerebras":
            # Same fallback pattern as forced-groq below — bug #10's fix
            # (2026-07-12) established that every forced-provider branch
            # must degrade to local on failure, never propagate a raw
            # exception up to the CLI. Doesn't pre-check is_available()
            # here (unlike the auto-mode cascade further down) since a
            # forced call is an explicit user request — if there's no key
            # configured, let the real 401 surface through chat() as a
            # normal ProviderError and fall back the same way any other
            # Cerebras failure would.
            try:
                return self.cerebras.chat(messages, tools=tools, think_override=think_override)
            except ProviderError as e:
                logger.warning(
                    f"Forced Cerebras call failed, falling back to "
                    f"{self.default_local}: {e}"
                )
                if e.is_rate_limit:
                    self._trip_cerebras_circuit()
                return self.local_providers[self.default_local].chat(
                    messages, tools=tools, think_override=think_override
                )

        if force_provider == "groq":
            # Real bug found + fixed (2026-07-12): this branch had NO
            # fallback at all, unlike the auto-mode Groq attempt below —
            # any Groq failure (rate limit, request-too-large, network)
            # propagated all the way up as an unhandled ProviderError and
            # crashed the CLI with a raw traceback. Went unnoticed while
            # forcing Groq explicitly was rare; became easy to hit the
            # moment direct-address routing ("Groq, ...") made forcing
            # Groq a normal, frequent thing to type. Mirrors the
            # auto-mode fallback below exactly, rather than inventing a
            # second fallback policy.
            try:
                return self.groq.chat(messages, tools=tools, think_override=think_override)
            except ProviderError as e:
                logger.warning(
                    f"Forced Groq call failed, falling back to "
                    f"{self.default_local}: {e}"
                )
                if e.is_rate_limit:
                    self._trip_groq_circuit()
                return self.local_providers[self.default_local].chat(
                    messages, tools=tools, think_override=think_override
                )

        if force_provider in self.local_providers:
            return self.local_providers[force_provider].chat(messages, tools=tools, think_override=think_override)

        # --- Auto mode: complexity scorer decides ---------------------
        score = score_complexity(query, retrieved_chunks, retrieved_files)
        should_try_groq = score > config.COMPLEXITY_THRESHOLD

        if should_try_groq and not self._groq_circuit_open():
            try:
                response = self.groq.chat(messages, tools=tools, think_override=think_override)
                # Attach the score that drove this decision — set here,
                # not inside GroqProvider, since providers have no
                # visibility into routing (see ProviderResponse's
                # complexity_score docstring in providers/base.py).
                response.complexity_score = score
                return response
            except ProviderError as e:
                logger.warning(f"Groq failed, trying Cerebras next: {e}")
                if e.is_rate_limit:
                    self._trip_groq_circuit()
                    logger.warning(
                        f"Groq circuit breaker tripped for "
                        f"{config.CIRCUIT_BREAKER_COOLDOWN_MINUTES} min"
                    )
                # Fall through to Cerebras (if configured) rather than
                # straight to local — same complexity gate, one more
                # cloud-tier attempt before giving up on cloud entirely.

        # Cerebras — second-tier cloud fallback (2026-07-13), attempted
        # under the identical should_try_groq gate: it's a cloud
        # escalation for queries the scorer already judged worth it, not
        # a separate routing tier with its own threshold. Only tried when
        # Groq didn't already succeed above (should_try_groq was true but
        # we're still here) AND a key is actually configured
        # (is_available() check, unlike the forced-provider branch above —
        # here it's a silent auto-routing decision, so skipping a
        # guaranteed-401 network call entirely is worth the extra check).
        if (
            should_try_groq
            and self.cerebras.is_available()
            and not self._cerebras_circuit_open()
        ):
            try:
                response = self.cerebras.chat(messages, tools=tools, think_override=think_override)
                response.complexity_score = score
                logger.info("Escalated to Cerebras after Groq unavailable/failed")
                return response
            except ProviderError as e:
                logger.warning(f"Cerebras failed, falling back to local: {e}")
                if e.is_rate_limit:
                    self._trip_cerebras_circuit()
                    logger.warning(
                        f"Cerebras circuit breaker tripped for "
                        f"{config.CIRCUIT_BREAKER_COOLDOWN_MINUTES} min"
                    )

        # Local fallback — either complexity was low, both cloud tiers'
        # circuits are open, neither is configured, or both just failed
        # above.
        response = self.local_providers[self.default_local].chat(messages, tools=tools, think_override=think_override)
        response.complexity_score = score
        return response


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
    print(f"Response: {result.content}\n")

    print("--- Multi-file query (same chunk count/volume as a low-complexity")
    print("    single-file case, but spans several files — should score")
    print("    higher via the multi-file term added 2026-07-15) ---")
    single_file_score = score_complexity(
        "what does this do?",
        retrieved_chunks=["def foo(): return 1"] * 3,
        retrieved_files={"core/agent.py"},
    )
    multi_file_score = score_complexity(
        "what does this do?",
        retrieved_chunks=["def foo(): return 1"] * 3,
        retrieved_files={"core/agent.py", "core/llm_client.py", "tools/search.py"},
    )
    print(f"Same query/chunks, 1 file:  score={single_file_score}")
    print(f"Same query/chunks, 3 files: score={multi_file_score}")
    assert multi_file_score > single_file_score, "multi-file term isn't contributing"
    print("OK — multi-file spread scores higher than single-file, same chunk volume")
