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

CASCADE (auto mode, Section 7): Groq -> Cerebras -> local.
Cerebras (added 2026-07-13) is a second, optional cloud tier — tried
only when Groq fails or its circuit is open, and only if a
CEREBRAS_API_KEY is actually configured. Together AI was the other
originally-planned V2 cloud fallback but is deliberately not
implemented (its free tier was retired industry-side; see
providers/cerebras_provider.py and OLORIN_PROJECT.md Section 13).

V1 SCOPE NOTE, updated 2026-07-20: Boromir and Faramir were collapsed
to one loaded qwen3:8b model ("local:latest") this session — see
_call_local()'s docstring and config.LOCAL_MODEL. The old blocker on
auto-routing Faramir (she couldn't reliably tool-call at all, so any
auto-mode query that might need a tool was unsafe to send her way) is
gone: this model reliably emits structured tool_calls with think=True
(verified live). The auto-mode local fallback now calls
select_local_specialist(query) to pick think=True vs False per query
(see its docstring below) instead of a fixed default — live-verified
2026-07-21 via direct-address testing, real auto-mode-triggered traffic
still not observed. self.default_local was removed 2026-07-21, once
confirmed unused both internally (all three of its call sites now use
select_local_specialist()) and externally (cli.py's --force-local, its
only other consumer, now calls the same function directly instead of
hardcoding "boromir"). Both personas remain reachable via explicit
--provider boromir/faramir regardless of any of this.
"""

import datetime
import re

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

# Code/file-reference signal words for select_local_specialist() below —
# presence of these (or a code-shaped token) means the query is about
# touching actual code/files, which favors Boromir (think=False, fast
# execution). Kept separate from PLANNING_WORDS rather than merged into
# one list, since the two lists vote in opposite directions.
CODE_SIGNAL_WORDS = {
    "read", "file", "files", "diff", "changed", "change", "implement",
    "fix", "refactor", "codebase", "repo", "repository", "function",
    "class", "method", "import", "grep", "search", "code", "bug",
    "error", "test", "run", "execute",
}
_CODE_TOKEN_PATTERN = re.compile(
    r"`[^`]+`"                                              # backtick code/identifiers
    r"|\b[\w\-]+\.(py|js|ts|tsx|jsx|rs|md|json|ya?ml|toml"
    r"|txt|c|cpp|h|hpp|java|go|rb|sql)\b"                    # file.ext
    r"|\b\w+\(\)"                                            # function()-shaped tokens
    r"|[\w\-]+[/\\][\w\-/\\.]+",                             # path/like/tokens
    re.IGNORECASE,
)


def select_local_specialist(query: str) -> str:
    """
    Chooses which local persona ("boromir" think=False vs "faramir"
    think=True) handles an auto-mode query landing on local — added
    2026-07-20 alongside the Boromir/Faramir model collapse (see
    LLMClient._call_local()'s docstring and OLORIN_PROJECT.md Section
    13's V4 entry).

    Deliberately query-only, not post-retrieval like score_complexity()
    — this has to be decided before the call goes out, since it picks
    the `think` value for THIS call, not something to react to after the
    fact. Reuses PLANNING_WORDS (the same "sounds like reasoning/
    planning" signal score_complexity() already trusts) as the vote for
    Faramir; CODE_SIGNAL_WORDS/_CODE_TOKEN_PATTERN as the vote for
    Boromir. Higher score wins; ties and zero-signal queries default to
    Boromir (think=False) — a sensible default for an ambiguous/generic
    query, not a safety-critical choice: unlike before the model
    collapse (2026-07-20), Faramir can now reliably tool-call too, so a
    wrong guess here costs `think` depth/latency, not lost capability.

    Only reached for auto-mode/forced-cloud-fallback local calls (see
    _route()) — direct-address routing ("Boromir, ..."/"Faramir, ...")
    and --provider boromir/faramir bypass this entirely, since those are
    explicit user choices this function has no business overriding.
    """
    query_lower = query.lower()

    code_score = len(_CODE_TOKEN_PATTERN.findall(query))
    code_score += sum(1 for w in CODE_SIGNAL_WORDS if w in query_lower)

    reasoning_score = sum(1 for w in PLANNING_WORDS if w in query_lower)

    persona = "faramir" if reasoning_score > code_score else "boromir"
    logger.info(
        f"specialist routing: {persona} "
        f"(code_score={code_score} reasoning_score={reasoning_score})"
    )
    return persona


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
        # Collapsed to one loaded model (2026-07-20) — Boromir and
        # Faramir are now the SAME qwen3:8b weights ("local:latest"),
        # differentiated purely by the `think` flag per request, not by
        # separate Ollama model tags. Verified live: switching `think` on
        # an already-loaded model costs ~0.2s (noise) vs ~8s for a real
        # cold load — genuinely zero swap cost between personas now.
        self.local: BaseProvider = OllamaProvider(config.LOCAL_MODEL)
        # think value per persona label — the ONLY thing that still
        # distinguishes "boromir" from "faramir" at the routing layer.
        # Faramir: deliberation is the point (Section 2's lore). Boromir:
        # fast, task-focused execution, no thinking overhead by default.
        self._PERSONA_THINK = {"boromir": False, "faramir": True}
        # Sampling params per persona (2026-07-21) — reinforces the
        # think-flag split with parameters that actually change token
        # selection, not just deliberation length. Real, not cosmetic:
        # this project's own entity-extraction investigation (2026-07-16)
        # already found Groq's un-set temperature caused genuine
        # inconsistency on a classification-shaped task — direct,
        # measured evidence from inside this codebase that temperature
        # materially affects output reliability/variety, not a knob
        # picked for symmetry. Boromir: low temperature/top_p, short
        # num_predict — decisive, terse, matches "fast execution" lore
        # and protects tool-call reliability the same way a low-
        # temperature classification call would. Faramir: higher
        # temperature/top_p, long num_predict, a touch more
        # repeat_penalty — genuine room to explore alternatives and
        # finish a multi-angle answer instead of hard-stopping mid-
        # thought. Threaded through OllamaProvider.chat()'s
        # options_override param (added same day — see its docstring
        # for the think-vs-options placement gotcha: options go INSIDE
        # an `options` sub-object, unlike `think` which is top-level).
        self._PERSONA_PARAMS = {
            # num_predict deliberately NOT set for either persona
            # (removed 2026-07-21, was 400/1200) — a hard length cap is
            # exactly the wrong lever for local models that already give
            # up quality relative to cloud; Boromir's first real test
            # got cut off mid-sentence at the 400 ceiling, which is worse
            # than no differentiation at all. Length differentiation, if
            # it happens, should come from the persona actually choosing
            # to be terse (voice/system-prompt content, see
            # build_system_prompt()), not a hard ceiling truncating
            # mid-thought regardless of whether the answer was finished.
            "boromir": {"temperature": 0.2, "top_p": 0.9},
            "faramir": {
                "temperature": 0.65, "top_p": 0.95, "repeat_penalty": 1.15,
            },
        }
        # self.default_local REMOVED (2026-07-21) — was a fixed "boromir"
        # fallback persona, dead within this file since select_local_
        # specialist() replaced all three of its call sites (the two
        # forced-cloud-failure branches and the plain auto-mode local
        # fallback, see _route() below). Confirmed unused externally too:
        # cli.py's only other consumer of persona selection (--force-local)
        # used to hardcode "boromir" directly rather than reading this
        # attribute, and has itself been fixed today to call select_local_
        # specialist() the same way (see cli.py's ask() command).

        # Circuit breaker state — see OLORIN_PROJECT.md Section 7.
        # None means "not tripped". A datetime means "skip this provider
        # until this time passes". Cerebras gets its own independent
        # breaker (added 2026-07-13) — a Cerebras 429 says nothing about
        # Groq's health or vice versa, so a shared flag would be wrong.
        self._groq_disabled_until: datetime.datetime | None = None
        self._cerebras_disabled_until: datetime.datetime | None = None

    def _call_local(
        self,
        persona: str,
        messages: list,
        tools: list | None,
        think_override: bool | None,
    ) -> ProviderResponse:
        """
        Dispatches to the single collapsed local provider (2026-07-20),
        selecting `think` from the persona unless the caller supplied an
        explicit override (think_override stays the higher-priority
        extension point BaseProvider.chat() has always documented).

        Stamps response.model = persona afterward, overwriting whatever
        OllamaProvider naturally reports (the literal Ollama tag,
        "local:latest", now identical for both personas). Every
        downstream consumer of ProviderResponse.model — Agent.
        _backend_used(), episodic memory, the journal, entity
        extraction's force_provider selection — depends on this value
        actually distinguishing "boromir" from "faramir"; that
        distinction has to be applied here, at the routing layer, now,
        since the provider layer genuinely can't tell anymore (same
        "providers have no visibility into routing" boundary
        providers/base.py's complexity_score docstring already
        establishes for a different field).

        Also applies self._PERSONA_PARAMS[persona] (2026-07-21) as the
        sampling options for this call — see __init__'s comment for the
        reasoning. No override parameter for this yet, unlike
        think_override: nothing in this codebase currently needs to
        override a persona's sampling params for a single call, so this
        stays simple until that need actually shows up (same
        extension-point-when-needed philosophy documented elsewhere in
        this project, not built speculatively).
        """
        effective_think = (
            think_override if think_override is not None else self._PERSONA_THINK[persona]
        )
        # Logged BEFORE the call, not just after (response = ... below
        # already logs latency/provider on completion, via chat()) --
        # added specifically so a slow local call shows up as "in
        # progress" to anything watching the log stream in real time
        # (server/app.py's /ask/stream), rather than only becoming
        # visible once it's already finished.
        logger.info(f"requesting from {persona} (local)...")
        response = self.local.chat(
            messages, tools=tools, think_override=effective_think,
            options_override=self._PERSONA_PARAMS[persona],
        )
        response.model = persona
        return response

    def _groq_circuit_open(self) -> bool:
        if self._groq_disabled_until is None:
            return False
        return datetime.datetime.now() < self._groq_disabled_until

    def _trip_groq_circuit(self, is_daily_quota: bool = False) -> int:
        """
        Disables Groq for either the standard per-minute cooldown or the
        much longer daily-quota cooldown, depending on what actually
        failed. Returns the cooldown duration in minutes actually used,
        so callers can log an accurate message instead of assuming the
        standard duration (a real gap found live 2026-07-19 — see
        config.DAILY_QUOTA_COOLDOWN_MINUTES's docstring and
        providers/base.py's classify_quota_error()).
        """
        minutes = config.DAILY_QUOTA_COOLDOWN_MINUTES if is_daily_quota else config.CIRCUIT_BREAKER_COOLDOWN_MINUTES
        self._groq_disabled_until = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
        return minutes

    def _cerebras_circuit_open(self) -> bool:
        if self._cerebras_disabled_until is None:
            return False
        return datetime.datetime.now() < self._cerebras_disabled_until

    def _trip_cerebras_circuit(self, is_daily_quota: bool = False) -> int:
        """Same dual-duration logic as _trip_groq_circuit() — see its
        docstring."""
        minutes = config.DAILY_QUOTA_COOLDOWN_MINUTES if is_daily_quota else config.CIRCUIT_BREAKER_COOLDOWN_MINUTES
        self._cerebras_disabled_until = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
        return minutes

    def chat(
        self,
        messages: list,
        tools: list | None = None,
        query: str = "",
        retrieved_chunks: list[str] | None = None,
        retrieved_files: set[str] | list[str] | None = None,
        force_provider: str | None = None,
        think_override: bool | None = None,
        local_persona_hint: str | None = None,
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
            local_persona_hint: "boromir" | "faramir" | None (added
                   2026-07-22, closing the unaddressed-auto-mode gap
                   flagged in OLORIN_PROJECT.md Section 13's V4 entries).
                   When any local fallback happens inside _route() —
                   forced-Groq failure, forced-Cerebras failure, or the
                   plain auto-mode local fallback — this value is used
                   INSTEAD OF calling select_local_specialist(query)
                   again there. The point isn't computing the persona
                   cheaper (select_local_specialist() was always cheap);
                   it's that Agent.run() now has to predict the local
                   persona BEFORE the first system prompt is built (so
                   _PERSONA_VOICE/_PERSONA_MAX_STEPS can apply to
                   unaddressed auto-mode too), and if _route() were left
                   to recompute its own answer independently later, nothing
                   guarantees the two calls agree — a real risk only
                   because select_local_specialist() reads the query text
                   directly and has no persistent state, so two calls
                   against the identical query are deterministic today,
                   but coupling that guarantee across two separate call
                   sites was never something worth relying on implicitly.
                   None (the default) preserves the exact prior behavior —
                   every caller that doesn't pass this (this module's own
                   __main__ smoke test, any future direct caller) still
                   gets select_local_specialist(query) computed fresh
                   inside _route(), unchanged.

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

        response = self._route(
            messages, tools, query, retrieved_chunks, retrieved_files,
            force_provider, think_override, local_persona_hint,
        )

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
        local_persona_hint: str | None = None,
    ) -> ProviderResponse:
        """Actual provider-selection logic, separated from chat() so
        caching/logging wraps this cleanly without duplicating routing
        rules.

        local_persona_hint (2026-07-22): if the caller already predicted
        which local persona this query would get (see chat()'s
        docstring), every local-fallback branch below uses that value
        instead of calling select_local_specialist(query) fresh —
        keeps whatever voice/step-budget the system prompt already
        committed to in sync with whichever persona actually answers.
        Falls back to computing it here (unchanged prior behavior) when
        no hint was supplied.
        """
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
                logger.info("requesting from cerebras...")
                return self.cerebras.chat(messages, tools=tools, think_override=think_override)
            except ProviderError as e:
                fallback_persona = local_persona_hint or select_local_specialist(query)
                logger.warning(
                    f"Forced Cerebras call failed, falling back to "
                    f"{fallback_persona}: {e}"
                )
                if e.is_rate_limit:
                    self._trip_cerebras_circuit(is_daily_quota=e.is_daily_quota)
                return self._call_local(fallback_persona, messages, tools, think_override)

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
                logger.info("requesting from groq...")
                return self.groq.chat(messages, tools=tools, think_override=think_override)
            except ProviderError as e:
                fallback_persona = local_persona_hint or select_local_specialist(query)
                logger.warning(
                    f"Forced Groq call failed, falling back to "
                    f"{fallback_persona}: {e}"
                )
                if e.is_rate_limit:
                    self._trip_groq_circuit(is_daily_quota=e.is_daily_quota)
                return self._call_local(fallback_persona, messages, tools, think_override)

        if force_provider in self._PERSONA_THINK:
            return self._call_local(force_provider, messages, tools, think_override)

        # --- Auto mode: complexity scorer decides ---------------------
        score = score_complexity(query, retrieved_chunks, retrieved_files)
        should_try_groq = score > config.COMPLEXITY_THRESHOLD

        if should_try_groq and not self._groq_circuit_open():
            try:
                logger.info("requesting from groq...")
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
                    minutes_used = self._trip_groq_circuit(is_daily_quota=e.is_daily_quota)
                    kind = "DAILY quota" if e.is_daily_quota else "rate limit"
                    logger.warning(
                        f"Groq circuit breaker tripped for {minutes_used} min "
                        f"({kind})"
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
                logger.info("requesting from cerebras...")
                response = self.cerebras.chat(messages, tools=tools, think_override=think_override)
                response.complexity_score = score
                logger.info("Escalated to Cerebras after Groq unavailable/failed")
                return response
            except ProviderError as e:
                logger.warning(f"Cerebras failed, falling back to local: {e}")
                if e.is_rate_limit:
                    minutes_used = self._trip_cerebras_circuit(is_daily_quota=e.is_daily_quota)
                    kind = "DAILY quota" if e.is_daily_quota else "rate limit"
                    logger.warning(
                        f"Cerebras circuit breaker tripped for {minutes_used} min "
                        f"({kind})"
                    )

        # Local fallback — either complexity was low, both cloud tiers'
        # circuits are open, neither is configured, or both just failed
        # above. Specialist routing (2026-07-20, select_local_specialist())
        # picks the persona here rather than always defaulting to
        # self.default_local — this is the one call site where the query
        # genuinely wasn't addressed to a specific persona by the user
        # (force_provider was None the whole way through auto mode), so
        # there's no explicit choice to respect either way.
        fallback_persona = local_persona_hint or select_local_specialist(query)
        response = self._call_local(fallback_persona, messages, tools, think_override)
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
