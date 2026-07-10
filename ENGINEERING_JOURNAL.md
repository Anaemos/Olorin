# Olorin — Engineering Journal

A running build log. Each entry: what got built, why, and the interview-ready
explanation for it. Full architectural rationale lives in `OLORIN_PROJECT.md`;
this file is the day-by-day record of actually building it.

---

## 2026-07-06 — Local models + Phase 1 scaffolding

### Local models finalized
- Deleted old `olorin`-named qwen2.5-coder wrapper (leftover from before the
  3-agent split) to avoid naming confusion.
- Rebuilt clean from base weights:
  - `boromir` — `qwen2.5-coder:7b-instruct-q4_K_M`, 16K context via Modelfile
  - `faramir` — `deepseek-r1:8b`, 16K context via Modelfile
- Verified via `ollama list`. `qwen3.5:9b` (unrelated project) left untouched.

**Interview angle:** explaining *why* a community GGUF or a leftover
experimental Modelfile can silently under-perform (low GPU utilization,
truncated context) shows you understand Ollama's model-layering system
(named models are Modelfile config layers over shared `FROM` base weights),
not just "I ran ollama pull."

### Faramir hallucination test (informal, pre-RAG)
- Asked `deepseek-r1:8b` a pure-recall trivia question (Tolkien lore) with
  zero retrieval context.
- Result: confidently fabricated plausible-sounding but incorrect details.
- **Conclusion:** this is expected behavior for an 8B parametric model with
  no grounding — not a config bug. Confirms the RAG-first design is correct:
  Faramir should never answer from bare memory in the real system, only
  reason over retrieved context.
- **Design decision (logged in OLORIN_PROJECT.md Section 13):** added
  `web_search` as a shared V2/V3 tool across all three backends — not a
  Groq-only feature, since Groq's `llama-3.3-70b-versatile` has the identical
  blind spot without a tool. Orthogonal to the complexity-scorer routing.
- **Open risk flagged for V2:** `deepseek-r1:8b` emits `<think>...</think>`
  before real output, which can break tool-call parsing. Must test Faramir's
  raw tool-calling in isolation before wiring `web_search` into its loop.
  Boromir (Qwen-based, fine-tuned for function calling) is not expected to
  have this issue.

### Environment setup
- Python **3.11.9** chosen over newer versions — best wheel availability for
  `chromadb`, `sentence-transformers`, CUDA-enabled `torch` at time of writing.
- `venv` created inside `olorin/` (`python -m venv venv`).
- `.gitignore` written before any other file touched the repo, to guarantee
  `.env` (API keys) never gets committed.

### Phase 1 code — provider abstraction begins
- **`config.py`** — single source of truth for all env vars, model names,
  and tunable thresholds (`COMPLEXITY_THRESHOLD`, `MAX_AGENT_STEPS`,
  `CIRCUIT_BREAKER_COOLDOWN_MINUTES`). Fails fast with a clear error if
  `GROQ_API_KEY` is missing, rather than failing silently deep in a provider
  call later.
  - **Interview angle:** "fail fast at config load, not mid-request" is a
    deliberate reliability pattern, not an accident.
- **`providers/base.py`** — abstract interface (`BaseProvider`) + normalized
  `ProviderResponse` dataclass + `ProviderError` (carries `is_rate_limit`
  flag for the future circuit breaker).
  - **Interview angle:** this is dependency inversion — `LLMClient` and the
    agent loop depend on this abstract contract, never on a concrete
    provider's SDK. Swapping Groq → Together AI later is a new class, zero
    changes to calling code.

### `providers/groq_provider.py`
- Reuses `openai` SDK pointed at `https://api.groq.com/openai/v1` — Groq is
  OpenAI-compatible, so no bespoke HTTP client needed.
- SDK exceptions (`RateLimitError`, `APIConnectionError`, `APITimeoutError`)
  caught and normalized into `ProviderError`, with `is_rate_limit` flag set
  correctly for the future circuit breaker.
- `is_available()` is a cheap pre-flight check (key + client exist) —
  deliberately does NOT send a real request, to avoid burning rate limit
  quota on a health check.
- **Verified live:** real response from `llama-3.3-70b-versatile`, 718ms,
  46 input / 5 output tokens.

### `providers/ollama_provider.py`
- Same OpenAI-compatible pattern, pointed at `localhost:11434/v1`.
- Takes `model_name` in `__init__` — one instance = one model. `LLMClient`
  will hold two instances (Boromir, Faramir) rather than this class knowing
  about personas. Keeps routing logic out of the provider layer.
- `is_available()` calls `models.list()`, NOT a chat completion — a stray
  health-check chat call could force an unwanted model swap given only one
  local model is resident in VRAM at a time.
- **Verified live:** both models responded correctly. Confirmed Ollama's
  keep-alive/idle-unload behavior firsthand — Boromir loaded, then Faramir
  loaded and evicted Boromir from VRAM automatically; Faramir stayed
  resident afterward until manually stopped (`ollama stop <model>`).
- **Note on latency:** first-call latency (~41s Boromir, ~45s Faramir) is
  dominated by cold-start model loading into VRAM, not inference time.
  A warm second call to the same model is expected to be dramatically
  faster. Don't mistake this number for steady-state performance.

### Status
- [x] `config.py`
- [x] `providers/base.py`
- [x] `providers/groq_provider.py`
- [x] `providers/ollama_provider.py`
- [x] `core/llm_client.py` — routing + circuit breaker verified end-to-end
- [x] End-to-end test: both backends respond to a hardcoded prompt

### `core/llm_client.py`
- Implements `score_complexity()` exactly per Section 7's formula (chunk
  count, token volume, query length, planning-keyword density).
- **V1 scope decision:** automatic fallback always routes local traffic to
  Boromir, not a Boromir/Faramir specialist split — that's explicitly a V4
  feature (LangGraph refactor) per Section 11. V1's tools are all
  code-focused, which is Boromir's lane anyway. Faramir stays reachable via
  explicit `--provider faramir` override for manual testing.
- Circuit breaker: trips only on `RateLimitError` (5 min cooldown, config-
  urable). Other Groq failures (timeout, connection) fall back to local for
  that single call without tripping the breaker — a transient blip
  shouldn't lock out Groq for 5 minutes.
- **Verified live:** low-complexity query routed to Boromir (9.5s, includes
  cold VRAM load), high-complexity query correctly routed to Groq (1.8s).
  Both models gave appropriately cautious "need more context" answers given
  the test used stubbed/fake retrieved chunks rather than real RAG output
  — correct behavior, not a bug, since the indexer doesn't exist yet.
- **Still open from Section 7:** query caching (SQLite, sha256 key) and
  structured logging aren't built yet — bundled in the same V1 checklist
  item but deferred to keep this commit focused on routing correctness.

### Status
- [x] `config.py`
- [x] `providers/base.py`
- [x] `providers/groq_provider.py`
- [x] `providers/ollama_provider.py`
- [x] `core/llm_client.py` (routing + circuit breaker)
- [x] Query cache (SQLite) — verified live
- [x] Structured logging — verified live
- [ ] Tool registry + agent loop

### `memory/cache.py` + `core/logging_config.py`
- Cache key: `sha256(query + context_hash + model_name)`, exactly per
  Section 7. `context_hash` is itself a hash of the retrieved chunks —
  meaning cache invalidation on re-index is automatic and free: new
  retrieval results change the hash, which changes the key, so stale
  entries are simply never looked up again (not explicitly deleted,
  just orphaned). No manual invalidation logic needed.
- **Scope decision:** caching only applies when `tools is None`. A
  tool-using response is tied to live state (file contents, search
  results) at the moment it ran — caching that risks serving stale tool
  output as if fresh. Tool-free Q&A is safe to cache; tool-driven agent
  steps are not, in this design.
- Logging: single `get_logger()` factory in `core/logging_config.py`,
  writes to both console and `logs/olorin.log` (gitignored). Every
  routing decision — including cache hits, Groq failures, and circuit
  breaker trips — gets a structured log line with provider, model,
  latency, and truncated query.
- **Verified live:** first run — real Boromir call (10.4s) and real Groq
  call (2.0s), both logged correctly with actual latency/provider/model.
  Second run (identical queries) — both served from cache, `0ms`,
  logged distinctly as `cache HIT`, identical answers returned.

**Section 7 of OLORIN_PROJECT.md is now fully implemented and proven:**
routing, circuit breaker, caching, and logging all working end-to-end.
Next: tool registry (Section 9) and the raw ReAct agent loop (Section 9).

### `tools/reader.py`, `tools/search.py`, `tools/registry.py`
- `read_file` / `list_files` fully implemented and scoped to a `repo_root`
  passed in at dispatch time — never trusted from model-supplied tool-call
  arguments. Path traversal (`../../../etc/passwd`-style) is blocked via
  `os.path.commonpath` resolution before any file touch.
  - **Interview angle:** same principle as SQL parameterization — never
    let untrusted input (here, LLM-generated tool arguments) control the
    security-relevant part of an operation. Directly relevant if a prompt-
    injected instruction hidden in an indexed file tries to redirect a
    tool call.
- `search_codebase` is a deliberate stub: fails LOUDLY with an explicit
  "not implemented, indexer not built yet" message rather than a silent
  empty result — lets the agent reason around the gap (fall back to
  list_files/read_file) instead of assuming something broke silently.
- `tools/registry.py` holds both the OpenAI-format tool schemas AND the
  dispatch function (`execute_tool`) in one file — adding a new tool later
  (e.g. `web_search` in V2/V3) is a two-part change entirely within this
  module: schema + dispatch branch, nothing else touched.
- **Verified live:** `list_files` found 14 real repo files, `read_file`
  correctly read `config.py` (3365 chars), the search stub returned its
  intended error message, an unknown tool name returned a graceful error
  dict instead of raising, and the path-traversal attempt was correctly
  blocked.

### Status
- [x] `config.py`
- [x] `providers/base.py`
- [x] `providers/groq_provider.py`
- [x] `providers/ollama_provider.py`
- [x] `core/llm_client.py` (routing + circuit breaker + cache + logging)
- [x] `tools/reader.py`, `tools/search.py` (stub), `tools/registry.py`
- [x] `core/agent.py` — raw ReAct loop, working end-to-end

### `core/agent.py` — raw ReAct loop
- Built exactly per Section 9's pseudocode: send messages + tools → if
  `finish_reason == "stop"` return final answer → if `"tool_calls"` execute
  each via `tools/registry.execute_tool`, append results as `role: "tool"`
  messages, loop. Hard-capped at `config.MAX_AGENT_STEPS`.
- `build_system_prompt()` is deliberately the plain V1 version — OLORIN.md
  per-repo injection (Section 8) is explicitly V2, not built yet.

### Real bug found + fixed: Ollama tool-calling transport quirk
- **First run failed silently-ish:** asked the agent to read `config.py`.
  Boromir correctly reasoned it needed `read_file`, but the loop returned
  immediately with a "final answer" that was literally the raw JSON tool
  call as text: `{"name": "read_file", "arguments": {"path": "config.py"}}`.
  `finish_reason` came back `"stop"`, not `"tool_calls"` — the structured
  tool-call mechanism never fired.
- **Diagnosed via web search, not guessed:** this is a well-documented,
  ecosystem-wide issue with Ollama's OpenAI-compatible endpoint
  (`/v1/chat/completions`) and qwen2.5-coder specifically — confirmed
  independently in GitHub issues from Cline, OpenCode, and a Hermes-based
  agent project, all hitting the identical symptom. Ollama's own docs mark
  the `/v1` endpoint as experimental. Not a bug in our code, not a Boromir
  reasoning failure — a transport-layer gap in the OpenAI-compat shim
  specifically.
- **Root-cause fix (not just a workaround):** rewrote `OllamaProvider` to
  use the official `ollama` Python library against Ollama's NATIVE
  `/api/chat` endpoint, which is documented to handle tool calling
  correctly, instead of the experimental OpenAI-compat shim. This broke
  symmetry with `groq_provider.py` (which correctly still uses `openai`,
  since Groq's compat layer is stable) — acceptable, since `BaseProvider`
  means nothing outside this file cares which client library is used
  internally.
- **Second issue surfaced by the switch, fixed the same day:** Ollama's
  native API expects a genuinely different message shape than OpenAI's
  convention for the two message types involved in tool use —
  `tool_calls[].function.arguments` must be a real `dict` (not a JSON
  string), and tool-result messages need a `tool_name` field instead of
  OpenAI's `tool_call_id`. `core/agent.py` builds message history in
  OpenAI-canonical shape (correct, since that's what Groq needs
  unmodified) — so `OllamaProvider` now translates bidirectionally:
  `_try_parse_fallback_tool_call()` / output normalization for native→
  OpenAI-shape (going out to the agent), and the new
  `_translate_messages_for_native()` for OpenAI→native-shape (going in
  to Ollama). Both translations live entirely inside
  `providers/ollama_provider.py` — `core/agent.py` never needed to
  change.
- **Kept the JSON-in-content fallback parser** as defense-in-depth even
  after the native-API fix — confirmed inert (never fires) on a correctly-
  behaving response, so it costs nothing to keep and protects against any
  future model/template combination that still misbehaves.
  - **Interview angle:** this whole arc — symptom → root-cause diagnosis
    via evidence, not guesswork → fix at the correct architectural layer
    → a second latent issue surfaced immediately by the first fix → fixed
    the same way — is the strongest engineering story in the project so
    far. It also concretely demonstrates why `BaseProvider` exists: two
    rounds of provider-specific quirks were absorbed entirely inside one
    file, with zero changes required to `core/agent.py`, `core/llm_client.py`,
    or `tools/registry.py`.
- **Verified live, final clean run:** step 1 — native `tool_calls` fired
  correctly (no fallback needed), `read_file` executed for real. Step 2 —
  model reasoned over the actual file content and produced a fully
  grounded, accurate answer citing real variable names, real threshold
  semantics, and the actual sanity-check behavior from the file — not a
  generic guess. Latency dropped to 2.9s/8.4s (warm model) from the
  40s+ cold-start numbers seen earlier.

### Status
- [x] `config.py`
- [x] `providers/base.py`
- [x] `providers/groq_provider.py`
- [x] `providers/ollama_provider.py` (native API + bidirectional message translation + fallback parser)
- [x] `core/llm_client.py` (routing + circuit breaker + cache + logging)
- [x] `tools/reader.py`, `tools/search.py` (stub), `tools/registry.py`
- [x] `core/agent.py` — verified working end-to-end, fully clean run
- [ ] Indexer (Rust file walker, tree-sitter, GPU embeddings, ChromaDB) — next
- [ ] Basic CLI (`cli.py`)

---

## 2026-07-08 — Indexer: Rust file walker (`indexer_core/`)

### Rust toolchain
- Rust was not previously installed on this machine. Installed via `rustup`
  (winget), verified with `rustc --version` / `cargo --version` before
  writing any code.

### `indexer_core/` scaffolded
- `Cargo.toml`: `ignore` + `rayon` (parallel, `.gitignore`-aware walking —
  same crate ripgrep uses), `serde`/`serde_json` (JSON output), `sha2` +
  `hex` (file hashing).
- `src/main.rs`: `WalkBuilder::build_parallel()` walks the repo; hashing
  happens *inside* the parallel visitor closure, so both traversal and
  SHA-256 hashing are parallelized, not just the directory walk. Streams
  each file through the hasher in 8KB chunks rather than loading whole
  files into memory. Extension → language-label mapping for the future
  tree-sitter chunker. Unreadable files are skipped, not fatal — one
  bad file doesn't kill the whole walk. Output: single JSON array on
  stdout matching the Section 8 contract exactly (`path`, `size`,
  `language`, `hash`, `modified`).
- Added `indexer_core/target/` and `logs/` to `.gitignore` (target/ for
  Cargo build artifacts; logs/ was referenced as "gitignored" in an
  earlier entry but had never actually been added to the file).

### Real bug #1: `.gitignore` silently ignored — `require_git` default
- **First run:** `indexer_core.exe ..` dumped hundreds of files from
  `venv/Lib/site-packages` straight to stdout — exactly the directory
  `.gitignore` excludes.
- **Diagnosed, not guessed:** the `ignore` crate's `WalkBuilder` only
  applies `.gitignore`/`.git/info/exclude`/global-gitignore rules when the
  walked path is inside an actual git repository (`require_git` defaults
  to `true`). `git init` had never been run in `olorin/`, so every
  `.gitignore` rule was silently inert — the walker fell back to "ignore
  nothing," full stop.
- **Fix:** added `.require_git(false)` to the `WalkBuilder` call in
  `main.rs`, with an inline comment explaining why — Olorin's ignore scope
  is defined by the `.gitignore` file as config, not by git repo status,
  so this shouldn't depend on whether `git init` has been run.

### Real bug #2: `.gitignore` written with literal quote characters
- **Second run, same symptom:** after the `require_git(false)` fix and a
  rebuild, `venv/` was *still* being walked in full. Identical symptom to
  bug #1, different root cause — systematic re-diagnosis required rather
  than assuming the first fix was incomplete.
- **Root cause:** `.gitignore`'s actual on-disk content was:
  ```
  ".env"
  "__pycache__/"
  "*.pyc"
  "venv/"
  ...
  ```
  Every pattern line had literal double-quote characters wrapped around
  it — almost certainly from an earlier PowerShell command that quoted
  each pattern for the shell, but wrote the quotes into the file itself
  instead of stripping them. Git's `.gitignore` syntax does not treat
  leading/trailing `"` as delimiters; the actual pattern being matched
  was the literal string `"venv/"` (quotes included), which matches no
  real path, so every rule in the file was silently a no-op.
- **Fix:** rewrote `.gitignore` with the same patterns, quote characters
  removed.
- **Interview angle:** two bugs, identical external symptom (`venv/`
  fully walked), completely different root causes — one a crate default
  behavior, one a malformed config file. Good illustration of why you
  verify the actual fix resolved the actual symptom instead of assuming
  the first plausible-sounding cause was the only one.

### Verified live, clean run
- Final `indexer_core.exe ..` output: 20 real project files (`config.py`,
  `core/`, `providers/`, `tools/`, `memory/`, `indexer_core/{Cargo.toml,
  Cargo.lock, src/main.rs}`, both markdown docs). Zero `venv/`,
  `__pycache__/`, `logs/`, `*.db`, or `indexer_core/target/` entries —
  every exclusion category confirmed working, not just spot-checked.
- **Benchmarked** (`Measure-Command`): **301ms** for the full walk —
  comfortably under the sub-1-second target claimed in
  `OLORIN_PROJECT.md` Section 13. This is a real number on a modest-sized
  repo (~20 tracked files plus the full `venv/` tree being traversed and
  filtered), not a guess.

### Status
- [x] Rust file walker (`indexer_core/`) — parallel walk + parallel
  hashing, `.gitignore`-aware, JSON output matching Section 8 contract,
  benchmarked at 301ms
- [x] tree-sitter chunker (`indexer/chunker.py`) — verified live against
  `core/agent.py`: correctly split into function/class chunks with
  accurate line ranges, deliberate class/method overlap confirmed
  working as designed
- [x] GPU embeddings (`indexer/embedder.py`) — verified live: `Device:
  cuda` confirmed (not a silent CPU fallback), 384-dim output matches
  bge-small-en-v1.5's known dimensionality, query embed at 20ms once
  warm. BGE asymmetric query/passage instruction split implemented as
  two separate functions (embed_chunks / embed_query) rather than a
  flag, so the two can't be accidentally swapped.
- [x] ChromaDB store (`indexer/store.py`) — verified live, full
  pipeline end to end: chunked + embedded + stored `core/agent.py` for
  real, then queried "how does the agent loop handle tool calls" and
  got back `Agent` (distance 0.51) and `run` (distance 0.53) ranked
  above the less-relevant `build_system_prompt` (0.81) — correct
  semantic ranking, not just "query didn't error." Chroma data
  deliberately stored outside any indexed repo, at a single global
  `~/.olorin/chroma_db`, with one collection per repo keyed by a hash
  of its resolved path — indexing someone else's repo never writes
  into their working tree.
- [ ] Swap `search_codebase` stub in `tools/search.py` for the real
  `indexer/store.py` query — next
- [ ] Basic CLI (`cli.py`) — needs an `index <path>` command that ties
  walker → chunker → embedder → store together as one pipeline
  (including the hash-based skip-unchanged-files logic that
  `get_indexed_file_hash()` was built to support)

**Indexer (Section 8) is now fully built and individually verified at
every stage.** What's left is wiring, not new subsystems: a `cli.py`
index command to orchestrate walker→chunker→embedder→store as one
pass over a repo, and pointing `tools/search.py`'s `search_codebase` at
`indexer/store.py`'s `query()` instead of its stub error message.

---

## 2026-07-09 — V1 complete: `search_codebase` live, `cli.py` built, two real bugs found and fixed

### `tools/search.py` — swapped the stub for a real ChromaDB call
- Thin wrapper: `get_collection(repo_root)` → `collection.count() == 0` check
  (repo not indexed yet — explicit, honest error, same philosophy as the old
  stub) → `indexer/store.py`'s `query()` → reshaped into `{chunks: [...]}`.
- `tools/registry.py`'s `execute_tool()` now forwards `repo_root` into
  `search_codebase`, matching the same injection pattern already used for
  `read_file`/`list_files` — never trusted from model-supplied arguments.
- Updated the tool schema description and `core/agent.py`'s system prompt,
  both of which still described the tool as unimplemented.

### `cli.py` — the actual V1 entry point, built
- `index <path>`: walker (subprocess) → per-file hash comparison against
  `get_indexed_file_hash()` (skip unchanged, `delete_file()` + full re-chunk
  on changed) → `chunk_file()` → `embed_chunks()` → `upsert_chunks()`.
  Reports skipped/reindexed/total-chunks + timing.
- `ask "<query>" [--path] [--provider] [--force-local]`: thin wrapper around
  `Agent.run()`.
- Deferred the torch/chromadb/sentence-transformers imports to inside
  `index()` only, so `ask` and `--help` don't pay that import cost.

### Real bug found + fixed: absolute paths in indexer output
- **Symptom:** first `python cli.py index .` run indexed real files, but
  every chunk's `file` metadata was a full absolute path
  (`C:\Users\Aryavart\olorin\tools\__init__.py`) instead of the
  Section-8-contract relative path (`tools/__init__.py`).
- **Root cause:** `cli.py` calls `os.path.abspath(path)` before invoking the
  Rust walker, and `main.rs` had never actually enforced the relative-path
  half of its own documented JSON contract — it just did
  `path.to_string_lossy()` verbatim. This had gone unnoticed because the
  earlier walker-only smoke test happened to pass a relative arg (`..`).
- **Fix, at the correct layer (Rust, not a Python-side path-stripping
  patch):** canonicalize both `repo_path` and each walked file path,
  `strip_prefix()` the repo root off, forward-slash-normalize for
  cross-platform stability. First compile attempt failed (`E0507`: can't
  move a captured `PathBuf` into an `FnMut` closure that's invoked once per
  worker thread) — fixed by wrapping `repo_root_canon` in an `Arc` and
  cloning it per-thread, same pattern already used for the shared `results`
  vec.
- **Why this matters beyond cosmetics:** absolute paths would have silently
  broken `get_indexed_file_hash()`'s file-identity comparisons across
  machines/checkouts, and skewed `tools/reader.py`'s `read_file` semantics
  (it happened to still work by coincidence — `os.path.join` treats an
  absolute second argument as the whole path — but that's not a design
  anyone should rely on).

### Real bug found + fixed: fenced-JSON tool-call not recognized
- **Symptom:** live `ask` run against a real indexed repo — Boromir needed
  `search_codebase`, but the loop returned a "final answer" that was
  literally a ` ```json ... ``` ` fenced block containing the tool-call
  JSON. `finish_reason` came back `"stop"`.
- **Diagnosis:** the same underlying transport quirk logged 2026-07-06
  (model emits tool-call JSON into plain content instead of the structured
  field), but a new shape of it — the existing
  `_try_parse_fallback_tool_call()` guard only matched bare `{...}`, so a
  markdown-fenced payload slipped past it silently.
- **Fix:** `_try_parse_fallback_tool_call()` now strips a leading/trailing
  ` ``` ` or ` ```json ` fence before the existing brace-matching check
  runs. Fixed entirely inside `providers/ollama_provider.py` — no changes
  to `core/agent.py` or `tools/registry.py`, consistent with the
  dependency-inversion principle already established for this exact class
  of bug.
- **Interview angle:** a good "root cause wasn't fully closed the first
  time" story — the original fix (native API + bidirectional translation)
  was correct and necessary, but the defense-in-depth fallback parser it
  left behind had a narrower net than the actual space of ways a small
  model can misformat structured output. A defensive fallback is only as
  good as the shapes it was tested against.

### Verified live, clean end-to-end run — V1 demo target achieved
- `python cli.py index .`: 25 files walked (0.14s), 74 chunks embedded and
  stored, 10.23s total, correct relative paths in every chunk's metadata.
- `python cli.py ask "how does the agent loop handle tool calls?"`: step 1
  — Boromir correctly called `search_codebase` with real arguments; step 2
  — reasoned over real retrieved chunks from `core/agent.py` and gave a
  fully grounded 8-point answer.
- **Fact-checked the answer against the actual source line by line** —
  every claim (message construction, `MAX_AGENT_STEPS` loop, exact
  `llm.chat()` argument names, `finish_reason` branching, tool-call message
  ordering, `execute_tool()` call shape, tool-result message shape,
  unrecognized-`finish_reason` fallback) matched the real code. No
  hallucinated details.
- **This is the V1 demo target from Section 16, achieved for real** (the
  specific query differs from the original "what does the auth module do?"
  example since this repo has no auth module — an equivalent grounded
  query against real indexed code was used instead).

### Status — V1 is complete
- [x] `config.py`
- [x] `providers/base.py`
- [x] `providers/groq_provider.py`
- [x] `providers/ollama_provider.py` (native API + bidirectional translation
  + fallback parser, now fence-aware)
- [x] `core/llm_client.py` (routing + circuit breaker + cache + logging)
- [x] `tools/reader.py`, `tools/search.py` (live), `tools/registry.py`
- [x] `core/agent.py`
- [x] Full indexer pipeline (`indexer_core/`, `indexer/chunker.py`,
  `indexer/embedder.py`, `indexer/store.py`)
- [x] `cli.py` (`index` + `ask`)
- [x] **V1 demo target verified live and fact-checked**

**Five real bugs found and fixed at the correct architectural layer across
this project so far** (full stories in Section 13 and this journal): the
two-part Ollama transport/schema quirk, the `require_git` default, the
quoted-`.gitignore` bug, the absolute-path walker bug, and the
fenced-JSON fallback-parser gap.

### Next direction (per Aryavart, 2026-07-09): reduce cold-start/reload overhead
- Every `python cli.py` invocation currently reloads `bge-small-en-v1.5`
  from disk into VRAM from scratch (~1-2s), even for a single query embed
  — because `cli.py` is a fresh process every call, `indexer/embedder.py`'s
  module-level `_model` cache buys nothing across invocations, only within
  one.
- Flagged as the next priority, ahead of V2 memory features: investigate a
  persistent local process (daemon-style — related to V4's system-tray
  plan but pulled earlier and scoped narrower: just "keep the embedding
  model warm across CLI calls," not the full tray/hotkey/watcher feature
  set) so `ask` doesn't pay a reload tax on every call.
- Not yet designed — next session's focus.

---

## 2026-07-09 (continued) — V1.5 profiling instrumentation, two profiling bugs, Boromir model swap

### `core/profiling.py` built — the 9-stage instrumentation plan from earlier today
- Shared `span()` context manager + `record()` for pre-measured durations,
  wired into `cli.py` (`agent_import_chain`, `total_request`),
  `indexer/embedder.py` (`embedder_init`, `query_embedding`),
  `indexer/store.py` (`chroma_init`, `vector_search`), and
  `providers/ollama_provider.py` (Ollama's own server-reported
  `load_duration`/`total_duration`, more accurate than wall-clock
  guessing for splitting model-load from real inference).
- `cli.py ask --profile` prints the full breakdown after the answer.

### Real bug found + fixed: profiling report double/triple/quadruple-counted nested spans
- **Symptom:** first live `--profile` run reported `sum of recorded spans`
  far exceeding the actual wall clock (117350ms summed vs. 58024ms wall
  clock), producing a nonsensical *negative* "unaccounted" number.
- **Root cause:** `report()` naively summed every span, but most spans are
  nested, not sequential — `total_request` already contains
  `chroma_init`/`embedder_init`/`query_embedding`/`vector_search`/every
  Ollama call, and each Ollama call was independently recorded four
  overlapping ways (`total`, `model_load`, `inference`, `wall_clock`).
  Summing all of that flat quadruple- and quintuple-counted huge chunks
  of time.
- **Fix:** added a `top_level: bool` flag to `span()`/`record()`. Only
  `agent_import_chain` and `total_request` (the two genuinely sequential,
  non-overlapping phases in `cli.py`'s `ask()`) are marked top-level and
  summed for the accounting total; everything else still prints for the
  detailed nested breakdown but is excluded from the sum. Ollama
  instrumentation rewritten to record a true partition — `model_load` +
  `inference` + `client_overhead` now sum *exactly* to wall-clock latency,
  no overlap.
- **Verified:** re-run after the fix produced `unaccounted` values of
  ~78-95ms across three runs — small, sane, and consistent, versus the
  original negative number.

### Real finding: HF Hub warning was a genuine, fixable cost, not noise
- `sentence-transformers` pings Hugging Face Hub to check for a newer
  model revision on every load, even with `bge-small-en-v1.5` fully
  cached locally — a live network round-trip sitting inside
  `embedder_init`, correlating exactly with a live 5847.9ms measurement
  (vs. the "~1-2s" the code's own comments expected).
- **Fix:** `os.environ.setdefault("HF_HUB_OFFLINE", "1")` /
  `TRANSFORMERS_OFFLINE` set in `indexer/embedder.py`, before the
  `sentence_transformers` import (some of its config is read at import
  time). Real fix, not a workaround — the model's already downloaded, so
  the network check was never buying anything.
- **Verified:** `embedder_init` dropped from 5847.9ms → 983.2ms (83% cut)
  on the next run, no more HF warning in the logs.

### What the profiling data actually revealed: import cost, not reload cost, dominates
- `agent_import_chain` (the cascade: `core.agent` → `core.llm_client` →
  providers → `tools.registry` → `tools.search` →
  `indexer.store`/`indexer.embedder`, i.e. torch/chromadb/
  sentence-transformers) measured 8546.5ms and 6807.1ms across two
  consecutive runs — 66%+ of total wall clock on a single-step query with
  no tool call at all. `embedder_init`/`chroma_init` (now that the HF fix
  is in) are comparatively small once the module-level singleton caching
  kicks in.
- **This directly answers the original V1.5 decision gate
  (OLORIN_PROJECT.md Section 11):** the module-level embedder/Chroma
  caching already does its job *within* one process — the dominant cost
  is Python import overhead paid fresh on **every process launch**, not
  reload-within-a-session. This is a real, measured signal (not an
  assumption) that a persistent process is justified. Daemon design is
  next session's concrete next step, no longer speculative.

### Real bug found (Boromir, not this instrumentation): a third tool-call malformation shape
- Same live profiling session surfaced Boromir (still qwen2.5-coder at
  this point) producing two more bad outputs on the *identical* repeated
  query: one flat non-answer with no tool-call attempt at all, and one
  prose-prefixed bare-JSON tool call (a sentence of reasoning followed by
  unfenced JSON) — a third distinct shape after bare-JSON (2026-07-06)
  and markdown-fenced JSON (earlier today), each of which required its
  own special case in `_try_parse_fallback_tool_call()`.
- **Assessment logged, not yet fixed via parser patching:** three shapes
  in one session is a pattern, not noise — chasing each new malformation
  individually doesn't guarantee the fourth is caught. This assessment
  directly motivated the model swap below instead of a fourth parser
  special case.

### Decision: Boromir's base swapped from qwen2.5-coder to qwen3:8b
- Verified via current web search (not assumed from training data, which
  would be stale for a model landscape moving this fast) that Qwen3 has
  native tool-calling support built into its chat template, and Qwen3 8B
  is repeatedly named the most reliable small local tool-calling model
  across independent 2026 benchmarks — a structural difference from
  qwen2.5-coder, which serializes tool descriptions into the prompt and
  the model decides for itself whether to emit structured output.
- Same VRAM class as the model it replaces (~5.2GB vs. 4.7GB, both
  comfortable in the 8GB budget), Apache 2.0 licensed, with a genuine
  hybrid thinking/non-thinking toggle.
- **Trade-off stated explicitly:** qwen3:8b is general-purpose, not
  code-specialized like qwen2.5-coder was. Accepted because Boromir's
  actual V1 job — calling `read_file`/`list_files`/`search_codebase` and
  reasoning over retrieved code — is comprehension and tool
  orchestration, not code generation, which is exactly the profile
  tool-call reliability should be weighted for.
- **Alternative considered and set aside:** Llama-3-Groq-8B-Tool-Use
  scored higher on raw tool-calling benchmarks (89% BFCL) and has a fun
  narrative tie-in given Olorin already uses Groq, but it's an older 2024
  specialist fine-tune with unconfirmed active maintenance in the Ollama
  library today. Qwen3:8b is the more current, broadly-supported pick.

### Discovered post-swap: Faramir and Boromir now share a base architecture
- `ollama show` on both models reports `architecture: qwen3` — not a bug.
  DeepSeek's `deepseek-r1:8b` distill (Faramir) is R1's reasoning traces
  trained onto a Qwen3 base, so the shared label reflects real ancestry.
- Confirmed genuinely distinct via `ollama show`: different license
  (MIT/DeepSeek vs. Apache 2.0/Qwen), different chat-template stop tokens
  (DeepSeek/R1 convention vs. ChatML — prompt formatting isn't
  interchangeable despite the shared architecture label), different
  reported max context (131072 vs. 40960).
- **Real bug found and fixed as a direct consequence:** `OllamaProvider`
  had no `think` control at all — both personas were constructed
  identically. Now that Boromir has a genuine thinking mode (qwen2.5-coder
  never did), it would have silently inherited Ollama/Qwen3's default
  thinking behavior, working against Boromir's whole design intent (fast,
  task-focused, no deliberation). Added `think: bool | None` to
  `OllamaProvider.__init__()`; `core/llm_client.py` now constructs Boromir
  with `think=False` and Faramir with `think=True` — explicit per-persona
  control instead of a shared default neither model asked for.

### Status
- [x] `core/profiling.py` — 9-stage instrumentation, two real profiling
  bugs found and fixed, verified live
- [x] `HF_HUB_OFFLINE` fix — verified live, 83% cut to `embedder_init`
- [x] V1.5 decision gate reached: import overhead, not reload overhead,
  is the dominant cost — daemon design is justified and is next
  session's concrete task
- [x] Boromir base swapped to `qwen3:8b`, `think` control added and wired
  per-persona
- [ ] Daemon architecture design — still next session's task
- [x] Verified: 3/3 consecutive `ask` runs post-swap produced clean,
  structured `tool_calls` (visible directly in the agent log, no
  fallback-parser trigger, no malformed JSON in any shape). All three
  final answers fact-checked against `core/agent.py`'s real source —
  accurate, including a system-prompt detail ("includes the repository
  root") that was specifically re-verified against `build_system_prompt()`.

---

## 2026-07-10 — think override extension point, think:false confirmed honored, daemon deliberately deferred

### Extension point: per-call `think_override` threaded through the whole stack
- Motivation: keep the possibility open of putting Boromir into thinking
  mode for a specific heavy task in a later version, without having to
  restructure the provider/routing layers when that need actually shows
  up. Added `think_override: bool | None = None` to `BaseProvider.chat()`'s
  abstract signature, `GroqProvider.chat()` (accepted, ignored — Groq has
  no thinking-mode concept), `OllamaProvider.chat()` (real effect: falls
  back to the instance's standing `think` default when not given), and
  `LLMClient.chat()`/`_route()` (passed straight through to whichever
  provider ends up handling the request). Nothing in V1 sets this yet —
  it's a deliberate no-op extension point, not a feature.

### Diagnostic added: directly confirm whether `think` is honored
- Added a log line in `OllamaProvider.chat()` reading Ollama's own
  `message.thinking` field and reporting
  `think_requested=<bool> thinking_produced=<yes/no> thinking_chars=<n>`
  on every call — replaces inferring thinking behavior indirectly from
  latency with a direct, per-call confirmation.
- **Verified live:** every one of Boromir's calls across four full `ask`
  runs logged `think_requested=False thinking_produced=no
  thinking_chars=0` — `think:false` is genuinely honored, not silently
  ignored.

### Re-measured with realistic tool-using queries — findings that complicated, not just confirmed, the daemon case
- On short single-step queries (2026-07-09's data), `agent_import_chain`
  was 66%+ of total wall clock — seemingly a clear daemon justification.
- On longer, more realistic tool-using queries (this session), `total_request`
  grew substantially because step 2's `ollama_inference` ballooned to
  13-27 seconds on multi-paragraph structured answers — real generation
  time, not overhead. Against that, `agent_import_chain`'s share of total
  wall clock dropped to 17-30%. **A daemon eliminates import overhead but
  cannot touch inference time, which is the actual largest cost on
  realistic queries.**
- Side observation, flagged as a hypothesis not a confirmed fact:
  `ollama_client_overhead` was consistently ~2050ms on every step-1 call
  and ~3-6ms on every step-2 call, across all four runs — possibly a
  one-time per-session cost (grammar/template compilation for
  constrained/tool-schema output) that a daemon would only pay once per
  model load instead of once per process. Not chased further this session.

### Decision: daemon justified by the numbers, deliberately DEFERRED
- Discussed directly with Aryavart, who raised the right question: is it
  wise to build persistent-process infrastructure now, before V1 has even
  finished evolving, when this is also new territory (no prior daemon/IPC
  experience on this project)?
- **Decision: not now.** A daemon is not a one-time build — it's ongoing
  complexity (process lifecycle, crash recovery, keeping in-memory state
  consistent when a repo gets re-indexed out from under a live daemon,
  concurrent-request handling) layered on top of a feature set that's
  about to change significantly in V2 (episodic memory, OLORIN.md
  loading) and beyond. Building it now risks designing it around V1's
  shape and having to redesign it once V2 changes what actually needs to
  stay warm. The measured cost (~6-10s import tax per call) is real but
  bounded — nothing in V1 is unusable because of it — making it exactly
  the kind of cost worth deferring in favor of features that expand what
  Olorin can do. Revisit post-V2/V3, once the feature set has settled
  enough that a daemon's shape wouldn't need a redesign the moment
  something new is built on top of it.
- **Interview angle:** recognizing when *not* to build infrastructure is
  as much a signal of engineering judgment as recognizing when to build
  it — this decision is logged with the same rigor as every "yes, build
  this" decision in this project, not glossed over as "we didn't get to
  it."

### Status
- [x] `think_override` extension point wired end-to-end (base → Groq →
  Ollama → LLMClient), unused by anything yet, ready for a future heavy-
  task routing decision
- [x] `think:false` confirmed genuinely honored via direct diagnostic,
  not inferred
- [x] V1.5 fully closed: measured, decided, documented — daemon
  deliberately deferred, not built
- [ ] Next: V2 features (episodic memory, OLORIN.md, journal automation,
  entity memory) — per Section 11's V2 tier

