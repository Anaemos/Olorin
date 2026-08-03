"""
core/agent.py — Raw ReAct agent loop (Section 9 of OLORIN_PROJECT.md).

No LangChain. No LangGraph. Every line here is meant to be explainable in
an interview — this is deliberately hand-written so the mechanics of
"model reasons, calls a tool, reasons over the result, repeats" are fully
visible rather than hidden inside a framework.

Flow per step:
  1. Send messages + tool schemas to LLMClient.
  2. If the model returns a final answer (finish_reason == "stop"), done.
  3. If the model wants to call tools (finish_reason == "tool_calls"),
     execute each one via tools/registry.py, append results as "tool"
     role messages, and loop back to step 1 so the model can reason over
     what it just learned.
  4. Hard-capped at MAX_AGENT_STEPS to prevent an infinite loop if a model
     keeps calling tools without ever producing a final answer.
"""

import json
import os
import re

import config
from core.llm_client import LLMClient, select_local_specialist
from core.logging_config import get_logger
from memory import capsules as capsules_store
from memory import entities as entities_store
from memory import history
from tools.registry import TOOL_SCHEMAS, execute_tool, parse_tool_call_arguments

logger = get_logger("agent")

# User-authored config should be short; this is a defensive cap, not
# expected to bind in practice — see load_olorin_md()'s docstring.
_MAX_OLORIN_MD_CHARS = 4000

# Direct-address routing override (V2, 2026-07-11) — maps a name spoken
# at the very start of a query onto the exact same force_provider strings
# the --provider CLI flag already uses (Section 7). "olorin" maps to
# "groq" per Section 2's lore table: Olorin IS the Groq persona.
#
# "cerebras" added 2026-07-14 — a real gap, not a deliberate omission
# that was properly communicated: when Cerebras was wired in (2026-07-13)
# only the --provider cerebras flag was added here; direct-address
# support was skipped on the reasoning that "Cerebras" isn't part of
# Section 2's fantasy-lore roster, but that limitation was never actually
# flagged clearly. First live test of `ask "Cerebras, ..."` silently fell
# through to auto-mode instead of erroring or routing correctly — the
# address was never stripped from the query (visible in the log:
# query='Cerebras, what is this project?', prefix still attached), the
# short remaining text scored low, and it got routed to local Boromir
# instead. A silent wrong-routing is worse than a missing feature would
# have been. Cerebras has no lore name (Section 2's roster is Olorin/
# Faramir/Boromir only), so it maps directly to itself, same as "groq".
_PROVIDER_ALIASES = {
    "olorin": "groq",
    "groq": "groq",
    "cerebras": "cerebras",
    "boromir": "boromir",
    "faramir": "faramir",
}
_ADDRESS_PATTERN = re.compile(
    r"^(olorin|groq|cerebras|boromir|faramir)\b[,:]?\s+", re.IGNORECASE,
)

# Persona voice (added 2026-07-21) — the actual thing missing when the
# first real Boromir/Faramir comparison came back generic. Sampling
# params (core/llm_client.py's _PERSONA_PARAMS) change HOW confidently
# tokens get picked; they were never going to fix a problem they don't
# cause. Nothing anywhere was telling the model "you are Boromir" or
# what that should mean — build_system_prompt() always opened with the
# generic "You are Olorin" framing regardless of which backend actually
# answered. This is the fix: real, disprovable behavioral instructions,
# not a label ("you are Boromir the coder") and not a knowledge/
# capability restriction (that was yesterday's now-removed tool-gating
# problem, not today's). Boromir's text directly targets the actual
# failure mode from the first live test — a generic, neutral, numbered-
# checklist answer ("consider performance, maintainability, feature
# requirements...") is explicitly called out as the anti-pattern to
# avoid, not just described abstractly.
_PERSONA_VOICE = {
    "boromir": (
        "You are Boromir. You value decisive, practical action over "
        "exhaustive analysis. When asked an open question, commit to "
        "your actual best recommendation FIRST, stated plainly, then "
        "briefly note the one or two things that would change your "
        "mind. Do not answer with a neutral checklist of generic "
        "considerations (\"you should think about performance, "
        "maintainability, requirements...\") — that's a template, not "
        "an answer, and it's exactly the failure mode to avoid. If you "
        "genuinely don't know enough to recommend something, say the "
        "one specific thing you'd check first and why, then stop."
    ),
    "faramir": (
        "You are Faramir. You value understanding the real shape of a "
        "problem before committing to an answer — but that means "
        "forming and stating an actual view, not listing options "
        "neutrally or opening with a string of clarifying questions "
        "before saying anything substantive. Ground your reasoning in "
        "what you've actually retrieved (entity memory, past "
        "conversations, real code) rather than generic advice. State "
        "what you think is true or likely right, explain the tradeoffs "
        "behind it, and note what would change your mind."
    ),
}

# Per-persona step budget (added 2026-07-21) — same scope boundary as
# persona voice above: only applies when the persona is known upfront
# (direct-address/--provider boromir|faramir), since auto-mode doesn't
# know which backend will answer until after the first call. Deliberately
# MILD, not the steep 3-4-step cap an earlier design pass considered —
# the num_predict cap that got removed this same session (core/
# llm_client.py's _PERSONA_PARAMS) cut Boromir off mid-sentence, and a
# too-tight step cap risks the same class of failure in a different
# shape: forcing "Max reasoning steps reached" on a genuinely multi-step
# task instead of a slower-but-complete answer. Boromir gets a modest
# tightening (fast execution is still the intent); Faramir keeps the
# existing shared default unchanged — already generous, no reason to
# inflate further just because "deliberation is the point."
_PERSONA_MAX_STEPS = {"boromir": 6, "faramir": config.MAX_AGENT_STEPS}


def parse_direct_address(query: str) -> tuple[str | None, str]:
    """
    Detects a direct address at the very start of a query ("Boromir, ...",
    "Groq: ...", "Olorin ...") and returns (force_provider, cleaned_query).
    Returns (None, query) unchanged if no address is found.

    This is an explicit user override, NOT a difficulty classifier —
    worth being precise about the distinction, since Section 13
    deliberately rejected keyword-based difficulty classification in
    favor of post-retrieval scoring. That decision was about not
    inferring how hard a question is from its wording. This is different:
    it's functionally identical to the --provider CLI flag (Section 7),
    just expressed inline instead of as a separate argument. Nothing
    about routing is being guessed here — the user is stating a choice.

    Deliberately narrow: only matches at the exact start of the query,
    only the four known names, immediately followed by punctuation and/or
    whitespace. A broader match (scanning anywhere in the sentence) risks
    silently overriding routing on a false positive ("I think Boromir
    handled that wrong") — worse than occasionally missing an informal
    address written differently.

    The address is stripped from what actually reaches the model (no
    reason for Boromir to parse its own name out of the real question),
    but callers should keep the ORIGINAL raw query for episodic memory
    logging — the user really did type it, and it should stay searchable
    as what actually happened. See Agent.run() for how the two are used.
    """
    stripped = query.lstrip()
    match = _ADDRESS_PATTERN.match(stripped)
    if not match:
        return None, query

    alias = match.group(1).lower()
    cleaned = stripped[match.end():].strip()
    if not cleaned:
        # The whole query was just a name ("Boromir") — nothing left to
        # ask. Don't strip it down to an empty string; treat as no match.
        return None, query

    return _PROVIDER_ALIASES[alias], cleaned


def load_olorin_md(repo_root: str) -> str | None:
    """
    Reads OLORIN.md from a repo's root, if present (Section 8). This is
    an optional per-repo file, not a required config — honest None when
    absent, no error. It's an opt-in way for a user to hand the agent
    standing project rules ("never suggest SQLAlchemy, we use raw
    asyncpg") that should hold for an entire run.

    Read once per Agent instance (see Agent.__init__), not once per
    step — it's static file content, no reason to re-read it on every
    loop iteration. Unlike Knowledge Capsules (Section 10/13), this
    content is NEVER stripped after step 1: it's explicit user-authored
    instruction, not orientation help the model can safely stop needing
    once it's grounded in real retrieved code — dropping a project rule
    mid-run would be a correctness problem, not just a missed
    optimization. It also does not feed the complexity scorer's
    retrieved_chunks, for the same reason capsules don't (Section 7): it's
    standing baseline context, not query-specific retrieval.

    Capped at _MAX_OLORIN_MD_CHARS since it still competes for the same
    local context budget on every step (unlike capsules, it can't be
    stripped later to free that budget back up) — unusual for a
    hand-written config file to need it, but a truncation is safer than
    an unbounded read here.
    """
    path = os.path.join(repo_root, "OLORIN.md")
    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as e:
        logger.warning(f"Failed to read OLORIN.md: {e}")
        return None

    if len(content) > _MAX_OLORIN_MD_CHARS:
        logger.warning(
            f"OLORIN.md is {len(content)} chars, truncating to "
            f"{_MAX_OLORIN_MD_CHARS} (see load_olorin_md()'s docstring)."
        )
        content = content[:_MAX_OLORIN_MD_CHARS]

    return content.strip() or None


def build_system_prompt(
    repo_root: str,
    capsules: list[dict] | None = None,
    olorin_md: str | None = None,
    extra_repo_roots: list[str] | None = None,
    persona: str | None = None,
) -> str:
    """
    V1 system prompt + V2's Knowledge Capsules (Section 10/13) + V2's
    OLORIN.md per-repo config (Section 8).

    capsules (changed 2026-07-14, was a bare include_capsules: bool) —
    the caller now fetches capsules once (Agent.run()) and passes the
    actual list through, rather than this function re-fetching them
    internally on every call. Two reasons: (1) Agent.run() needs the same
    capsule content to also seed the complexity scorer's retrieved_chunks
    (see run()'s docstring for why), so fetching it twice — once here,
    once there — would be wasteful and could theoretically observe two
    different snapshots if a capsule regenerates mid-call; (2) it makes
    the step-1-only injection explicit at the call site (pass the real
    list for step 1, pass None for step 2+) instead of a boolean flag
    whose meaning you have to trace back into this function to understand.

    The context-budget reasoning for why this is step-1-only at all is
    unchanged (Section 10): capsule text competes with growing tool-
    result content for the same local 16K context budget, worst exactly
    on multi-step queries where the budget is already tightest, and by
    step 2+ the model is reasoning over concrete retrieved content, not
    trying to orient itself. See Agent.run() for where capsules=None
    kicks in on the step-1 rebuild.

    olorin_md, by contrast, is passed through unconditionally on every
    call this run makes — see load_olorin_md()'s docstring for why it
    can't be stripped the way capsules can.

    Faramir's tool-gating (tools_available flag, added 2026-07-13) was
    REMOVED 2026-07-20 — both personas now run on the same qwen3:8b
    weights, which reliably emits structured tool_calls even with
    think=True (verified live: a real get_weather-shaped test returned a
    clean tool_calls array with empty content, not the DeepSeek-R1-era
    prose-narration failure test_faramir_tools.py originally caught —
    see OLORIN_PROJECT.md Section 13's V4 entry). Both personas always
    get the real tool listing below now.

    persona (added 2026-07-21, extended to unaddressed auto-mode
    2026-07-22) — "boromir"/"faramir"/None. Swaps the opening identity
    line for real voice content (_PERSONA_VOICE above) instead of the
    generic "You are Olorin" framing. Two cases now supply a real value:
    direct-address ("Boromir, ...") / --provider boromir/faramir (known
    upfront, force_provider already set before this function runs), AND
    plain unaddressed auto-mode queries, where Agent.run() now calls
    select_local_specialist(query) itself BEFORE building the first
    system prompt — the same function core/llm_client.py's _route() was
    already calling, just moved earlier so its answer can inform the
    prompt instead of only being decided after the prompt was already
    sent. This is a real behavioral change from the original design
    (which left auto-mode as generic "Olorin" framing since the persona
    genuinely wasn't decided yet at prompt-build time) — the prediction
    is speculative (the query might still escalate to cloud, in which
    case a persona-voiced prompt just goes unused by Groq/Cerebras,
    harmless the same way an OLORIN.md rule that doesn't bear on a given
    question is harmless) but costs nothing to compute (pure
    query-text heuristic, no retrieval, no LLM call) and closes the real
    gap flagged in OLORIN_PROJECT.md Section 13's V4 entries: without
    it, unaddressed auto-mode traffic — almost certainly the majority
    of real usage — never benefited from persona voice at all. See
    Agent.run() for where the prediction is computed once and threaded
    both here and into LLMClient.chat()'s local_persona_hint, so the
    persona the prompt commits to and the persona that actually answers
    can't independently diverge.

    force_provider values other than boromir/faramir/None ("groq",
    "cerebras") still get generic framing here, deliberately — an
    explicit cloud address is a real user choice for THIS call to be
    cloud-voiced (or rather, voice-neutral), not a signal to pre-commit
    to a local persona that might not even end up answering.

    extra_repo_roots (V3, "cross-repo querying", Section 11) — when set,
    lists the additional repos search_codebase also searches this run,
    so the model understands its results may span multiple codebases and
    can correctly attribute/relate what it finds (each search_codebase
    chunk carries a "repo" field for exactly this). Every other tool
    stays scoped to repo_root alone — a deliberate scope decision, see
    tools/search.py's module docstring.
    """
    capsule_section = ""
    if capsules:
        lines = "\n".join(f"- {m['module']}: {m['summary']}" for m in capsules)
        capsule_section = (
            "\nRepository overview (pre-computed module summaries, "
            "shown once at the start of this conversation to help you "
            "orient before deciding which tools to call):\n"
            f"{lines}\n"
        )

    cross_repo_section = ""
    if extra_repo_roots:
        repo_list = "\n".join(f"  - {r}" for r in extra_repo_roots)
        cross_repo_section = (
            "\nsearch_codebase also searches across these additional "
            f"repos this session (cross-repo mode, V3):\n{repo_list}\n"
            "Its results include a 'repo' field indicating which repo "
            "each chunk came from — mention it when relevant, especially "
            "if comparing or relating code across repos. Every other "
            "tool (read_file, list_files, git_diff, file_importance, "
            "search_history, search_entities) is still scoped to the "
            "primary repository root above only.\n"
        )

    olorin_md_section = ""
    if olorin_md:
        olorin_md_section = (
            "\nProject-specific instructions (from this repo's OLORIN.md "
            "— follow these exactly, they take priority over your general "
            "defaults):\n"
            f"{olorin_md}\n"
        )

    tools_section = (
        "You have nine tools available:\n"
        "- search_codebase: semantic search over the indexed codebase — "
        "try this first for conceptual/\"how does X work\" questions. If "
        "the repo hasn't been indexed yet, it'll tell you so.\n"
        "- list_files: list files matching a glob pattern\n"
        "- read_file: read the full contents of a specific file\n"
        "- search_history: search past conversations you've had about "
        "this repo, for questions like 'what did I figure out about X "
        "before?'\n"
        "- search_entities: look up decisions and concepts previously "
        "identified as worth remembering for this repo, for questions "
        "like 'what did we decide about X?' or 'what's the status of "
        "Y?'\n"
        "- file_importance: look up a specific file's import graph — "
        "how many other files depend on it and what it depends on — "
        "for questions like 'why does this file matter?' or 'is it safe "
        "to change X?'\n"
        "- git_diff: explain what changed via git — call with NO ref for "
        "'what did I just change?' / 'what changed today?' style "
        "questions; that shows every uncommitted change (working tree + "
        "staged) vs HEAD, which is what most repos' real recent work "
        "actually is — don't guess a specific ref/range like HEAD~1 for "
        "these unless the question explicitly names a commit, a range, "
        "or asks about commit history specifically.\n"
        "- web_search: search the live web for current information "
        "outside this codebase and outside your training data (current "
        "events, library versions, anything time-sensitive) — use it "
        "instead of guessing when a question needs up-to-date facts\n"
        "- crawl_page: fetch ONE specific web page (given a real URL) "
        "and return its actual content, when a web_search snippet isn't "
        "enough — e.g. following a link from a search result or reading "
        "a page the user linked directly. Requires a URL, not a query "
        "— use web_search first if you don't have one.\n\n"
        "When asked about the codebase, ground your answer in the actual "
        "code rather than guessing — use search_codebase to find relevant "
        "chunks, falling back to list_files/read_file if search isn't "
        "available or doesn't find what you need. Give a direct, grounded "
        "answer once you have enough information — don't call tools more "
        "than necessary."
    )

    identity_line = _PERSONA_VOICE.get(
        persona,
        "You are Olorin, a local-first engineering assistant with access "
        "to tools for exploring a codebase.",
    )

    return (
        f"{identity_line}\n\n"
        f"The repository root is: {repo_root}\n"
        f"{cross_repo_section}"
        f"{olorin_md_section}"
        f"{capsule_section}\n"
        f"{tools_section}"
    )


class Agent:
    """
    Owns one LLMClient and runs the ReAct loop against a primary repo_root.

    extra_repo_roots (V3, "cross-repo querying", Section 11): optional
    additional repo paths whose code also becomes searchable via
    search_codebase this run. Every other tool (read_file, list_files,
    git_diff, file_importance, search_history, search_entities) stays
    scoped to repo_root alone — a deliberate scope decision made
    directly before writing any code, not an oversight. See tools/
    search.py's module docstring and indexer/store.py's query_multi()
    for the actual cross-collection merge logic.
    """

    def __init__(
        self,
        repo_root: str,
        llm_client: LLMClient | None = None,
        extra_repo_roots: list[str] | None = None,
    ):
        self.repo_root = repo_root
        self.llm = llm_client or LLMClient()
        self.olorin_md = load_olorin_md(repo_root)
        self.extra_repo_roots = extra_repo_roots or []

        # Exposed metadata from the most recent run() call -- added so
        # callers (server/app.py, specifically the richer /ask/stream
        # "done" event) can report real backend/complexity/token info
        # without run()'s return type changing. All the underlying data
        # was already computed internally for _log_conversation()/
        # _extract_entities(); this just also keeps it accessible after
        # the call returns, via a small _record_run_metadata() step at
        # each of run()'s three return points.
        self.last_backend_used: str | None = None
        self.last_provider: str | None = None
        self.last_model: str | None = None
        self.last_complexity_score: int | None = None
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    def _accumulate_retrieved_chunks(
        self,
        tool_result: dict,
        retrieved_chunks: list[str],
        retrieved_files: set[str],
    ) -> None:
        """
        Feeds tool results back into the post-retrieval complexity scorer
        (core/llm_client.py's score_complexity(), OLORIN_PROJECT.md
        Section 7) so later steps in the same run see real retrieval
        volume instead of an empty list.

        Real bug found + fixed (2026-07-11): run() was calling
        llm.chat() without ever passing retrieved_chunks, on any step —
        meaning score_complexity() always scored against an empty list,
        even on step 2+ after search_codebase had already returned real
        chunks into the message history. Section 7's headline design
        goal ("classify complexity AFTER seeing retrieved chunks, not
        before") had never actually been exercised in the live ask
        path — only in llm_client.py's standalone smoke test with
        hardcoded chunks. Surfaced by episodic memory logging real
        complexity_score=0 values that didn't reflect what had actually
        been retrieved.

        search_codebase's chunks are the canonical case Section 7 was
        written for. read_file's content is included too — a large file
        pulled into context is exactly the kind of thing that should
        make a query score as harder, not just chunk *count*.
        list_files and search_history results are deliberately excluded:
        filenames and past conversations aren't the code context this
        scorer is meant to weigh, and including them would conflate two
        orthogonal signals. web_search results are excluded the same way
        (2026-07-13) — its result shape has neither a "chunks" nor
        "content" key, so this is structural rather than an explicit
        check, but the reasoning is identical: a web snippet isn't code
        context either, and folding it in would make the scorer harder
        to reason about for no benefit.

        retrieved_files (added 2026-07-15, closing the multi-file-
        detection half of the V2 "better complexity scoring" item
        deliberately deferred on 2026-07-14): populated alongside
        retrieved_chunks from the same two tool results, using whatever
        file-identifying field each one already carries —
        search_codebase's chunks each have a "file" key (indexer/
        store.py's chunk metadata, Section 8), read_file's result has a
        top-level "path" key (tools/reader.py). No new tool output
        shape was needed; both fields already existed for other reasons
        (display/logging), this just also feeds them into the scorer.
        Same exclusions as retrieved_chunks for the same reasons —
        list_files/search_history/web_search results never reach this
        method at all, so there's nothing to exclude here that isn't
        already excluded by the two branches below.
        """
        if "chunks" in tool_result:
            for c in tool_result["chunks"]:
                retrieved_chunks.append(c.get("content", ""))
                if c.get("file"):
                    retrieved_files.add(c["file"])
        elif "content" in tool_result:
            retrieved_chunks.append(tool_result["content"])
            if tool_result.get("path"):
                retrieved_files.add(tool_result["path"])

    def _backend_used(self, last_response) -> str:
        """
        Derives the backend label from a ProviderResponse — extracted out
        of _log_conversation() (2026-07-15) since _extract_entities() now
        needs the identical value for its own force_provider selection.
        See _log_conversation()'s docstring for why this isn't simply
        last_response.provider: providers/ollama_provider.py reports
        provider="ollama" for both local personas, so model is what
        actually distinguishes boromir from faramir.
        """
        if last_response is None:
            return "unknown"
        return (
            last_response.model if last_response.provider == "ollama"
            else last_response.provider
        )

    def _record_run_metadata(self, last_response) -> None:
        """
        Populates the self.last_* attributes declared in __init__ (see
        their comment there for why they exist) from the final
        ProviderResponse of a run. Called once at each of run()'s three
        return points, right alongside the existing _log_conversation()/
        _extract_entities() calls those points already make — same
        "real data from the response that just happened" source, just
        also kept accessible to the caller after run() returns.
        """
        self.last_backend_used = self._backend_used(last_response)
        if last_response is not None:
            self.last_provider = last_response.provider
            self.last_model = last_response.model
            self.last_complexity_score = last_response.complexity_score
            self.last_input_tokens = last_response.input_tokens
            self.last_output_tokens = last_response.output_tokens

    def _log_conversation(
        self,
        user_query: str,
        answer: str,
        tools_used: list[str],
        last_response,
    ) -> None:
        """
        Records this run to episodic memory (Section 10). Best-effort: a
        logging failure should never break the actual answer the user is
        waiting on, so failures are caught and warned about, not raised.
        """
        backend_used = self._backend_used(last_response)
        complexity_score = last_response.complexity_score if last_response is not None else None

        try:
            history.log_conversation(
                repo_path=self.repo_root,
                user_query=user_query,
                agent_response=answer,
                tools_used=tools_used,
                backend_used=backend_used,
                complexity_score=complexity_score,
            )
        except Exception as e:
            logger.warning(f"Failed to log conversation to episodic memory: {e}")

    def _extract_entities(self, user_query: str, answer: str, last_response) -> None:
        """
        Entity memory extraction (designed 2026-07-15 — see
        ENGINEERING_JOURNAL.md's 2026-07-15 (continued) entry for the
        full design conversation this implements; memory/entities.py's
        module docstring for the storage/dedup/commit-gate design).

        Deliberately uses the SAME backend that just answered the query
        (via force_provider=backend_used) rather than a dedicated
        extraction model — reuses the routing decision the complexity
        scorer already made (Section 7) instead of inventing a second
        gate, and avoids any Ollama model-swap cost since that backend
        is already warm. Faramir is excluded inside memory/entities.py's
        extract_candidates(), not here — test_faramir_tools.py already
        proved she doesn't reliably emit structured output, and asking
        her to emit a clean entities JSON block after answering would
        hit the identical failure mode that got her gated out of tools
        entirely, just relocated into this new call.

        Only called from the clean finish_reason=="stop" path in run()
        — not on max-steps/unexpected-finish_reason exits, since those
        don't represent a real grounded conclusion worth extracting
        facts from. Best-effort, same posture as _log_conversation():
        a failure here must never affect the answer already returned to
        the user.
        """
        backend_used = self._backend_used(last_response)
        try:
            committed = entities_store.extract_and_commit(
                llm_client=self.llm,
                backend_used=backend_used,
                repo_path=self.repo_root,
                query=user_query,
                answer=answer,
            )
            if committed:
                logger.info(
                    f"entity memory: committed {len(committed)} "
                    f"entit{'y' if len(committed) == 1 else 'ies'}"
                )
        except Exception as e:
            logger.warning(f"Entity extraction failed: {e}")

    def run(self, user_query: str, force_provider: str | None = None) -> str:
        # Direct-address override (see parse_direct_address()'s docstring)
        # — only applies if the caller didn't already force a provider
        # explicitly (an explicit --provider/--force-local flag always
        # wins over an in-text address). original_query is preserved for
        # episodic memory logging even after the address is stripped from
        # what actually reaches the model.
        original_query = user_query
        if force_provider is None:
            detected_provider, cleaned_query = parse_direct_address(user_query)
            if detected_provider:
                logger.info(
                    f"Direct address detected: routing to {detected_provider} "
                    f"(query: {cleaned_query!r})"
                )
                force_provider = detected_provider
                user_query = cleaned_query

        # Faramir's tool-calling gate REMOVED (2026-07-20) — both
        # personas now run on the same qwen3:8b weights, which reliably
        # emits structured tool_calls even with think=True (verified
        # live, see OLORIN_PROJECT.md Section 13's V4 entry). Both
        # personas always get the real tool schema now.
        tools_for_call = TOOL_SCHEMAS

        # Predicted local persona (2026-07-22) — computed once, here,
        # BEFORE the first system prompt is built, and threaded to two
        # places: build_system_prompt() (so unaddressed auto-mode gets
        # real persona voice too, not just direct-address/--provider
        # cases) and llm.chat()'s local_persona_hint (so if the run
        # falls back to local from a forced-cloud failure, or resolves
        # locally via auto-mode routing, the SAME persona answers that
        # the prompt already committed to — core/llm_client.py's
        # _route() would otherwise recompute select_local_specialist()
        # independently at fallback time, and while today that's
        # deterministic against the same query text, there's no reason
        # to rely on two separate call sites implicitly agreeing when
        # computing it once and passing it through costs nothing.
        # Always computed (cheap: query-text heuristic, no retrieval, no
        # LLM call) regardless of force_provider — even a forced-groq/
        # cerebras call benefits from a consistent fallback persona if
        # the cloud call fails.
        predicted_local_persona = select_local_specialist(user_query)

        # Persona voice (2026-07-21, extended to unaddressed auto-mode
        # 2026-07-22) — known_persona covers direct-address/--provider
        # boromir|faramir (explicit user choice). Auto-mode (force_
        # provider is None — not addressed, not forced to cloud, not
        # forced local to a specific persona) now also gets real voice,
        # using the same prediction above. Forced-cloud addresses
        # ("Groq, ...", "Cerebras, ...") deliberately still get generic
        # framing — see build_system_prompt()'s docstring for why.
        known_persona = force_provider if force_provider in ("boromir", "faramir") else None
        persona_for_prompt = known_persona or (predicted_local_persona if force_provider is None else None)

        # Per-persona step budget (2026-07-21, extended alongside voice
        # 2026-07-22) — see _PERSONA_MAX_STEPS' comment for the reasoning
        # and the deliberately mild numbers. Uses the same persona_for_
        # prompt value as voice above, so a run that gets Boromir's voice
        # also gets Boromir's step budget, never one without the other.
        effective_max_steps = _PERSONA_MAX_STEPS.get(persona_for_prompt, config.MAX_AGENT_STEPS)

        # Fetched once, used two ways (2026-07-14, complexity-scoring fix
        # — see below and OLORIN_PROJECT.md Section 7/13). Previously
        # build_system_prompt() re-fetched capsules internally on every
        # call; now Agent.run() owns the single fetch so the same content
        # can seed retrieved_chunks too, not just the system prompt.
        try:
            capsules = capsules_store.get_all_capsules(self.repo_root)
        except Exception as e:
            logger.warning(f"Failed to load capsules: {e}")
            capsules = []

        messages = [
            {
                "role": "system",
                "content": build_system_prompt(
                    self.repo_root, capsules=capsules, olorin_md=self.olorin_md,
                    extra_repo_roots=self.extra_repo_roots,
                    persona=persona_for_prompt,
                ),
            },
            {"role": "user", "content": user_query},
        ]
        tools_used: list[str] = []

        # Complexity-scoring fix (2026-07-14, OLORIN_PROJECT.md Section
        # 7/13): Section 7's scorer only ever sees retrieved_chunks, which
        # started empty on step 1 of EVERY query by design ("classify
        # complexity after seeing retrieved context, not query wording").
        # The unintended side effect: step 1 always routed local, no
        # matter how rich or complex the repo actually was, because
        # there was structurally nothing for the scorer to see yet —
        # confirmed live 2026-07-14 when a capsule-answerable orientation
        # question never got a chance at Groq/Cerebras even once.
        # Knowledge Capsule text IS retrieved context — it's retrieved
        # from the capsule store instead of a live search_codebase call,
        # but it's real content about the repo, not query wording. Seeding
        # it here lets step 1's routing decision react to real repo
        # richness (a 7-module capsule set vs. a 1-module one) instead of
        # defaulting to zero every single time. This does NOT reopen
        # Section 13's rejection of keyword-based difficulty
        # classification — it's still entirely content-driven, just
        # content that happens to already be sitting in context rather
        # than freshly retrieved via a tool call.
        #
        # Deliberately NOT cleared after step 1 the way the SYSTEM PROMPT
        # capsule section is (build_system_prompt()'s capsules=None on
        # the step-1 rebuild below) — that stripping is about context-
        # budget (capsule text competing with tool-result text for the
        # same 16K window), which doesn't apply here: retrieved_chunks is
        # only ever used for score_complexity() math, never reinserted
        # into the actual messages sent to a model. A genuinely complex
        # repo should keep scoring as complex for the rest of the run,
        # with real tool-retrieved content accumulating on top of it via
        # _accumulate_retrieved_chunks() as before.
        retrieved_chunks: list[str] = [
            f"{m['module']}: {m['summary']}" for m in capsules
        ] if capsules else []

        # Multi-file detection (added 2026-07-15, OLORIN_PROJECT.md
        # Section 7/13) — a set, not a list, since only distinct-count
        # matters to score_complexity(), not order or repeats. Seeded
        # from the same capsule fetch as retrieved_chunks above, using
        # module names as the "file" identity for step 1 (a capsule IS a
        # whole-module summary, so a repo with several capsuled modules
        # genuinely does span several distinct areas before a single
        # tool has even run — the same content that already justifies a
        # non-zero step-1 chunk score should count toward multi-file
        # spread too, not just chunk volume). Never cleared after step 1
        # for the identical reason retrieved_chunks isn't (see the
        # comment above): this only feeds score_complexity() math, never
        # re-enters the actual messages sent to a model, so there's no
        # context-budget reason to strip it.
        retrieved_files: set[str] = {m["module"] for m in capsules} if capsules else set()
        last_response = None

        for step in range(effective_max_steps):
            logger.info(f"step={step + 1} | sending {len(messages)} messages")

            response = self.llm.chat(
                messages=messages,
                tools=tools_for_call,
                query=user_query,
                retrieved_chunks=retrieved_chunks,
                retrieved_files=retrieved_files,
                force_provider=force_provider,
                local_persona_hint=predicted_local_persona,
            )

            last_response = response

            if response.finish_reason == "stop":
                logger.info(f"step={step + 1} | final answer from {response.provider}")
                self._record_run_metadata(last_response)
                self._log_conversation(original_query, response.content, tools_used, last_response)
                self._extract_entities(original_query, response.content, last_response)
                return response.content

            if response.finish_reason == "tool_calls":
                # Record the assistant's tool-call request in the transcript
                # before executing anything — the message history must
                # match what actually happened, in order, for the next
                # model call to make sense of the tool results that follow.
                messages.append({
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in response.tool_calls
                    ],
                })

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call["name"])
                    arguments = parse_tool_call_arguments(tool_call["arguments"])
                    logger.info(
                        f"step={step + 1} | executing {tool_call['name']}"
                        f"({arguments})"
                    )

                    result = execute_tool(
                        tool_name=tool_call["name"],
                        arguments=arguments,
                        repo_root=self.repo_root,
                        extra_repo_roots=self.extra_repo_roots,
                    )
                    self._accumulate_retrieved_chunks(result, retrieved_chunks, retrieved_files)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(result),
                    })

                if step == 0:
                    # Capsules (if any) were shown once, in the very first
                    # system message the model saw. From here on, every
                    # subsequent llm.chat() call resends the full messages
                    # list, so leaving capsule text in place would keep
                    # taxing the local context budget on every remaining
                    # step for no further benefit — see build_system_
                    # prompt()'s docstring for the full reasoning.
                    messages[0] = {
                        "role": "system",
                        "content": build_system_prompt(
                            self.repo_root, capsules=None, olorin_md=self.olorin_md,
                            extra_repo_roots=self.extra_repo_roots,
                            persona=persona_for_prompt,
                        ),
                    }

                # Loop continues — model reasons over the tool results
                # on the next iteration.
                continue

            # Defensive: shouldn't happen given only "stop"/"tool_calls"
            # are produced by our providers, but fail loudly rather than
            # silently looping forever on an unrecognized finish_reason.
            logger.warning(f"Unexpected finish_reason: {response.finish_reason}")
            answer = response.content or "No answer produced."
            self._record_run_metadata(last_response)
            self._log_conversation(original_query, answer, tools_used, last_response)
            return answer

        logger.warning(f"Max steps ({effective_max_steps}) reached without final answer.")
        answer = "Max reasoning steps reached without a final answer."
        self._record_run_metadata(last_response)
        self._log_conversation(original_query, answer, tools_used, last_response)
        return answer


if __name__ == "__main__":
    # Manual smoke test: python -m core.agent
    test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    agent = Agent(repo_root=test_root)

    print("--- Query: 'What does config.py do?' ---")
    answer = agent.run("What does config.py do? Read the file to find out.")
    print(f"\nFinal answer:\n{answer}")
