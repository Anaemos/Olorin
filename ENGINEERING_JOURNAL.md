# Olorin — Engineering Journal

A running build log. Each entry: what got built, why, and the interview-ready
explanation for it. Full architectural rationale lives in `OLORIN_PROJECT.md`;
this file is the day-by-day record of actually building it.

**Continuing from `ENGINEERING_JOURNAL_ARCHIVE_01.md`** (2026-07-06 →
2026-07-12), split off once that file passed ~1000 lines — nothing was
lost or changed, this is purely a readability split. This file picks up
from 2026-07-12 (continued) onward.

---

## 2026-07-12 (continued) — Engineering Journal automation shipped

### Engineering Journal automation — the last open V2 checklist item
- Section 10's spec ("every session writes `journal/YYYY-MM-DD.md`") had
  sat unimplemented since V2 planning began — this project's own
  `ENGINEERING_JOURNAL.md` has been maintained by hand the entire time,
  which was itself the motivating example: automate the thing already
  being done manually every session.
- **No new data needed.** Everything a journal entry requires — query,
  answer, tools used, backend — is already written by `Agent.run()` into
  episodic memory (`memory/history.py`'s `conversations` table, Section
  10) on every completed run. Journal generation is purely a read + a
  reshape, not a new write path.

### `memory/history.py` — `get_conversations_for_date()` added
- One new query function: all conversations for a `repo_path` on a given
  `YYYY-MM-DD`, matched via `substr(timestamp, 1, 10)` against the
  isoformat timestamps `log_conversation()` already writes. Returned
  oldest-first (chronological) rather than `get_recent()`'s newest-first
  — a journal entry reads as a timeline of the session, not a feed.

### `memory/journal.py` built — store/generate split, same shape as Capsules
- **Storage location mirrors `indexer/store.py`'s ChromaDB pattern
  exactly, for the identical reason:** writing generated journal files
  into the *indexed* repo's own working tree would mean journaling
  someone else's project leaves files in their tree they never asked
  for. Journals live at `~/.olorin/journal/<repo_hash>/YYYY-MM-DD.md`,
  keyed by the same sha256-of-resolved-path scheme `store.py` uses for
  Chroma collection names — reimplemented locally (three lines) rather
  than imported, since `memory/` has had no dependency on `indexer/` up
  to this point and there's no reason to introduce one for this.
- **Deterministic sections (no LLM, always run):** "Queries" (`"query" ->
  first ~100 chars of the answer`, matching Section 10's arrow-summary
  spec example), "Tools used" (`search_codebase (4x), read_file (2x)`),
  "Backend" (`Groq (2 queries), Boromir (6 queries)`) — all pure
  aggregation over what `get_conversations_for_date()` returns.
- **"Worked on" is the one section that genuinely needs synthesis, not
  aggregation** — Section 10's own spec example ("- Olorin agent loop")
  is a theme, not a literal question anyone typed, so it can't be
  produced by reformatting stored rows. `synthesize_worked_on()` sends
  the day's queries to **Faramir** (not Boromir) for the identical reason
  Knowledge Capsules use Faramir (`indexer/capsules.py`, Section 10):
  this is prose synthesis, not tool orchestration — Faramir's lore role
  — and the known Faramir `<think>`-block tool-calling risk (Section 9)
  doesn't apply, since this call makes no tool calls either. `llm_client`
  is duck-typed (`.chat(messages, force_provider=...)`), matching
  `generate_module_summary()`'s exact pattern, so `memory/` still doesn't
  need a hard dependency on `core/`.
- **Graceful degradation, not a hard dependency:** `synthesize_worked_on()`
  returns `[]` — never raises — on `llm_client=None` or any call failure;
  `write_journal()` falls back to a literal, deduplicated query list in
  that case. A Groq/Ollama hiccup degrades the journal (less polished
  "Worked on" section) instead of blocking it entirely.

### `cli.py` — new `journal` command
- `python cli.py journal [--path] [--date] [--no-llm]` — fetches the
  day's conversations, generates the Markdown, writes it, prints the
  path. `--no-llm` skips the Faramir call and forces the literal-query
  fallback (useful for a fast/offline regen, or as a way to sanity-check
  the deterministic sections in isolation).
- **Deliberately NOT auto-triggered on every `ask`**, unlike indexing.
  Auto-indexing (`_index_repo()`, Section 8) was justified because the
  hash-skip logic makes a repeat freshness check nearly free when nothing
  changed. Regenerating a journal calls an LLM for "Worked on" *every
  time it runs* — real, non-trivial latency with no equivalent "skip if
  unchanged" shortcut (the whole point is it reflects the latest state of
  the day, including the very last query). Folding that into every `ask`
  would tax the interactive path for a document nobody's reading
  mid-session. Journal generation stays a deliberate, explicit,
  end-of-session action — same category of decision as the daemon
  deferral (Section 13): recognizing what's cheap enough to automate
  versus what isn't, rather than automating everything indiscriminately
  just because the mechanism now exists.
- Regeneration is a full overwrite of that date's file, not an append —
  idempotent and simple; the only real cost is the optional LLM call, and
  a stale partial file merged with a fresh one is a worse failure mode
  than "just regenerate the whole thing."

### Verified live — two real runs, one real bug found and fixed (bug #13)

**Run 1 — `python -m memory.journal` (no-LLM smoke test):**
```
Journal written to: C:\Users\Aryavart\.olorin\journal\repo_0c0896fd0277292c\2026-07-12.md
```
Correct output, exactly as designed: literal deduplicated query list
under "Worked on" (no `llm_client` passed), correct arrow-summary
Queries section, correct tool/backend counts. No external dependency,
ran clean.

**Run 2 — `python cli.py journal` against this real repo (live Faramir
synthesis path):** wrote to a *different* `repo_hash` directory than
Run 1's fake test path — confirms the sha256-of-resolved-path scheme
correctly separates repos, not a coincidence of reusing one folder.
Ollama's own diagnostic (Section 13's think-honored logging) confirmed
`think_requested=True thinking_produced=yes thinking_chars=1200`,
latency 21.8s (Faramir `think:true` — deliberation is the point, same
as Capsule generation, and this runs at explicit end-of-session time,
not the interactive path).

**Real bug found + fixed: "Worked on" synthesis hallucinated
implementation work that never happened (bug #13).** The two real
queries logged that day were both *asking Olorin to explain/summarize
existing material* — "read the actual walker code", "summarize what
OLORIN_PROJECT.md says about the daemon deferral". Faramir's synthesis
turned the phrase "daemon deferral" into four confident, fabricated
bullets: "Implement daemon deferral mechanism for processing tasks",
"Handle background task execution delays properly", "Ensure correct
behavior during deferred state" — describing work that was never done;
the actual daemon deferral decision (Section 11's V1.5 tier) was made
and documented *days* before this journal-automation session even
started. Mechanically the feature worked exactly as designed (no crash,
correct files, correct storage) — this was a content-quality bug, not a
plumbing bug, and easy to miss if "did it run" is the only bar checked.
Same class of failure this project already has a name for (Section 9's
Faramir-hallucinates-on-thin-grounding finding from 2026-07-06) showing
up in a new place: synthesis over sparse, underspecified real data, not
bare parametric recall.
- **Fix, at the prompt layer, not a suppression heuristic:**
  `synthesize_worked_on()`'s prompt (`memory/journal.py`) now explicitly
  instructs Faramir that a question asking to explain/read/summarize
  existing code or docs means the developer was *reviewing* that area,
  not building it — bullets should say "reviewed X"/"investigated Y",
  never "implemented"/"fixed" unless the question is actually about a
  change. Deliberately not fixed by adding a conversation-count
  threshold that suppresses synthesis on thin sessions instead — that
  would hide the symptom on small sessions without addressing why the
  model over-extrapolated in the first place, and would leave larger
  sessions with the same underlying risk unaddressed.
- **Re-verification of the fix: confirmed live.** Re-ran `python cli.py
  journal --date 2026-07-12` against this real repo after the prompt fix.
  Bullets changed exactly as intended — "Investigated the use of Rust in
  the walker code", "Read the actual walker code related to Rust",
  "Summarized the OLORIN_PROJECT.md documentation regarding daemon
  deferral" — correctly describing the two queries as explanation/
  summarization work, zero fabricated implementation claims. Bug #13
  closed.

### Status
- [x] `memory/history.py` — `get_conversations_for_date()` added
- [x] `memory/journal.py` — store/generate split (deterministic sections
  + optional Faramir "Worked on" synthesis, graceful degradation)
- [x] `cli.py journal` command wired in
- [x] Live-verified: no-LLM path (Run 1) and live Faramir synthesis path
  (Run 2) both produced correct files at correctly-separated storage
  paths
- [x] Real bug found and fixed: "Worked on" synthesis hallucinated
  implementation work from underspecified queries (bug #13) — fixed at
  the prompt layer, **re-verified live, confirmed fixed**
- [x] **Section 11's V2 tier's Engineering Journal item is now fully
  closed** — implemented, live-verified, one real bug found and fixed
  and re-verified, same bar as every other V2 feature in this project

---

## 2026-07-13 — Cerebras provider, `web_search` tool, Together AI dropped, Faramir isolation test written

### Together AI's free tier is retired — verified before building anything
- Went to implement the originally-planned Groq → Together AI →
  Cerebras → local cascade (Section 7 of `OLORIN_PROJECT.md`). Before
  writing a provider class, checked Together's *current* terms rather
  than trusting the doc's own "$25 free credits on signup" line, which
  was already stale.
- **Confirmed via Together's own pricing docs (verified June 2026):**
  the signup credit was discontinued industry-side in July 2025; Together
  now requires a **$5 minimum purchase upfront**, no free trial. Several
  SEO/affiliate blog posts still claim "$100 free credits" — recognized
  as low-credibility recycled marketing content, not trusted over the
  vendor's own docs.
- **Decision, confirmed directly with Aryavart: stay free-tier-only.**
  `providers/together_provider.py` is deliberately not built — an active
  decision, not a stub left for later. Cerebras absorbed the "second
  cloud tier" role instead.

### `providers/cerebras_provider.py` built
- Mirrors `groq_provider.py` almost exactly (OpenAI-compatible endpoint,
  same request/response shape) — concrete proof `BaseProvider`'s
  dependency-inversion design does what it always claimed: a new cloud
  provider is a new class, zero changes to `core/agent.py` or
  `tools/registry.py`.
- **Real caveats found by actually researching Cerebras's current docs,
  not assumed from parity with Groq:** free-tier context is capped at
  **8,192 tokens** (tighter than Groq's 12,000 TPM budget that already
  caused bug #10's 413), and the free-tier model catalog **churns** —
  multiple independent sources within weeks of each other reported
  different available models. Handled by making `CEREBRAS_MODEL`
  env-overridable (same pattern as `GROQ_MODEL`/`BOROMIR_MODEL`/
  `FARAMIR_MODEL`) and deliberately NOT adding pre-emptive request-size
  guarding for the 8K cap — a too-large request just raises
  `ProviderError` and falls through the existing cascade to local, the
  same mechanism bug #10 already generalized. Speculative size-checking
  before it's ever been measured to matter would be exactly the kind of
  premature complexity this project has consistently avoided elsewhere.
- Unlike Groq, constructed to never fail without a key —
  `is_available()` is the real gate, checked explicitly in auto-mode
  routing to avoid a silent guaranteed-401 network call for anyone who
  hasn't opted in.

### `core/llm_client.py` — Cerebras wired into the cascade
- New cascade: Groq → Cerebras → local, both cloud tiers gated by the
  same `should_try_groq` complexity check (Cerebras is a second attempt
  at the same cloud-escalation decision, not a separate routing tier).
- Independent circuit breaker (`_cerebras_disabled_until` /
  `_cerebras_circuit_open()` / `_trip_cerebras_circuit()`) — a direct
  parallel to Groq's existing fields rather than a generic "circuit
  breaker per provider" abstraction, since with only two cloud providers
  a second named field-set matches the codebase's own existing style; a
  generic abstraction is the right move once a third cloud provider
  actually shows up.
- `--provider cerebras` added as a forced option, mirroring bug #10's
  fix for forced-Groq: wrapped in `try/except ProviderError`, falls back
  to local on any failure. Doesn't pre-check `is_available()` (unlike
  auto-mode) since a forced call is an explicit user request — a missing
  key just surfaces as a normal 401-turned-`ProviderError`.

### `tools/websearch.py` — `web_search` tool built (later corrected same day, see below)
- Closes a V2 item that had sat as a design decision without code since
  2026-07-06. First version: two free-tier backends cascaded with the
  same dependency-inversion + graceful-fallback pattern already proven
  for LLM providers — Tavily primary, Serper fallback, claimed "3,500
  free searches/month combined." **This claim was wrong — see the
  correction further down.**
- Tavily's LLM-ready output was chosen deliberately over building a
  separate fetch-and-parse layer on raw search results: Boromir/Faramir
  only have 16K context and already-fragile tool-call parsing (this
  project's entire bug history), so a small, pre-cleaned tool result
  removes one more way a local model's response can go wrong — same
  reasoning that motivated `list_files`'s cap (bug #9) and the chunk
  size ceiling (bugs #11/#12).
- Neither key configured returns an honest `{"error": ...}` steering the
  model back to trained knowledge, consistent with `search_codebase`'s/
  `search_history`'s existing honesty pattern for an unconfigured/empty
  state.
- Wired into `tools/registry.py` (schema + dispatch) and
  `core/agent.py`'s system prompt (now describes five tools). Excluded
  from `_accumulate_retrieved_chunks()`'s complexity-scorer feed the
  same way `list_files`/`search_history` are — structural (no "chunks"/
  "content" key in its result shape), documented explicitly rather than
  left as an accident.

### Design conversation: SearXNG vs. a search-provider orchestration
- Aryavart raised SearXNG (self-hostable, free, no-API-key metasearch)
  as an alternative to combining two rate-limited free APIs.
- **Decided against it, for now:** running SearXNG means standing up and
  keeping alive a persistent local service (Docker, uptime, occasional
  upstream-engine blocking) — real ongoing operational complexity, the
  same category the V1.5 daemon was deliberately deferred for, and for
  the identical reason: no measured need for it yet. Revisit only
  if usage ever actually threatens the free ceiling.

### `test_faramir_tools.py` written — closing a gap open since 2026-07-06
- Section 9 has flagged since the very first session that Faramir's
  `<think>` blocks could break tool-call parsing and that her raw
  tool-calling reliability should be tested in isolation before trusting
  it — but across seven sessions of building on top of that assumption,
  no such test was ever actually written or run.
- `test_faramir_tools.py` (repo root): a single trivial forced-tool-call
  prompt against a throwaway dummy tool (`get_weather`), entirely
  isolated from the real agent loop and real tool registry — a failure
  here is unambiguous about Faramir's tool-calling mechanics, not about
  `search_codebase`/`web_search`/message-handling semantics specifically.

### Live verification: `python config.py` — clean
- All keys loaded correctly: Groq, Cerebras (`gpt-oss-120b`), Tavily,
  Serper all present. Confirmed the new config plumbing works before
  touching anything that actually calls out to these services.

### Live verification: `python test_faramir_tools.py` — FAILED, as the standing risk predicted
- Real output:
  ```
  finish_reason: stop
  tool_calls: []
  content: '\nI can check that for you! Let me pull up the latest weather
  from Tokyo using the `get_weather` tool...'
  ```
- Faramir did not emit a structured tool call at all — she narrated
  about calling the tool instead. `finish_reason` came back `"stop"`,
  not `"tool_calls"`, and there was **no malformed JSON anywhere in the
  content** for `_try_parse_fallback_tool_call()` to catch. This is a
  categorically different failure mode than every prior Ollama
  tool-calling bug in this project (bugs #1 and #5, both JSON-shaped
  malformations the fallback parser was specifically built to catch) —
  here the model never attempted structured output in the first place,
  so there's nothing for a parser to fix.
- **Decision, confirmed directly with Aryavart: gate tools away from
  Faramir entirely, don't chase a prompt or `think:false` fix.** This
  matches how she's already used everywhere else in the project —
  Knowledge Capsule generation and Engineering Journal synthesis are
  both single plain completions with no tools, never a ReAct loop. The
  gap was only ever in the direct-address/`--provider faramir` path,
  which had never actually been exercised with a tool-requiring query
  until this test.

### `core/agent.py` — Faramir tool-gating implemented
- `build_system_prompt()` gained a `tools_available: bool = True` param.
  When `False`, the five-tool listing is replaced with an explicit "you
  do NOT have access to any tools for this query" note, so she doesn't
  narrate as if she's about to use one that was never offered.
- `Agent.run()`: `is_faramir = force_provider == "faramir"`;
  `tools_for_call = None if is_faramir else TOOL_SCHEMAS`, passed to
  `llm.chat()` instead of the unconditional `TOOL_SCHEMAS` it always
  used before. With `tools=None`, the loop's existing structure needed
  no other change — a model offered no tools always returns
  `finish_reason=="stop"` on step 1, so the loop just naturally exits
  after one step for her, same as any other tool-free single-shot call.

### Corrected: Serper's 2,500 credits are one-time, not monthly — caught by direct question, not assumed
- Aryavart pushed back on the "3,500 free searches/month" claim, having
  found conflicting info. Verified via fresh web search rather than
  trusting either the original claim or Aryavart's correction blindly:
  **confirmed one-time** across multiple independent sources — Serper's
  2,500 credits are a signup grant that doesn't renew monthly. The
  original module docstring and both project docs had this wrong.

### Cascade redesigned: Tavily → Jina → Serper → Exa
- Researched two replacement/addition candidates given Serper's real
  shape, rather than guessing: **Jina** (`s.jina.ai`) — no card at all,
  ~1M free tokens per new API key (one-time per key, but large), LLM-
  ready extracted text like Tavily rather than raw SERP data like Serper
  did. Free tier is licensed non-commercial (CC-BY-NC) — a real term,
  not just a quota, noted explicitly rather than glossed over. **Exa** —
  strongest semantic/neural search of the four, but its real free story
  is a one-time $10 credit (~1,400 searches) with no card; the
  frequently-quoted "1,000/month recurring" figure only applies once a
  payment method is added, per Exa's own billing docs.
- **Exa's card requirement raised the same question Together AI's did.**
  Asked directly rather than assumed: keep Exa card-free as an opt-in
  last-resort hop (using only the one-time credit), skip it entirely, or
  add a card for the modest recurring amount. **Decision: opt-in,
  card-free, one-time credit only** — consistent with the free-tier-only
  principle already established when Together AI was dropped.
- New cascade order, implemented in `tools/websearch.py`: Tavily
  (primary, recurring) → Jina (secondary, no card, large one-time grant,
  replaces Serper's old slot) → Serper (reserve, one-time, raw SERP
  metadata, kept for when both above are down) → Exa (opt-in last
  resort, one-time, best quality). `config.py` gained `JINA_API_KEY` and
  `EXA_API_KEY` alongside the existing `TAVILY_API_KEY`/`SERPER_API_KEY`.
- `_ExaBackend.search()` requests `contents: {"highlights": true}`
  rather than full `text`, per Exa's own docs recommendation for agent
  workflows (far fewer tokens for the most relevant excerpts) — both to
  fit Boromir's context budget and to conserve the one-time credit,
  since highlights-only calls are cheaper than full-content extraction.

### Status
- [x] `providers/cerebras_provider.py` — written, config-verified live,
  not yet exercised against a real chat call
- [x] `core/llm_client.py` — Cerebras wired into cascade + forced-
  provider branch + independent circuit breaker, not yet exercised live
- [x] `tools/websearch.py` — rebuilt as a four-backend cascade (Tavily →
  Jina → Serper → Exa) after the Serper correction; not yet run against
  real keys
- [x] `test_faramir_tools.py` — written AND run. **FAILED**, confirming
  the Section 9 risk empirically for the first time in seven sessions.
- [x] Faramir tool-gating implemented in `core/agent.py` in direct
  response to the failure — `tools=None` whenever
  `force_provider == "faramir"`, system prompt adjusted to match
- [ ] **Next session, in order:** (1) `python -m providers.cerebras_provider`
  and `python -m tools.websearch` for isolated smoke tests of each new
  backend; (2) `python cli.py ask "Cerebras, what is this project?"` to
  confirm the forced-provider branch works end-to-end against a real
  key; (3) a query designed to actually trigger `web_search` (something
  outside the repo and outside training data) to confirm the tool fires
  and a real cascade backend answers it.
- [ ] Section 11's V2 tier fully closes once all of the above is
  actually run and logged — remaining after that: better complexity
  scoring, entity memory's design pass

---

## 2026-07-15 — `web_search` cascade fully live-verified (Jina, Serper, Exa), multi-file complexity detection built and verified

### Part 1: closing the `web_search` gap
- Tavily always wins the cascade first in normal use, so Jina/Serper/
  Exa had only ever been config-checked, never actually called.
  `test_websearch_backends.py` (repo root) calls all four independently.
- **Result: all four passed, first run, no bugs.** Tavily (3 results,
  LLM-ready text), Jina (3 results, `s.jina.ai`'s `data` array parsed
  correctly), Serper (3 results, raw SERP snippets as designed), Exa (3
  results, `highlights` field populated and correctly joined). `tools/
  websearch.py` is now fully live-verified end-to-end.

### Part 2: multi-file detection for the complexity scorer
- Closed the half of the V2 "better complexity scoring" item
  deliberately deferred on 2026-07-14. That entry named the exact gap:
  `retrieved_chunks` was a flat `list[str]` with no file attribution, so
  there was nothing for a multi-file signal to be computed from.
- **`core/agent.py`:** `Agent._accumulate_retrieved_chunks()` now also
  populates a parallel `retrieved_files: set[str]`, reading fields that
  already existed for other reasons — `search_codebase`'s chunk
  metadata already carries `"file"` (`indexer/store.py`), `read_file`'s
  result already carries `"path"` (`tools/reader.py`). No new tool
  output shape needed. `Agent.run()` seeds it from capsule module names
  on step 1, mirroring how `retrieved_chunks` is already seeded, and
  never clears it — same reasoning as the chunk list (it only feeds
  scorer math, never re-enters the actual model messages).
- **`core/llm_client.py`:** `score_complexity()` gained a
  `retrieved_files` param and a `(distinct_files - 1) * MULTI_FILE_WEIGHT`
  term (`MULTI_FILE_WEIGHT = 3`, module constant). Deliberately
  `distinct_files - 1`: touching exactly one file contributes zero, since
  that's not a "spans multiple files" situation at all. `LLMClient.chat()`/
  `_route()` thread the new param through; `cli.py` needed no changes
  since it only ever calls `Agent.run()`, never `llm.chat()` directly.
- **Verified live, two ways:**
  1. Unit-level (`python -m core.llm_client`'s new third smoke-test
     block): identical query/chunks scored `6` at one file vs. `12` at
     three files — confirms `(3-1)*3=6` contribution in isolation from
     any live-call noise.
  2. Real `ask` (`"how do the agent loop and the LLM client work
     together to route a request?"`) triggered `search_codebase` across
     `core/agent.py` and `core/llm_client.py`, routed to Groq.
     `python cli.py history` confirmed the logged row: `complexity=89`
     — real, stacked signal (capsule-seeded volume + multi-file term +
     a "why"-adjacent planning word), not a placeholder.
- **Both halves of Section 11's "better complexity scoring" item are now
  closed.** `COMPLEXITY_THRESHOLD=15` re-tuning remains the one open
  piece, correctly gated on accumulating real post-fix traffic rather
  than being guessed at again.

### searxNcrawl question, addressed directly
- Asked whether searxNcrawl should now move up given today's other
  work. **No** — it's already correctly logged as a well-specified V3
  candidate (2026-07-14 entry), deferred on a measured-need bar
  ("revisit once the free cascade's ceiling is actually threatened")
  that today's all-four-backends-pass result if anything reinforces
  rather than crosses. No doc change needed; already in the right place.

### Status
- [x] `test_websearch_backends.py` written and run — Tavily, Jina,
  Serper, Exa all independently confirmed working live
- [x] Multi-file detection built in `core/agent.py` + `core/llm_client.py`
- [x] Verified live at both the unit level (isolated scoring math) and
  through a real `ask` with a logged `complexity_score`
- [x] `OLORIN_PROJECT.md` Sections 11 and 13 updated to reflect both
  pieces of work
- [x] Confirmed searxNcrawl's V3 placement is still correct, no roadmap
  change needed

**Section 11's V2 tier is now fully closed** except for entity memory
(explicitly deferred pending a design pass, not code) and
`COMPLEXITY_THRESHOLD` re-tuning (gated on traffic, not actionable yet).

---

## 2026-07-16 — Entity memory: designed, built, and live-verified

### The open question: which entity memory?
The original stub (`type: "repo" | "file" | "person" | "concept"`) had
sat unresolved since 2026-07-11. Raised directly before writing any
code: is this "object relations in a repo" (code structure — imports,
call graphs) or "the agent's relation to things" (persistent,
cross-session knowledge of decisions/concepts)? These are genuinely
different features. Resolved: code structure already has its own home
— V3's "why is this file important?" import-graph item — so building
it here would just be that item wearing a different name. Went with the
latter, settled by a purpose test: what question does this answer that
nothing else in the system does?
- Cache → "have I solved this exact query before?"
- Capsules → "what does this module do?"
- `search_history` → "which past conversation mentioned X?"
- **Entity memory** → "what is X, what's its current status?" — a
  resolved fact, not a conversation to re-read

### Three design decisions, resolved directly, in order

**1. Population mechanism.** Chosen: extraction runs on every completed
run, but only commits candidates "deemed valuable." This has a real cost
implication — Faramir's observed latency (~20-26s) would be a 10x tax on
a trivial Boromir-answered query if she did the extracting. Considered
gating behind `complexity_score` (free, already computed) to skip
trivial queries, but a better idea surfaced during discussion: use the
SAME backend that just answered (`force_provider=backend_used`) instead
of a dedicated model. This reuses the routing decision the complexity
scorer already made instead of inventing a second gate, and avoids any
Ollama model-swap cost since that backend is already warm — cost
naturally scales with the routing decision instead of needing a separate
threshold. One real exclusion this reopened: Faramir. `test_faramir_
tools.py` (2026-07-13) already proved she doesn't reliably emit
structured output — asking her to emit an entities JSON block after
answering would hit the identical failure mode that got her gated out of
tools entirely, just relocated. Faramir-answered runs skip live
extraction, a deliberate logged gap, not silently dropped. Real,
accepted cost tradeoff: this adds one extra Groq request per non-Faramir
run, against the free-tier 1,000 RPD/30 RPM budget — raised explicitly,
accepted without a pre-filter ("keep it simple").

**2. Type scope.** Narrowed to `"decision" | "concept"` only —
`"repo"`/`"file"` dropped (overlap with Capsules/OLORIN.md),
`"person"` dropped (no clear population source for a solo project yet,
can be added later without a migration since `type` is plain TEXT).

**3. Retrieval.** A `search_entities` agent tool (same shape as
`search_history`), not a system-prompt injection like Capsules —
Capsules are small and bounded (5-8 modules); entities accumulate
indefinitely, so injecting all of them would eventually reopen the exact
context-budget problem Capsules already solved by not doing that.

**4. Dedup.** Keyword-overlap match against existing entities (same
mechanism `search_history` already uses), deliberately conservative —
a false-positive merge (silently overwriting one real thing's
description with a different real thing's) is worse than a
false-negative (two near-duplicate rows, still findable via search).

### Implementation
- `memory/entities.py` (new) — schema (with `repo_path` added, a real
  deviation from the original stub — without it, a decision from a
  different repo entirely would show up in this repo's results), the
  commit gate (confidence >= "medium" only), the dedup matcher, and
  extraction (prompt + JSON parsing) all in one module. Duck-typed
  `llm_client`, matching `memory/journal.py`'s pattern exactly — no
  hard dependency on `core/`.
- `tools/entities.py` (new) — `search_entities` tool wrapper, same
  honest-error shape as `tools/history.py`.
- `tools/registry.py` — wired in as the sixth tool.
- `core/agent.py` — `_backend_used()` extracted out of `_log_
  conversation()` for reuse; new `_extract_entities()`, called right
  after `_log_conversation()`, but ONLY on the clean `finish_reason==
  "stop"` path — not on max-steps/error exits, since those aren't a
  real grounded conclusion worth extracting facts from.
- `cli.py` — new `entities` command for human browsing, mirroring
  `history`.

### Bug found + fixed live: dedup matching missed a real duplicate
The original dedup used one blended ratio over name+description
combined, threshold 0.6. Smoke test (`python -m memory.entities`,
hand-constructed candidates, no LLM) caught it immediately: two
candidates for the same real decision — "Daemon deferral" and "Daemon
deferral decision" — shared near-identical names (keyword overlap ratio
1.0) but phrased their descriptions differently enough that the blended
ratio came out to 0.3, well under the bar. Inserted a duplicate instead
of updating. **Fix:** split into two independent signals — name-only
overlap against a lower `_NAME_MATCH_THRESHOLD=0.5` (names are short,
stable labels), OR combined name+description overlap against the
original higher `_DEDUP_MATCH_THRESHOLD=0.6` (the fallback for when
names differ but descriptions clearly match). A candidate matches if
*either* clears its own bar. Re-ran the smoke test: correctly matched
and updated, total count stayed at 2, not 3.

### A real mystery, diagnosed rather than guessed at
A real `ask "why was the daemon deliberately deferred instead of
built?"` made a visible extraction call (an extra Groq request in the
log right after the final answer) but `cli.py entities` showed nothing
committed afterward. Rather than guess, wrote `debug_entity_extraction.py`
— same pattern as `test_faramir_tools.py`/`test_websearch_backends.py`
— replaying the EXACT same query/answer in isolation and printing the
raw model response before any parsing.

**Result: the mechanism is fine.** The isolated replay returned three
well-formed, high-confidence candidates — prompt, JSON parsing, and the
commit gate all worked correctly. Traced the real cause: `providers/
groq_provider.py`'s `chat()` never sets `temperature` in the API call,
so Groq's non-zero server default applies. The identical prompt can
legitimately return different output across separate calls — fine for
the main conversational answer, not fine for a classification task
("does this qualify as a decision") that should ideally be consistent.

**Raised directly: fix it or accept it?** Threading a `temperature`
parameter through `LLMClient` → every provider (`Groq`, `Cerebras`,
`Ollama`) is a real, contained, well-motivated change — but a multi-file
one, so it wasn't done silently. **Decided: accept the variance.** A
missed extraction isn't data corruption, just an opportunity that didn't
get captured this run; the same decision tends to resurface and get
caught on a later pass — same graceful-degradation posture this project
already applies to `search_history`'s keyword-recall limits and
Journal's LLM-call failures. Added cheap, permanent observability
instead: `memory/entities.py` now logs raw candidate counts and each
commit/update at INFO level, but only when non-zero — the common
empty-result case doesn't spam the logs, but a future silent miss is
now diagnosable from normal logs without needing a special script again.

### Final live verification
Re-ran the same real query after the observability logging was added.
Clean success end-to-end:
```
entity extraction: 3 raw candidate(s) from groq
entity memory: new entity #7 (decision: Defer Daemon Development)
entity memory: new entity #8 (concept: Import Overhead vs Inference Time Tradeoff)
entity memory: new entity #9 (concept: Bounded Performance Tax)
entity memory: committed 3 entities
```
`cli.py entities` showed exactly these 3, correctly scoped to the real
repo's `repo_path` — not the earlier smoke-test rows under fake repo
paths, confirming repo-path scoping works. **Bonus finding:** this same
run is the first live confirmation that the agent calls `search_entities`
on its own inside a real ReAct loop, not just when exercised directly —
step 1 called it, got an honest "nothing remembered yet" (correct, table
was empty for this repo before this run), and the loop continued
normally into `search_history` and a final answer.

### Status
- [x] Design conversation resolved: scope, population mechanism, type
  scope, retrieval shape, dedup policy — all decided directly before
  code, same discipline as every other feature in this project
- [x] `memory/entities.py`, `tools/entities.py`, `tools/registry.py`,
  `core/agent.py`, `cli.py` all implemented
- [x] One real bug found (dedup blended-ratio miss) and fixed, re-
  verified live
- [x] One real tradeoff found (Groq temperature/sampling variance) and
  deliberately accepted, with cheap observability added instead of a
  fix
- [x] Final live run: 3 real entities committed end-to-end, `search_
  entities` confirmed being called by the agent unprompted
- [x] `OLORIN_PROJECT.md` Sections 6, 9, 10, 11, and 16 updated to
  reflect the full design and both findings

**Section 11's V2 tier is now fully, genuinely closed.** The only
remaining open item from the entire V2 checklist is
`COMPLEXITY_THRESHOLD` re-tuning, correctly gated on accumulating real
traffic rather than more code. Next up: V3.

---

## 2026-07-18 — V3 kickoff: import graph ("why is this file important?") designed and built, live verification pending

### Picking a V3 item
V3 has five open items of genuinely different shape and cost (GPU
benchmark table, import graph, git integration, document ingestion,
cross-repo querying). Rather than default to the cheapest one, asked
directly which to start with. **Chosen: import graph** — the item
Section 10's entity-memory design conversation (2026-07-16) had already
scoped out as a separate, already-defined V3 item ("object relations in
a repo" vs. entity memory's "agent's relation to things"), so its shape
was already well-understood going in.

### Three design questions resolved before any code, same discipline as entity memory
1. **Language scope: multi-language via tree-sitter**, not Python-only —
   reuses `chunker.py`'s existing grammar infrastructure rather than a
   second parsing approach.
2. **When to build it: at index time, cached**, like Capsules — not
   computed fresh on every question.
3. **Agent access: a new `file_importance` tool**, not a system-prompt
   injection like Capsules — import-graph data doesn't share Capsules'
   small-and-bounded property (a large repo's full graph could be
   sizeable), same reasoning that put `search_entities` behind a tool
   instead of an injection back on 2026-07-16.

### Verifying tree-sitter assumptions before writing extractors, not after
Before writing `indexer/imports.py`'s node-type-to-extractor mapping,
ran real parses (via a sandboxed Python environment with
`tree-sitter-language-pack` installed) against representative Python,
Rust, TypeScript, and C snippets and dumped the actual AST — confirming
field names (`import_from_statement.module_name`, `import_statement
.source` for JS/TS) and child node types (Rust's `scoped_identifier`,
C's `string_literal` vs. `system_lib_string`) directly rather than
assuming from training-data memory of these grammars, which could
easily be stale or subtly wrong for a specific grammar version. Same
"verify before trusting" instinct that drove `test_faramir_tools.py`
and the tree-sitter field-name checks `chunker.py`'s own build already
relied on.

### `indexer/imports.py` built — extraction + resolution, two-stage
- `extract_raw_imports(content, language)` — parses one file, returns
  raw specifiers exactly as written (`"core.llm_client"`, `"./bar"`,
  `"crate::config"`), no resolution yet. Mirrors `chunker.py`'s
  grammar-lookup-with-soft-fallback pattern (unsupported language or
  missing grammar returns `[]`, never raises).
- `resolve_imports(importer_path, raw_specs, language, repo_files)` —
  turns raw specifiers into repo-relative file paths, keeping only ones
  that match a real file in `repo_files` (the walker's own output,
  never a second filesystem check) and dropping everything else
  (external packages, stdlib, unresolvable specifiers) silently — the
  expected, common case, not a failure.
- **Scope deliberately narrowed to Python, Rust, JavaScript,
  TypeScript, C, C++** — Go and Java extraction was considered and
  explicitly left out, not partially wired up: resolving either
  correctly needs information this project doesn't parse (Go's module
  name from `go.mod`, Java's source root convention), and extraction
  with no working resolution is dead code that always drops everything.
  Same "deliberately conservative for V1" posture `chunker.py`'s own
  `CHUNKABLE_NODES` comment already takes, applied to a new module.
- Known limitations documented in the module docstring rather than
  silently accepted: Rust grouped imports (`use std::{io, fs};`) aren't
  parsed; `crate::` resolves as rooted at the repo root (correct for
  this project's own single-crate `indexer_core/`, not a multi-crate
  workspace); JS/TS path aliases (`tsconfig.json`'s `paths`) aren't
  resolved, only relative (`./`, `../`) specifiers are.
- **Unit-verified via its own `__main__` smoke test** — hand-built
  Python/Rust/TypeScript source strings with asserted expected resolved
  paths, run in the sandboxed verification environment before writing
  the real file to this repo. All assertions passed.

### `memory/import_graph.py` built — storage, piggybacking on the existing hash-skip loop
- Single table, `import_edges(repo_path, importer, imported)`,
  `set_file_imports()` (delete-then-insert per file, mirroring
  `store.py`'s `delete_file()`-before-upsert pattern for chunks),
  `get_importers()`/`get_imports()` (the two query directions),
  `has_any_edges()` (existence check for the tool's honest-error path).
- **Deliberately no separate hash-tracking table, unlike Capsules'
  `needs_regeneration()`.** Capsules need one because module-level
  generation doesn't align to any single file's reindex decision — a
  capsule covers several files. Import-graph edges are keyed one-to-one
  to the same file `cli.py`'s existing per-file hash comparison already
  gates, so `set_file_imports()` is simply called from inside that same
  branch, right after a file's chunks are upserted. Genuinely simpler
  than Capsules' design, not a corner cut — the two features' natural
  granularity is just different.
- Known limitation, same class as `store.py`'s own, not introduced
  here: no delete-detection for files removed from the repo entirely
  (pre-existing gap in the indexing loop generally, not made worse by
  this feature).

### `tools/importance.py` built — the `file_importance` agent tool
- Thin wrapper: `has_any_edges()` for the "not indexed yet" honest
  error (matching `tools/search.py`/`tools/history.py`'s pattern),
  otherwise `get_importers()`/`get_imports()` reshaped into
  `{file, imported_by, imported_by_count, imports, imports_count,
  verdict}` — the `verdict` field is a plain-language one-liner
  ("imported by N files ... — high/moderate/low blast radius") so a
  local model doesn't have to reason its way to the obvious conclusion
  from raw counts alone.

### Wired in: `tools/registry.py` (seventh tool), `core/agent.py` (system prompt), `cli.py` (`_index_repo()`)
- `tools/registry.py` — schema + dispatch branch, same two-part pattern
  as every prior tool addition.
- `core/agent.py`'s `tools_section` — "six tools" → "seven tools",
  `file_importance` described alongside the others. `execute_tool()`'s
  uniform `repo_root`-injection dispatch needed no special case.
- `cli.py`'s `_index_repo()` — a `repo_files` set built once from the
  walker's output (existence-check input for `resolve_imports()`), and
  a new try/except block right after chunk upsert in the per-file loop:
  extract → resolve → `set_file_imports()`. Best-effort, matching the
  existing capsule-generation failure handling in the same function —
  an extraction failure on one file shouldn't lose that file's already-
  successful chunking/embedding work.

### Verification status — honest, not assumed
**What's actually been verified:** grammar node types/field names
against real parses; `indexer/imports.py`'s own extraction+resolution
logic against hand-built examples with asserted output, in the same
sandboxed environment used for the grammar checks. **What hasn't:** any
of it running against this repo's real `cli.py index` pipeline, or a
real `file_importance` tool call through the actual agent loop — this
session had no way to execute Python directly against Aryavart's real
machine (filesystem read/write access only, no shell execution there),
unlike sessions where a live run closed out the same day. Logged
explicitly rather than checked off early — this project's whole track
record (bugs #10, #13, #14, entity memory's dedup miss) is that "the
logic looks right" and "a live run against a real repo confirms it" are
genuinely different bars, and only one of them has been cleared so far.

### Status
- [x] Design conversation resolved: language scope, build timing, agent-
  access shape — all decided directly before code
- [x] `indexer/imports.py`, `memory/import_graph.py`,
  `tools/importance.py` all implemented
- [x] Wired into `tools/registry.py` (7th tool), `core/agent.py` (system
  prompt), `cli.py` (`_index_repo()`'s per-file loop)
- [x] Grammar assumptions verified against real tree-sitter parses
  before writing extractors that depend on them
- [x] Extraction + resolution logic unit-verified via a standalone smoke
  test with asserted expected output
- [ ] **NOT yet live-verified against this real repo** — next session's
  first action: `python -m indexer.imports`, `python -m
  memory.import_graph` (both fast/offline smoke tests), then
  `python cli.py index .`, then `python cli.py ask "Boromir, why is
  core/agent.py important?"` end-to-end through the real agent loop.
- [ ] `OLORIN_PROJECT.md` Sections 6, 11, 13, 16 updated to reflect
  design + implementation + the explicit not-yet-verified status

**V3's import-graph item is implemented but not closeable yet** — the
same bar every other feature in this project has had to clear
(live-verified against the real repo, not just unit-tested in
isolation) hasn't been cleared. First action next session.

---

## 2026-07-18 (continued) — Import graph live-verified end-to-end; bug #15 found and fixed

### The live run
Aryavart ran the exact three commands flagged as pending: `python -m
indexer.imports`, `python -m memory.import_graph`, `python cli.py
index .`, then `python cli.py ask "Boromir, why is core/agent.py
important?"`.

**`memory/import_graph.py`'s smoke test passed cleanly** — storage
layer confirmed correct in isolation against this real machine's SQLite
setup, no surprises.

**`indexer/imports.py`'s smoke test failed with an `AssertionError`.**
Raw specs from the test's Python source came back as `['os', 'config',
'core.llm_client', '.', '.reader']` — the resolved list only contained
`['config.py', 'core/llm_client.py']`, missing `tools/reader.py` and
`tools/registry.py` that the test asserted should be there.

### Diagnosing it — turned out to be two separate things, not one
**First: the test's own expected values were wrong**, not just the
resolution logic. The test used relative-import syntax (`from . import
registry`, `from .reader import read_file`) with `importer_path="core/
agent.py"`, but asserted those would resolve into `tools/registry.py`/
`tools/reader.py` — which contradicts real Python semantics. A relative
import from `core/agent.py` can only ever resolve within `core/`, never
cross into a sibling package like `tools/`. The test's own source
syntax didn't match how this actual repo does it (`core/agent.py`
really imports `tools.registry` via an *absolute* import) — an
authoring mistake in the test itself.

**Second, underneath that: a real extraction bug, confirmed via a fresh
sandboxed tree-sitter dump before touching any fix.** `from . import
registry` was only ever contributing `"."` (the bare module_name field
text) to the raw-specs list — `_extract_python()`'s attempt to also
capture the imported name `registry` (so a bare relative import can
resolve to a submodule file) was silently never firing. Root cause,
found by dumping a real parse tree and checking node identities
directly: the code compared `child is module_node` to find children
positioned after the module_name field in the AST, but tree-sitter's
Python bindings return a *new wrapper object* on every node access —
`node.child_by_field_name("module_name")` and the same underlying node
reached via `node.children` iteration are never the same Python object,
even though they cover the identical byte range. The identity check
silently never matched, so the whole "also capture the imported name"
branch never executed, despite reading correctly in review.

**Fix:** compare `child.start_byte == module_node.start_byte and
child.end_byte == module_node.end_byte` instead of `is`. Verified in
the sandboxed environment against two cases before touching the real
repo file: an importer in the *same* package as the relative imports
(where they should resolve) and an importer in a *different* package
(where they correctly should NOT) — both passed. The smoke test's own
assertions were corrected at the same time, replacing the single wrong
expectation with both of these real cases, so the test itself is now
actually checking something true about relative-import semantics
rather than a fixed list that happened to look plausible.

### Real end-to-end verification, the bar that actually matters
With the fix applied, `python cli.py index .` ran cleanly against this
real repo: 147 chunks written, 8 files (re)indexed, 5 capsules
regenerated to pick up the new files (`indexer/imports.py`,
`memory/import_graph.py`, `tools/importance.py`, plus the modified
`cli.py`/`core/agent.py`/`tools/registry.py`) — no `import graph failed
for ...` warnings anywhere in the output, meaning every file's
extraction+resolution completed without hitting the try/except
safety net in `_index_repo()`'s new import-graph block.

`python cli.py ask "Boromir, why is core/agent.py important?"` then ran
the real thing this feature was built for: direct-address routing sent
it to Boromir, step 1 called `file_importance({'path': 'core/agent.py'})`,
step 2 produced a final answer. **Fact-checked against `core/agent.py`'s
actual real imports:** the answer correctly reported it's imported by
exactly `cli.py` (true — no other file in this repo imports
`core/agent.py`), and correctly listed its own imports as `config.py`,
`core/llm_client.py`, `core/logging_config.py`, `memory/__init__.py`,
and `tools/registry.py`. The `memory/__init__.py` result is worth
calling out specifically — it's not a bug, it's the *correct* resolution
for `core/agent.py`'s three separate `from memory import ...`
statements (`capsules as capsules_store`, `entities as entities_store`,
`history`): importing a name from a package really does execute that
package's `__init__.py` first, so `memory/__init__.py` genuinely is
what gets imported, correctly deduplicated down to one edge across all
three statements.

### Status
- [x] Design conversation resolved: language scope, build timing, agent-
  access shape — all decided directly before code
- [x] `indexer/imports.py`, `memory/import_graph.py`,
  `tools/importance.py` all implemented
- [x] Wired into `tools/registry.py` (7th tool), `core/agent.py` (system
  prompt), `cli.py` (`_index_repo()`'s per-file loop)
- [x] Grammar assumptions verified against real tree-sitter parses
  before writing extractors that depend on them
- [x] **Live-verified end-to-end**: `cli.py index .` (real repo, no
  extraction failures) → `cli.py ask "Boromir, why is core/agent.py
  important?"` (real agent loop, real `file_importance` call,
  fact-checked accurate answer)
- [x] Bug #15 found (by the live run, not the isolated smoke test —
  whose own assertions had encoded the bug's symptom as "expected") and
  fixed: bare `from . import X` imports weren't resolving due to a
  tree-sitter node-identity comparison that silently never matched.
  Fixed with a byte-offset comparison; re-verified in isolation before
  reapplying to the real file.
- [x] `OLORIN_PROJECT.md` Sections 11, 13, and 16 updated to reflect the
  live verification and bug #15

**V3's import-graph item is now genuinely closed** — implemented,
live-verified against the real repo and real agent loop, one real bug
found and fixed, same bar every other feature in this project has had
to clear. V3's remaining items (GPU benchmark table, git integration,
document ingestion) are all still open and well-specified.

---

## 2026-07-18 (continued) — Git integration built: `git_diff` tool, live verification pending

### Design questions resolved directly before code, same discipline as import graph
1. **Default diff scope: uncommitted changes** (`git diff HEAD` —
   working tree + staged vs. HEAD), not "last commit only" — "what have
   I changed?" is the more common real question, and an explicit `ref`
   argument still covers "what just happened in commit X."
2. **Scope: just `git_diff`, not a companion `git_log` tool** — matches
   the roadmap item's literal wording ("git diff → agent explains what
   changed") rather than expanding it preemptively. A log/history tool
   is a legitimate separate follow-up if it turns out to be needed, not
   assumed necessary now — same YAGNI instinct as everywhere else in
   this project.
3. **Security for a model-supplied ref reaching a subprocess argv:
   strict allowlist regex + explicit leading-dash rejection + a literal
   `--` separator before the pathspec in every invocation** — asked
   directly rather than picking one defense and hoping. Deliberately
   layered: the regex alone doesn't stop `-` appearing mid-string from
   being fine while still rejecting a *leading* `-` (option-injection
   shapes like `--upload-pack=evil` always start with `-`), and the
   `--` separator alone doesn't stop a leading-dash ref from being
   parsed as an option before git even reaches that separator.

### Verifying the security claim, not just reasoning about it
Built a throwaway real git repo in the sandboxed verification
environment (`git init`, two commits' worth of realistic multi-file
changes) and tested the actual implementation against it before writing
anything to the real repo: a real multi-file diff (confirmed correct
per-file splitting into 3 files), a real option-injection-shaped ref
(`"--upload-pack=evil"` — confirmed rejected before reaching
subprocess), a nonexistent ref, and a non-git directory. One real bug
found during this sandbox verification itself (not after writing to the
real repo): the "not a git repository" error-message check was
case-sensitive (`"not a git repository" in stderr`) but git's actual
message capitalizes it (`"Not a git repository"`) — caught immediately
because the test asserted on the error actually being returned, not just
on the command not crashing. Fixed with `.lower()` before the substring
check, re-verified in the same sandbox run.

### `tools/git_diff.py` built
- `git_diff(repo_root, ref=None, max_chars=20000)` — wraps `git diff`
  via `subprocess.run()` with a list (never `shell=True`, ruling out
  shell injection outright; the ref-validation work above is about a
  narrower, still-real risk: option injection via argv, not shell
  metacharacters).
- Runs `git diff <ref> --stat --` for a summary (always returned in
  full, capped defensively at 2000 chars) AND `git diff <ref> --` for
  the actual unified diff — two subprocess calls, so the agent still
  knows the complete scope of what changed even if the unified diff
  itself gets truncated.
- `_split_diff_by_file()` splits the unified diff on each `"diff --git
  a/X b/Y"` header into `{"file", "content"}` chunks — the same shape
  `search_codebase`'s chunks already use. This wasn't just a formatting
  choice: it means the result feeds `Agent._accumulate_retrieved_
  chunks()`'s existing multi-file complexity signal (Section 7/13) with
  zero changes needed there — a diff touching five files legitimately
  scores as more complex than one touching a single file, for free.
- `max_chars` truncation drops whole files once the running total would
  exceed the cap, rather than truncating mid-file-diff — every chunk the
  agent sees is a complete, coherent diff for that file, same philosophy
  as `read_file`'s truncation.

### Wired in: `tools/registry.py` (8th tool), `core/agent.py` (system prompt)
- Schema + dispatch branch in `tools/registry.py`, same two-part pattern
  as every prior tool.
- `core/agent.py`'s `tools_section` — "seven tools" → "eight tools",
  `git_diff` described alongside the others.

### Verification status — honest, not assumed
**What's been verified:** subprocess handling, ref validation (including
the real option-injection test case), diff-splitting, `max_chars`
truncation, and all error paths (invalid ref, nonexistent ref, non-git
directory) — all against a real throwaway git repo in the sandboxed
environment, not just reasoned about. **What hasn't:** a real `cli.py
ask` call against this actual repo that triggers `git_diff` through the
real agent loop. Given import graph's own bug #15 was only found by
exactly that kind of live run earlier today — not by an isolated smoke
test whose assertions turned out to be subtly wrong — this gap is
flagged with the same seriousness, not treated as a formality.

### Status
- [x] Design conversation resolved: default diff scope, tool scope
  (just `git_diff`), ref-validation security posture — all decided
  directly before code
- [x] `tools/git_diff.py` implemented
- [x] Wired into `tools/registry.py` (8th tool), `core/agent.py` (system
  prompt)
- [x] Subprocess handling, security validation, and diff-splitting all
  verified against a real throwaway git repo in an isolated sandbox
- [x] One real bug found and fixed during that sandbox verification
  itself (case-sensitive error-message check)
- [ ] **NOT yet live-verified against this real repo's actual agent
  loop** — next session's first action: `python -m tools.git_diff`
  (fast/offline, runs against this repo's real git state), then a real
  `cli.py ask "Boromir, what did I just change?"` after making a small
  uncommitted edit, then a ref-based query to confirm the non-default
  path too.
- [x] `OLORIN_PROJECT.md` Sections 6, 11, 13, 16 updated to reflect
  design + implementation + the explicit not-yet-verified status

**Git integration is implemented but not closeable yet** — same bar
import graph just got held to earlier today. First action next session.

---

## 2026-07-18 (continued) — Bug #16: `git_diff` smoke test caught a truncation bug the sandbox never exercised

### The report
Aryavart ran `python -m tools.git_diff` against this real repo. Result:
```
ref=HEAD (uncommitted changes)
files_changed=12 files_shown=0 truncated=True
```
twelve real changed files (`ENGINEERING_JOURNAL.md`, `OLORIN_PROJECT.md`,
`cli.py`, `config.py`, `core/agent.py`, `core/llm_client.py`,
`indexer/embedder.py`, `indexer/store.py`, `indexer_core/Cargo.lock`,
`providers/base.py`, `tools/reader.py`, `tools/registry.py` —
accumulated since V1, since Aryavart hasn't committed yet), but zero
shown. Both the option-injection guard and the non-git-repo error path
worked correctly in the same run, isolating the problem to truncation
specifically.

### Diagnosis
The bug was structural, not a typo: `git_diff()`'s truncation loop used
`break` when a file's diff content didn't fit the remaining `max_chars`
budget — correct if files are processed smallest-first, wrong given
they're processed in git's own (alphabetical) order. `ENGINEERING_
JOURNAL.md` sorts first alphabetically and, after a full day of
continuous doc updates, is genuinely huge — its diff alone almost
certainly exceeds the 20,000-char budget on its own. Hitting `break` on
the very first file meant the loop gave up entirely, even though
several much smaller files later in the list (`config.py`,
`tools/reader.py`, `providers/base.py`, `indexer_core/Cargo.lock`) would
clearly have fit within the budget on their own.

**Why the sandbox verification missed this:** the throwaway git repo
used to verify `git_diff.py` before shipping it had a real multi-file
diff, but every file in it was small — nothing in that test repo's
diff ever came close to `max_chars`, so the `break`/`continue` distinction
never mattered for it. Correct implementation logic tested against an
incomplete range of realistic inputs is a gap in what the test
exercised, not a flaw in the security or correctness reasoning that
went into the tool — the same broad lesson as bug #15's smoke test
having encoded a wrong assumption as "expected," just a different shape
of the same underlying risk (isolated tests only catch what they're
shaped to catch).

### Fix and re-verification
Reproduced the exact failure shape first, before touching the fix:
built a fresh sandboxed throwaway repo with one large file ordered
alphabetically first (3000 lines) and one small file after it,
confirmed `files_shown=0` reproduced there too. Changed `break` to
`continue` in the truncation loop — a file that doesn't fit gets
skipped, not treated as an end-of-budget signal for the entire diff.
Re-ran the reproduction case (now correctly shows the small file,
skips the large one) and the tool's full original test suite (multi-file
diff, option-injection ref, nonexistent ref, non-git directory) to
confirm nothing else regressed. Applied to the real repo file — caught
a copy-paste mismatch in the first edit attempt (the `return {` line
got dropped, leaving a syntax error) via a follow-up read of the file,
fixed immediately.

### Status
- [x] Bug #16 found (via the first live smoke test), diagnosed,
  reproduced in isolation, fixed, and re-verified
- [x] `OLORIN_PROJECT.md` Sections 13 and 16 updated with bug #16's
  full story
- [ ] **Still open, deliberately** — Aryavart hasn't committed since V1,
  so the two full-agent-loop verification steps (a real `cli.py ask`
  triggering `git_diff`, and a ref-based query against a real commit)
  are waiting on a real commit existing to test against cleanly. Not a
  gap in the tool; a sequencing choice.

**Git integration's implementation is now more trustworthy than it was
this morning — a second real bug found and fixed the same day it was
written, both times by actually running it, not by re-reading the code
more carefully.** Two verification steps remain, correctly deferred
until there's a commit to test against.
