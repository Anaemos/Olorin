# Project Olorin — Complete Design Document
> *"He was wiser than Gandalf, and more subtle in perceiving what was amiss."*
> — Tolkien, on Olórin in Valinor

---

## 1. Identity & Concept

**Olorin** is a local-first engineering assistant with persistent project memory.  
It indexes your codebases, reads your documents, remembers your decisions, and lets you query all of it through natural conversation — fully offline when needed, frontier-grade when online.

Named after Gandalf's true Valinórean name — the scholar form of Gandalf, who spent his time observing and learning quietly before carrying a staff. The version that *knows*, not the version that performs.

**Not a chatbot wrapper. Not another RAG project.**  
A system with real architectural decisions at every level: provider abstraction, AST-aware indexing, post-retrieval routing, persistent memory, and a Rust-accelerated pipeline.

---

## 2. The Lore — Agent Roster

| Agent | Model | Role | Lore |
|---|---|---|---|
| **Olorin** | Groq `llama-3.3-70b-versatile` | Frontier brain, orchestrator, online | Gandalf's true form — ancient, all-knowing, dispatches the others |
| **Faramir** | Ollama `deepseek-r1:8b` | Reasoning, scholar, planning, knowledge | The studious brother — reads books, thinks before acting, strategic |
| **Boromir** | Ollama `qwen3:8b` (`think: false`) | Code, tools, execution, fast responses | The warrior — direct, task-focused, gets things done |

**Note on shared lineage (added 2026-07-09):** Faramir and Boromir both
report `architecture: qwen3` via `ollama show` — not a mistake. DeepSeek's
`deepseek-r1:8b` distill is R1's reasoning traces trained onto a Qwen3
base, so the two share ancestry. They remain meaningfully distinct models,
not the same weights twice: different license (MIT/DeepSeek vs. Apache
2.0/Qwen), different chat-template stop tokens (DeepSeek/R1 convention vs.
ChatML), different reported max context (131072 vs. 40960), and — the
functionally important one — Boromir runs with `think: false` (fast,
task-focused execution, per its role above) while Faramir runs with
`think: true` (reasoning is the point). See `providers/ollama_provider.py`
and `core/llm_client.py` for where that's actually enforced.

**Routing logic:**
- Complex reasoning, architecture, planning, multi-step analysis → **Olorin (Groq)**
- General reasoning, comparison, knowledge questions → **Faramir (local)**
- Code understanding, tool calls, file reads, refactoring → **Boromir (local)**

**Key rule:** Only one local model runs at a time. Ollama swaps between Faramir and Boromir automatically (~5-10 second swap cost). Never run both simultaneously.

---

## 3. Hardware Profile

| Component | Spec |
|---|---|
| GPU | NVIDIA RTX 4060 Laptop GPU (8GB VRAM) |
| RAM | 16GB |
| CPU | AMD Ryzen 7 7840HS |
| OS | Windows |

**VRAM budget:**
- Boromir (qwen3:8b-q4_K_M): ~5.2GB
- Faramir (deepseek-r1:8b): ~5.2GB
- bge-small-en-v1.5 during GPU embedding: +0.3GB
- Peak usage: ~5.5GB — well within 8GB ceiling

**Storage estimate:**
- Boromir model: ~5.2GB
- Faramir model: ~5.2GB
- bge-small embedding model: ~130MB
- ChromaDB index (per repo): 50–200MB
- SQLite memory DB: 10–50MB
- Python deps + Rust binary: ~2–3GB
- **Total: ~13–14GB**

---

## 4. Ollama Model Setup

### Boromir
```bash
# Base model (switched from qwen2.5-coder 2026-07-09 — see
# ENGINEERING_JOURNAL.md and OLORIN_PROJECT.md Section 13 for why:
# qwen2.5-coder's tool-calling produced three distinct malformed-output
# shapes across one session; qwen3 has native tool-calling support)
ollama pull qwen3:8b

# Create Modelfile
ollama show qwen3:8b --modelfile > boromir.modelfile
# Add to modelfile after FROM line:
# PARAMETER num_ctx 16384
ollama create boromir -f boromir.modelfile
```
Note: `think: false` (fast, non-deliberative tool execution — see Section
2's lore table) is NOT set via the Modelfile — `think` is a per-request
API field, not a Modelfile `PARAMETER`. It's set in code, in
`core/llm_client.py`'s `OllamaProvider(config.BOROMIR_MODEL, think=False)`
construction.

### Faramir
```bash
ollama pull deepseek-r1:8b
ollama show deepseek-r1:8b --modelfile > faramir.modelfile
# Add to modelfile after FROM line:
# PARAMETER num_ctx 16384
ollama create faramir -f faramir.modelfile
```

### Verify
```bash
ollama list
# Should show: boromir:latest, faramir:latest
# Plus base models (keep them — olorin/boromir/faramir depend on them as FROM references)
```

**Important:** Do NOT delete base models. The named models (`boromir`, `faramir`) reference them via `FROM`. Deleting the base breaks the named model.

---

## 5. API Keys Required

| Provider | URL | Free Tier | Priority |
|---|---|---|---|
| **Groq** | console.groq.com | 1,000 RPD, 100K TPD (llama-3.3-70b) | V1 — implemented |
| **Cerebras** | cloud.cerebras.ai | Genuinely free, no card, ~1M tokens/day; **8,192-token context cap on the free tier**, model catalog churns (verify against docs periodically) | V2 — implemented 2026-07-13 |
| ~~Together AI~~ | api.together.xyz | **Retired** — the $25 signup credit was discontinued industry-side in July 2025; now requires a $5 minimum purchase. Confirmed via Together's own pricing docs, 2026-07-13. **Deliberately not implemented** — project stays free-tier-only, see Section 13 | Dropped |
| **Tavily** | tavily.com | Free 1,000 queries/month, recurring, no card, LLM-ready extracted text | V2 — implemented 2026-07-13 (`web_search` primary) |
| **Jina** | jina.ai | No card, ~1M free tokens per new key (one-time per key), LLM-ready extracted text; free tier is non-commercial (CC-BY-NC) | V2 — implemented 2026-07-13 (`web_search` secondary) |
| **Serper** | serper.dev | Free 2,500 credits, **one-time on signup, not monthly** (corrected 2026-07-13 — verify vendor terms before trusting aggregator claims), raw SERP snippets | V2 — implemented 2026-07-13 (`web_search` reserve) |
| **Exa** | exa.ai | $10 one-time credit (~1,400 searches), no card; the "1,000/month recurring" figure quoted elsewhere requires adding a payment method, declined here for the same reason as Together AI | V2 — implemented 2026-07-13 (`web_search` opt-in last resort) |

**Groq free tier reality for `llama-3.3-70b-versatile`:**
- 30 RPM, 1,000 requests/day, 12,000 TPM, 100,000 TPD
- ~25 substantial queries/day (at ~4,000 tokens/query)
- With smart local-first routing: effectively 6+ heavy sessions/day
- 429 errors handled by circuit breaker → cascade to Cerebras, then local

**Groq risk note:** Following NVIDIA's $20B deal (Dec 2025), ~90% of Groq engineering moved to NVIDIA. GroqCloud continues operating but long-term trajectory is uncertain. The provider abstraction in `LLMClient` means swapping providers is a one-line change — Cerebras (2026-07-13) is the concrete proof of that, not just a claim.

---

## 6. Project Structure

```
olorin/
├── core/
│   ├── __init__.py
│   ├── llm_client.py          # Provider abstraction + routing — Phase 1
│   └── agent.py               # Raw ReAct loop — Phase 3
├── providers/
│   ├── __init__.py
│   ├── base.py                # BaseProvider abstract interface
│   ├── groq_provider.py       # Groq (implemented, V1)
│   ├── ollama_provider.py     # Ollama (implemented, V1)
│   └── cerebras_provider.py   # Cerebras (implemented, V2, 2026-07-13) — second-tier cloud fallback
│   # together_provider.py deliberately not built — free tier retired, see Section 5/13
├── tools/
│   ├── __init__.py
│   ├── registry.py            # Tool definitions in OpenAI JSON schema
│   ├── search.py              # search_codebase tool
│   ├── history.py             # search_history tool — LIVE (V2)
│   ├── websearch.py           # web_search tool — LIVE (V2, 2026-07-13), Tavily/Jina/Serper/Exa cascade
│   ├── entities.py            # search_entities tool — LIVE (V2, 2026-07-16)
│   ├── importance.py          # file_importance tool — LIVE (V3, 2026-07-18)
│   ├── git_diff.py            # git_diff tool (V3, 2026-07-18) — unit-verified, pending live verification
│   └── reader.py              # read_file, list_files tools
├── indexer/
│   ├── __init__.py
│   ├── chunker.py             # tree-sitter AST chunking
│   ├── embedder.py            # sentence-transformers (GPU)
│   ├── store.py               # ChromaDB read/write
│   ├── imports.py             # import-statement extraction + resolution — LIVE (V3, 2026-07-18)
│   └── capsules.py            # Knowledge Capsule generation — LIVE (V2, pulled forward from V3)
├── indexer_core/              # Rust file-walker
│   ├── Cargo.toml
│   └── src/
│       └── main.rs
├── memory/
│   ├── __init__.py
│   ├── cache.py               # SQLite query cache
│   ├── history.py             # Episodic conversation memory — LIVE (V2)
│   ├── capsules.py            # Knowledge Capsule storage — LIVE (V2, pulled forward from V3)
│   ├── journal.py             # Engineering Journal automation — LIVE (V2, 2026-07-12)
│   ├── entities.py            # Entity memory (decisions, concepts) — LIVE (V2, 2026-07-16)
│   └── import_graph.py        # Import-graph storage ("why is this file important?") — LIVE (V3, 2026-07-18)
├── config.py                  # Keys, model names, paths, thresholds
├── cli.py                     # Entry point (typer-based) — index-on-demand (Section 8), LIVE (V2)
├── test_faramir_tools.py      # Isolated Faramir tool-calling reliability test (Section 9), 2026-07-13 — run before trusting her tool calls live
├── test_websearch_backends.py # Isolated web_search backend smoke test (Tavily/Jina/Serper/Exa), 2026-07-15
├── debug_entity_extraction.py # Standalone entity-extraction diagnostic (raw response, no parsing), 2026-07-16
├── requirements.txt
├── OLORIN.md                  # Per-repo config — LIVE (V2), loaded via core/agent.py's load_olorin_md()
└── README.md
```

---

## 7. LLMClient Architecture — V1 (Locked)

### Design principles
1. **Post-retrieval routing** — classify complexity AFTER seeing retrieved chunks, not before
2. **Local by default** — only escalate to cloud when complexity score exceeds threshold
3. **Circuit breaker** — on 429, disable provider for 5 minutes instead of retrying every request
4. **Full metadata caching** — cache answer + provider + latency + retrieved files + model + timestamp
5. **Extension points today, implementations when needed**

### Complexity scorer (post-retrieval)
```python
def score_complexity(query, retrieved_chunks):
    score = 0
    score += len(retrieved_chunks) * 2          # more chunks = harder
    score += sum(len(c) for c in retrieved_chunks) // 500  # token volume
    score += len(query.split()) // 10           # question length
    
    planning_words = ["design", "architecture", "compare", "tradeoffs",
                      "approach", "structure", "optimize", "review", "why"]
    score += sum(2 for w in planning_words if w in query.lower())
    
    # threshold is a config value, tunable without code changes
    return score  # if score > COMPLEXITY_THRESHOLD: use Groq
```

**Capsule-seeding fix (2026-07-14):** `retrieved_chunks` used to start
empty on step 1 of every single query, by design ("classify complexity
after seeing retrieved context, not query wording") — but the
unintended side effect was that step 1 *always* routed local, no matter
how rich or complex the repo actually was, since there was structurally
nothing for the scorer to see yet. Confirmed live the same day: a
capsule-answerable orientation question against this repo (7 modules by
then) never got a single chance at Groq/Cerebras, because it never made
it past step 1's always-zero score. Fix: `core/agent.py`'s `Agent.run()`
now seeds `retrieved_chunks` with the same Knowledge Capsule content
already being injected into the system prompt (Section 10) — capsule
text IS retrieved context, just retrieved from the capsule store instead
of a live `search_codebase` call. This does NOT reopen Section 13's
rejection of keyword-based difficulty classification: it's still
entirely content-driven (repo richness, not query wording), just content
that happens to already be in context rather than freshly fetched. See
Section 13 for the full account and why `COMPLEXITY_THRESHOLD=15` needs
re-tuning against live data now that step 1 can actually score non-zero.

### Provider cascade (V1)
```
Groq (llama-3.3-70b-versatile)
    ↓ 429 / timeout / offline / circuit open
Ollama local_model (boromir or faramir)
    ↓ always available
```

### Provider cascade (V2, LIVE as of 2026-07-13)
```
Groq → Cerebras → Ollama local
```
Together AI was the other originally-planned V2 tier but is deliberately
not implemented — its free signup credit was retired industry-side in
July 2025 (now requires a $5 minimum purchase), and this project stays
free-tier-only. See Section 13 for the full account. Cerebras is
optional and skipped cleanly in auto-routing if no `CEREBRAS_API_KEY` is
configured — `self.cerebras.is_available()` gates the attempt so a
missing key costs nothing beyond the check itself, no wasted network
call. Both Groq and Cerebras have independent circuit breakers (a Groq
429 says nothing about Cerebras's health or vice versa).

### Circuit breaker
```python
# Per provider:
groq_disabled_until = None  # datetime or None

# On 429:
groq_disabled_until = datetime.now() + timedelta(minutes=5)

# On each request:
if groq_disabled_until and datetime.now() < groq_disabled_until:
    skip_groq()  # go straight to next provider
```

### Cache key
```python
import hashlib
key = hashlib.sha256(
    f"{query}{context_hash}{model_name}".encode()
).hexdigest()
# TTL: 24 hours
# Invalidated if repo is re-indexed (new context_hash)
```

### Cache stored fields
```sql
CREATE TABLE query_cache (
    key TEXT PRIMARY KEY,
    query TEXT,
    answer TEXT,
    provider TEXT,
    model TEXT,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    retrieved_files TEXT,  -- JSON array
    context_hash TEXT,
    timestamp TEXT
);
```

### CLI flags
```bash
python cli.py ask "..." --provider groq
python cli.py ask "..." --provider cerebras
python cli.py ask "..." --provider ollama
python cli.py ask "..." --provider auto   # default
python cli.py ask "..." --force-local     # never call cloud
```

### Direct-address routing override (V2, implemented 2026-07-12, Cerebras added 2026-07-14)
`core/agent.py`'s `parse_direct_address()` lets a query name its backend
inline — `"Groq, ..."`, `"Cerebras, ..."`, `"Boromir, ..."`, `"Faramir,
..."`, `"Olorin ..."` (maps to Groq per Section 2's lore) — matched only
at the exact start of the query, mapping onto the same `force_provider`
strings the `--provider` flag already uses. An explicit
`--provider`/`--force-local` flag always takes precedence over an
in-text address. Explicitly NOT the keyword-based difficulty
classification this section already rejects elsewhere — it's a
deterministic, narrow user override functionally identical to the CLI
flag, just expressed inline; nothing about routing is being inferred.
See Section 13 for the full design rationale, the two real bugs this
feature originally surfaced, and bug #14 (Cerebras was left out of this
parser for a full day after `--provider cerebras` shipped).

### Forced-provider fallback (bug #10, fixed 2026-07-12)
`_route()`'s forced-`groq` branch now wraps its call in the same
`try/except ProviderError` pattern auto-mode has always had, falling back
to local on any Groq failure rather than crashing. This gap predates
direct-address routing (the `--provider groq` flag had the same hole) —
it just went unnoticed until forcing Groq became a natural, frequent
thing to do. See Section 13 for the two genuinely different Groq
failures this was verified against.

### BaseProvider interface
```python
from abc import ABC, abstractmethod

class BaseProvider(ABC):
    @abstractmethod
    def chat(self, messages: list, tools: list = None) -> ProviderResponse:
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        pass

class ProviderResponse:
    content: str
    tool_calls: list
    finish_reason: str  # "stop" | "tool_calls"
    input_tokens: int
    output_tokens: int
    latency_ms: int
    provider: str
    model: str
```

---

## 8. Indexing Pipeline

### Flow
```
Repo folder
    ↓
Rust binary (parallel file walking, .gitignore aware)    ← rayon + ignore crates
    ↓
Python chunker (tree-sitter AST — splits by function/class)
    ↓
sentence-transformers bge-small-en-v1.5 (GPU, CUDA)
    ↓
ChromaDB (file-based, no server, persists between sessions)
```

### Indexing: index-on-demand (V2, 2026-07-11)
Indexing is no longer purely a manual step. `_index_repo()` (`cli.py`)
houses the pipeline above and is called two ways: explicitly via
`python cli.py index <path>` (verbose, full output), and automatically by
`python cli.py ask` as a precondition before every query (silent unless
real work happens). The hash comparison above is what makes the automatic
path cheap: a repeat `ask` against an unchanged repo pays only a fast walk
+ hash comparison, not a re-chunk/re-embed/re-capsule cycle. First-time or
changed-file calls pay the real cost, communicated honestly ("first time
indexing—this may take a minute", or a one-line "repo changed since last
index" summary) rather than hidden. `--skip-index` bypasses the check
entirely for advanced/debugging use. See Section 13 for the full design
conversation (why this reframing, and the tradeoff it doesn't eliminate).

### Rust binary responsibilities
- Walk repo respecting `.gitignore` (using `ignore` crate — same as ripgrep)
- Parallel execution via `rayon`
- Output JSON array: `[{path, size, language, hash, modified}]`
- Called from Python via `subprocess`
- Hash-based: unchanged files return same hash → Python skips re-embedding

### Chunk metadata (per chunk)
```json
{
  "content": "def authenticate(user, pw):\n    ...",
  "type": "function",
  "name": "authenticate",
  "file": "src/auth.py",
  "language": "python",
  "lines": "12-28",
  "repo": "/path/to/repo"
}
```

### Chunk size ceiling (bugs #11 & #12, fixed 2026-07-12)
No chunk had a size cap through V1 and most of V2 — `chunker.py`'s
whole-file fallback (any language without a tree-sitter grammar,
notably markdown) could produce one arbitrarily large chunk. Its own
docstring flagged this and explicitly deferred the fix to the embedder
layer; never implemented until this project's own `OLORIN_PROJECT.md`
and `ENGINEERING_JOURNAL.md` grew large enough from this session's
documentation work to trigger a real Groq 413. `indexer/embedder.py`'s
`_split_oversized_chunks()` now splits any chunk over `MAX_CHUNK_CHARS`
(2000, roughly bge-small's own ~512-token limit) into sequential
sub-chunks anchored to real file line numbers (an early version used
line numbers relative to the chunk's own text, causing deterministic
Chroma ID collisions — bug #12, see Section 13). `store.py`'s
`upsert_chunks()` also gained a defense-in-depth ID-dedup guard so any
future collision degrades gracefully instead of aborting an entire
file's upsert. **Known limitation, not yet fixed:** the split is a blunt
line-budget cut, not markdown/structure-aware — can sever a topical
section mid-way and hurt retrieval precision on long docs. See Section
13's entry for a real example this caused.

### GPU embeddings
```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer(
    "BAAI/bge-small-en-v1.5",
    device="cuda"  # RTX 4060 — several times faster than CPU
)
```

### OLORIN.md (per-repo config) — LIVE (V2, implemented 2026-07-11)
Each repo can have an `OLORIN.md` file at its root. Loaded automatically
by `core/agent.py`'s `load_olorin_md()`, once per `Agent` instance.

Example:
```markdown
# OLORIN.md

This project uses FastAPI and Python 3.12.
Never suggest SQLAlchemy — we use raw asyncpg.
Always prefer asyncio patterns.
Auth is handled in src/middleware/auth.py.
Database connection is in src/db/connection.py.
```

Olorin prepends this to the system prompt automatically. Makes the agent feel like it "understands" the repo from the first message.

**Implementation notes:** honest `None` when the file is absent — this is
opt-in, not required config. Capped at 4,000 chars defensively (user-
authored config should be short, but a truncation is safer than an
unbounded read competing for context budget every step). **Deliberately
never stripped**, unlike Knowledge Capsules (Section 10) — this is
explicit user-authored instruction, not orientation help the model can
safely stop needing once it's grounded in retrieved code; dropping a
project rule mid-run would be a correctness problem, not just a missed
optimization. Threaded through both the step-1 and step-2+
`build_system_prompt()` calls in `Agent.run()`. Doesn't feed the
complexity scorer's `retrieved_chunks`, same reasoning as capsules — it's
standing baseline context, not query-specific retrieval. **Verified
live:** a rule correctly cited on a step-1 answer, and — the actual test
this design was built for — a different rule correctly cited on a step-2
answer, confirming persistence past the point where capsules get stripped.

---

## 9. Agent Loop (Raw ReAct)

```python
def run(self, user_query: str) -> str:
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": user_query}
    ]
    
    for step in range(MAX_STEPS):  # prevent infinite loops
        response = self.llm.chat(messages, tools=TOOL_REGISTRY)
        
        if response.finish_reason == "stop":
            return response.content  # final answer
            
        if response.finish_reason == "tool_calls":
            for tool_call in response.tool_calls:
                result = self.execute_tool(tool_call)
                messages.append({
                    "role": "tool",
                    "content": json.dumps(result),
                    "tool_call_id": tool_call.id
                })
            # loop continues — model reasons over tool results
    
    return "Max reasoning steps reached."
```

**No LangChain. No LangGraph. Every line is yours to explain.**

### V1 Tools
| Tool | Signature | What it does |
|---|---|---|
| `search_codebase` | `(query: str, n_results: int = 5)` | Semantic search over ChromaDB |
| `read_file` | `(path: str)` | Direct file read |
| `list_files` | `(pattern: str = "*", max_files: int = 200)` | List repo files by extension/pattern, capped (bug #9, Section 13) with `total_matched`/`truncated` fields |

### V2 Tools (implemented 2026-07-11)
| Tool | Signature | What it does |
|---|---|---|
| `search_history` | `(query: str, n_results: int = 5)` | Keyword-overlap search over past conversations logged for this repo (`memory/history.py`) — not semantic search, see Section 10 |
| `search_entities` | `(query: str, n_results: int = 5)` | Keyword-overlap search over entity memory — decisions and concepts extracted from past runs (`memory/entities.py`, added 2026-07-16, see Section 10) |

### `web_search` — LIVE (V2, implemented 2026-07-13)
| Tool | Signature | What it does |
|---|---|---|
| `web_search` | `(query: str, max_results: int = 5)` | Live web search, available to **any** backend (Groq, Cerebras, Boromir, Faramir) |

**Why this exists:** Local models (Faramir especially) hallucinate confidently on open-knowledge questions outside their training data or parametric memory — confirmed by direct testing (deepseek-r1:8b fabricated plausible-sounding but incorrect details about Faramir/Boromir's relationship in Tolkien's text when asked from memory with no retrieval context). Adding `web_search` as a standard tool closes that gap for **all backends equally** — not just Groq.

**Key clarification:** this is not a cost-saving lever against Groq usage. The complexity scorer (Section 7) routes on retrieval size / query complexity, which is orthogonal to whether a question needs live facts. Groq's `llama-3.3-70b-versatile` has no inherent internet access either — without this tool it's just as capable of confidently getting live facts wrong as Faramir is. Giving *all* backends the same tool means local models can match or beat an ungrounded Groq call on live-fact questions, and everything still degrades gracefully to trained-knowledge-only when offline.

**Implementation (`tools/websearch.py`):** four free-tier backends cascaded with the same dependency-inversion + graceful-fallback pattern already proven for LLM providers (Section 7) — Tavily primary (1,000/month, recurring, no card, LLM-ready), Jina secondary (no card, ~1M tokens one-time per key, LLM-ready, non-commercial license), Serper reserve (2,500 credits, **one-time on signup, not monthly** — corrected 2026-07-13, an earlier version of this doc had the wrong claim), Exa opt-in last resort ($10 one-time, no card, strongest semantic search quality of the four). All four keys optional and independently checked; none configured returns an honest `{"error": ...}` result steering the model back to trained knowledge instead of silently returning nothing. See Section 13 for the full design account, including why a self-hosted option (SearXNG) was deliberately not built instead.

**Reasoning-model tool calling — risk confirmed, and resolved by gating, not fixing (2026-07-13).** `deepseek-r1:8b` wraps output in `<think>...</think>` before its final answer, which Section 9 flagged since 2026-07-06 as a risk to tool-call parsing. `test_faramir_tools.py`, written 2026-07-13, finally tested this in isolation — and Faramir failed: `finish_reason` came back `"stop"`, `tool_calls` was empty, and her content was plain prose narrating about calling the tool ("Let me pull up the latest weather...") rather than emitting a structured call. Notably, this produced **no malformed JSON for the existing fallback parser to catch** — a categorically different failure mode than the JSON-in-content quirks that parser was built for (Section 13), so there was nothing to patch there. **Decision, confirmed with Aryavart: gate tools away from Faramir entirely** rather than chase a prompt or `think:false` fix — `core/agent.py`'s `Agent.run()` now passes `tools=None` whenever `force_provider == "faramir"`, and `build_system_prompt()`'s new `tools_available` flag swaps the tool-listing section for an explicit "you have no tools this turn" note so she doesn't narrate as if she's about to use one. This isn't a new restriction in practice — it matches how Faramir is already used everywhere else in the project (Knowledge Capsule generation and Engineering Journal synthesis are both single plain completions with no tools, never a ReAct loop).

---

## 10. Memory System

### Episodic memory (SQLite) — LIVE (V2, implemented 2026-07-11)
```sql
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    repo_path TEXT,
    user_query TEXT,
    agent_response TEXT,
    tools_used TEXT,      -- JSON array
    backend_used TEXT,    -- "groq" | "boromir" | "faramir"
    complexity_score INTEGER
);
```
Implemented in `memory/history.py` (schema, `log_conversation()`,
`get_recent()`, `search_history()`) exactly as specified above. Written
automatically by `core/agent.py`'s `Agent.run()` on every completed run
— including non-happy-path exits (max-steps-reached, unexpected
`finish_reason`), not just clean final answers — so a run that goes
wrong still leaves a record. Queryable two ways: `python cli.py history
[--search]` (human browsing) and the `search_history` agent tool
(Section 9's V2 Tools table, so the agent can answer "what did I figure
out about X before?"-style questions itself).

**Scope decision:** `search_history()` is keyword-overlap search (shared
keyword count between the query and each row's stored text), not
semantic/embedding search — a real vector-based history search would
need its own store design (second Chroma collection? reuse the code
collection with a type filter?), which is a bigger decision than V2's
first pass warrants. Revisit if keyword recall proves too weak in
practice.

**`complexity_score` caveat:** only populated when the post-retrieval
scorer actually ran — `None` for forced-provider calls (`--provider X`,
`--force-local`) and cache hits. See Section 13's entry on the
retrieved_chunks bug for why this column is now trustworthy in the first
place.

### Entity memory (SQLite) — LIVE (V2, designed and implemented 2026-07-16)
```sql
CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    repo_path TEXT,
    type TEXT,             -- "decision" | "concept"
    name TEXT,
    description TEXT,      -- latest known state, overwritten on update
    metadata TEXT,         -- JSON, reserved for future use
    last_updated TEXT
);
```
**Scope, resolved via a direct design conversation before any code was
written:** the original stub's open question was genuinely two different
features wearing one name — "object relations in a repo" (code
structure: imports, call graphs) vs. "the agent's relation to things"
(persistent, cross-session knowledge of named things). Settled on the
latter. Code structure already has its own, separate, already-scoped
home — V3's "why is this file important?" import-graph item (Section
11) — so building that here would just be the same item wearing a
different name. The purpose test that settled it: what question does
this answer that nothing else in the system already does?
- Cache → "have I solved this exact query before?"
- Capsules → "what does this module do?"
- `search_history` → "which past conversation mentioned X?"
- **Entity memory** → "what is X, what's its current status?" — a
  resolved, updateable fact, not a conversation to re-read

**Schema deviation from the original stub:** `repo_path` added (the
stub never had one — a real gap, since without it a decision about a
different repo entirely would show up in this repo's results) for
consistency with every other memory table. Type scope narrowed from
`"repo" | "file" | "person" | "concept"` to just `"decision" | "concept"`
— `"repo"`/`"file"` overlap with Capsules/OLORIN.md, `"person"` has no
clear population source yet for a solo project (can be added later
without a migration, since `type` is a plain TEXT column, not an enum).

**Population mechanism, the design conversation's central question:**
extraction runs after every completed `Agent.run()` that ends in a clean
`finish_reason=="stop"` (not on max-steps/error exits — those aren't a
real grounded conclusion worth extracting from), as one follow-up
completion using the SAME backend that just answered
(`force_provider=backend_used`) rather than a dedicated extraction
model. This reuses the routing decision the complexity scorer already
made (Section 7) instead of inventing a second gate, and avoids any
Ollama model-swap cost since that backend is already warm. **Faramir is
deliberately excluded:** `test_faramir_tools.py` already proved she
doesn't reliably emit structured output (narrates prose instead of
returning clean `tool_calls`, with no malformed JSON for a fallback
parser to even catch, Section 9) — asking her to emit a clean entities
JSON block after answering would hit the identical failure mode that
got her gated out of tools entirely, just relocated. Most exchanges
produce zero candidates — the extraction prompt says so explicitly
("most exchanges contain NEITHER"), and this is the expected, normal
result, not a failure.

**Commit gate:** only candidates the model itself rates `"medium"` or
`"high"` confidence get written; below that, discarded — no staging
table for "maybe" candidates, same YAGNI discipline as everywhere else
in this project.

**Dedup, and a real bug found + fixed the same day:** new candidates are
matched against existing entities via keyword overlap (same mechanism
`search_history` already uses), deliberately conservative — a
false-positive merge (silently overwriting one real thing's description
with a different real thing's) is worse than a false-negative (two
near-duplicate rows, still findable via `search_entities`). The first
implementation used one blended ratio over name+description combined,
with a high (0.6) threshold. **Live-verified miss:** two independent
extractions of the same real decision ("Daemon deferral" / "Daemon
deferral decision") shared near-identical names (keyword ratio 1.0) but
phrased their descriptions differently enough that the blended ratio
came out to 0.3 — well under the bar, so it inserted a duplicate instead
of updating. **Fix:** two independent signals instead of one blend —
name-only overlap (lower `_NAME_MATCH_THRESHOLD=0.5`, since a name is a
short, stable label) OR combined name+description overlap (higher
`_DEDUP_MATCH_THRESHOLD=0.6`, the original fallback for when names
differ but descriptions clearly match). A candidate matches if *either*
signal clears its own bar. Re-verified live: the same near-duplicate
pair correctly matched and updated after the fix (total entity count
stayed at 2, not 3).

**Retrieval:** `search_entities` agent tool (Section 9's V2 Tools table),
same keyword-overlap shape as `search_history`, plus `python cli.py
entities [--search]` for human browsing. Deliberately a tool, not a
system-prompt injection like Capsules — Capsules are small and bounded
(5-8 modules); entities accumulate indefinitely, so injecting all of
them would eventually reopen the exact context-budget problem Capsules
already solved by *not* doing that.

**A real, accepted tradeoff, not a bug:** `GroqProvider.chat()` never
sets `temperature`, so Groq's non-zero server default applies, and the
identical query+answer pair can legitimately return different
extraction results across separate calls — confirmed directly: a real
`ask` made an extraction call that committed nothing, and an isolated
replay of the exact same query/answer (`debug_entity_extraction.py`)
returned three high-confidence candidates. Raised directly: thread a
`temperature` parameter through `LLMClient` → all providers and use a
low value for extraction specifically (the standard practice for a
classification task), or accept the variance. **Decided: accept it** —
a missed extraction isn't data corruption, just an opportunity that
didn't get captured this run; the same decision tends to resurface and
get caught on a later pass, the same graceful-degradation posture this
project already applies to `search_history`'s keyword-recall limits and
Journal's LLM-call failures. Cheap, permanent observability was added
instead (`memory/entities.py` logs raw candidate counts and each
commit/update at INFO level, but only when non-zero, so the common
empty-result case doesn't spam the logs) — a future silent miss is now
diagnosable from normal logs, no special script required.

**Verified live, end-to-end, not just via the isolated diagnostic:**
`ask "why was the daemon deliberately deferred instead of built?"`
against the real repo produced 3 committed entities (1 decision, 2
concepts, all high confidence), correctly scoped to this repo's
`repo_path` (confirmed via `cli.py entities` showing only these 3, not
the earlier smoke-test rows under fake repo paths). The same run is
also the first live confirmation that `search_entities` actually gets
called by the agent in a real ReAct loop, not just exercised directly —
step 1 called it, got an honest "nothing remembered yet" (correct, since
this repo's entity table was empty before this run), and the loop
continued normally to `search_history` and a final answer.

### Repository Knowledge Capsules (SQLite) — LIVE (V2, pulled forward from V3, implemented 2026-07-11)
```sql
CREATE TABLE capsules (
    repo_path TEXT,
    module TEXT,
    summary TEXT,
    source_files TEXT,   -- JSON array of {"path": ..., "hash": ...}
    last_updated TEXT,
    PRIMARY KEY (repo_path, module)
);
```
Originally scoped for V3 ("pre-computed module summaries loaded before
retrieval") but pulled forward: discussed directly against entity
memory (this section, above) and judged the better-specified, higher-
payoff item to build with the time available, out of strict roadmap
order — same kind of judgment call as the V1.5 daemon deferral, just in
the opposite direction (building early instead of deferring).

**Granularity is module-level (one capsule per top-level repo
directory), not file-level** — file-level capsules don't scale with repo
size and duplicate what `search_codebase` already does at chunk
granularity; module-level stays a small, bounded set regardless of repo
size.

**Generation (`indexer/capsules.py`):** computed as part of `python cli.py
index`, one Faramir completion per module whose file-hash set changed
since its capsule was last generated (`memory/capsules.py`'s
`needs_regeneration()`, same hash-comparison philosophy as the per-file
reindex-skip logic in Section 8, applied at module granularity). Faramir,
not Boromir, generates capsules — Section 2's lore assigns Faramir to
general reasoning/synthesis, which "what does this module do" squarely
is; the known Faramir tool-calling risk (Section 9) doesn't apply since
generation makes no tool calls. Per-file and total character caps
(`indexer/capsules.py`'s `MAX_CHARS_PER_FILE`/`MAX_TOTAL_CHARS`) bound
the generation prompt itself; the synthesis prompt also asks for
"3-4 sentences, under 100 words" to bound the summary's own length.

**Delivery — injected into the system prompt, step 1 only, not
tool-called:** `core/agent.py`'s `build_system_prompt(repo_root,
include_capsules=True)` prepends every module's summary to the initial
system message. This is the actual differentiator from `search_codebase`:
the agent gets a repo map *before* deciding whether to call any tool at
all.

**Context-budget design (discussed directly, 2026-07-11):** Boromir/
Faramir run at 16K ctx, and the full `messages` list — including the
system prompt — is resent on every step of the ReAct loop (no persistent
server-side context between calls). `read_file` alone can return ~5,000
tokens in a single call, so capsule text left in place past step 1 would
compete with growing tool-result content for the same fixed budget,
worst exactly on the multi-step queries where the budget is already
tightest — and by step 2+ the model is reasoning over concrete retrieved
content, not trying to orient itself, so capsules stop earning their
keep at that point anyway. Resolved by rebuilding `messages[0]` with
`include_capsules=False` immediately after step 0's tool calls execute
(`Agent.run()`, guarded by `if step == 0` so it only fires once).
**Deliberately deferred:** per-query relevance filtering (only inject
capsules for modules that seem related to the question) — the real
answer for large repos, but solving a problem this repo's scale (5-6
modules) doesn't have yet. Revisit once a repo is big enough that even
step-1-only, length-capped capsules start crowding the budget.

**Verified live:** single-step, zero-tool-call answer to "what is this
project and how is it structured?" correctly synthesizing all seven
modules' roles from capsule text alone; a real multi-step run (`search_
codebase` → `read_file` → Groq) confirmed `messages[0]` gets rebuilt
cleanly without capsules on step 2+, with no malformed message history.
See `ENGINEERING_JOURNAL.md`'s 2026-07-11 (continued) entry for full
verification detail.

### Engineering Journal (auto-generated) — LIVE (V2, implemented 2026-07-12)
Automates the exact thing this project's own `ENGINEERING_JOURNAL.md` had
been doing by hand every session: turn a day's episodic memory into a
readable session summary. `python cli.py journal [--path] [--date]
[--no-llm]` fetches every conversation logged for a repo on a given date
(`memory/history.py`'s new `get_conversations_for_date()`) and writes:
```markdown
# 2026-07-05

## Worked on
- Olorin agent loop

## Queries
- "what does the auth module do?" -> found auth.py middleware chain
- "where are database connections handled?" -> src/db/connection.py

## Tools used
- search_codebase (4x), read_file (2x)

## Backend
- Groq (2 queries), Boromir (6 queries)
```

**No new write path.** Every field above already exists in the
`conversations` table (Section 10, above) by the time `Agent.run()`
finishes a run — journal generation is purely a read and a reshape, not
a new source of truth.

**Storage location, same reasoning as ChromaDB's (Section 8):** journals
are written to `~/.olorin/journal/<repo_hash>/YYYY-MM-DD.md`, never into
the indexed repo's own working tree — keyed by the identical
sha256-of-resolved-path scheme `indexer/store.py` uses for Chroma
collection names, reimplemented locally in `memory/journal.py` rather
than imported (no prior `memory/` → `indexer/` dependency existed, and
three lines isn't worth introducing one).

**Split into deterministic aggregation + optional LLM synthesis** — the
same two-part pattern established by Knowledge Capsules and reused
verbatim here (Section 13). "Queries" / "Tools used" / "Backend" are
pure aggregation over stored rows, no LLM, never fail. "Worked on" is
the one section that's a genuine theme rather than a restatement of raw
questions — generated via a single **Faramir** completion (`memory/
journal.py`'s `synthesize_worked_on()`), for the identical reason
Capsules use Faramir (prose synthesis, not tool orchestration — the
known `<think>`-block tool-calling risk doesn't apply since this call
makes no tool calls). `llm_client` is duck-typed, matching `indexer/
capsules.py`'s `generate_module_summary()` exactly, so `memory/` still
has no hard dependency on `core/`. On `llm_client=None` or any call
failure, falls back to a literal deduplicated query list rather than
blocking — a Groq/Ollama hiccup degrades the journal, it doesn't break it.

**Deliberately NOT auto-triggered on every `ask`**, unlike indexing.
Index-on-demand (Section 8) is justified by the hash-skip logic making a
repeat freshness check nearly free. Journal regeneration calls an LLM
for "Worked on" every time it runs, with no equivalent skip-if-unchanged
shortcut — folding that into every `ask` would tax the interactive path
for a document nobody's reading mid-session. `journal` stays a
deliberate, explicit, end-of-session command — recognizing what's cheap
enough to automate versus what isn't, the same category of judgment as
the daemon deferral (Section 13), not "automate everything just because
the mechanism now exists."

**Status: fully closed, 2026-07-12.** Implemented, wired in, and live-verified — both the no-LLM fallback path and the live Faramir synthesis path ran correctly, including confirming the per-repo storage-path separation. That same live run surfaced a real bug (#13: the "Worked on" synthesis hallucinated implementation work from underspecified queries) — fixed at the prompt layer, then **re-verified live**: a re-run against the same real session produced correctly-grounded bullets ("Investigated the use of Rust...", "Summarized the OLORIN_PROJECT.md documentation...") with zero fabricated implementation claims. Full story in `ENGINEERING_JOURNAL.md`'s 2026-07-12 (continued) entry.

---

## 11. Build Roadmap

### V1 — It works
- [x] `config.py` + `.env` setup
- [x] `BaseProvider` interface
- [x] `GroqProvider` (working — verified live against llama-3.3-70b-versatile)
- [x] `OllamaProvider` (working — verified live against both Boromir and Faramir)
- [x] `CerebrasProvider` (implemented 2026-07-13, second-tier cloud fallback — live verification pending, see Section 16)
- [ ] ~~`TogetherProvider`~~ — deliberately not built, free tier retired, see Section 5/13
- [x] `LLMClient` with post-retrieval scorer, circuit breaker, cache, logging — all four verified live end-to-end
- [x] Rust file walker (`indexer_core/`) — parallel walk + parallel hashing, `.gitignore`-aware, benchmarked at 301ms (well under the sub-1s target)
- [x] tree-sitter chunker (`indexer/chunker.py`) — verified live, correct AST-based function/class boundaries
- [x] GPU embeddings (`indexer/embedder.py`) — verified live on RTX 4060, 384-dim bge-small-en-v1.5 output confirmed
- [x] ChromaDB store (`indexer/store.py`) — verified live end-to-end including a real semantic query with correct ranking
- [x] Raw ReAct agent loop (`core/agent.py`) — working end-to-end,
  including a fix for an Ollama tool-calling transport quirk (see
  Section 13)
- [x] Tool registry + 3 tools (`tools/`) — `read_file`/`list_files`/
  `search_codebase` all fully working; `search_codebase` is now live
  against real ChromaDB retrieval (previously stubbed)
- [x] Basic CLI: `python cli.py index <path>` + `python cli.py ask "<query>"`

**V1 demo — ACHIEVED (2026-07-09):** `python cli.py ask "how does the agent
loop handle tool calls?"` returned a grounded, correct answer built from
real `search_codebase` retrieval — fact-checked line-by-line against
source, zero fabricated details. (Query differs from the original "what
does the auth module do?" example since this repo has no auth module; an
equivalent grounded query against real indexed code was used instead.)

### V1.5 — It's fast to invoke (next up)

**Approach (locked 2026-07-09): measurement before architecture.** Instrument
the current pipeline first, get real per-stage numbers, and let those
numbers decide whether a persistent daemon is justified — not the other
way around. First optimization to try, cheapest and lowest-risk: reuse the
embedder + Chroma client within a single process (no new infra). Only if
process-startup cost still dominates after that (because `cli.py` launches
a fresh interpreter per command) does a daemon get designed.

**Stages to instrument, each as its own timed span:**
1. Python interpreter startup
2. Module imports (torch/sentence-transformers/chromadb are the suspects)
3. Embedder initialization (`SentenceTransformer(...)` construction + CUDA move)
4. Chroma client initialization (`PersistentClient` + `get_or_create_collection`)
5. Query embedding (`embed_query()` call itself, once the model is warm)
6. Vector search (`collection.query()` call itself)
7. Ollama model load/swap into VRAM — **measured separately from inference**,
   since this is a one-time-per-model-per-session cost, not a per-request one
   (confirmed ~41-45s cold vs. 2.9-8.4s warm back on 2026-07-06 — conflating
   the two would hide which cost a daemon actually addresses)
8. Ollama inference latency once warm (first-token if streaming is ever
   added, otherwise full response time as currently measured)
9. Total end-to-end request time (wall clock, `cli.py ask` invocation to
   printed answer)

**Deliverable:** a small instrumentation utility (timed-span context manager
or decorator) wired into `cli.py`/`core/agent.py`/`indexer/embedder.py`/
`indexer/store.py`/`providers/ollama_provider.py` at each stage boundary
above, plus a real run's numbers written into `ENGINEERING_JOURNAL.md` —
same rigor as the 301ms walker benchmark, not estimates.

**Decision gate:** if reusing the embedder/Chroma client within one process
(no daemon) closes most of the gap, stop there — don't build a daemon
Olorin doesn't need. If process-startup/import overhead is still the
dominant cost after that, design the persistent-process architecture next,
scoped narrowly (embedding model + maybe a warm Ollama connection surviving
across CLI calls) — explicitly NOT the full V4 system-tray/hotkey/watcher
feature set.

- [x] Build the instrumentation utility and wire it into all 9 stages above
- [x] Run `cli.py ask` with instrumentation on, record real per-stage numbers
- [x] Optimization #1 done implicitly: module-level singleton caching in
  `indexer/embedder.py`/`indexer/store.py` already reuses the embedder +
  Chroma client within a process; `HF_HUB_OFFLINE` closed the other real
  cost found along the way
- [x] Decision gate reached with real data: `agent_import_chain` (pure
  import overhead) is 17-30% of total wall clock on realistic tool-using
  queries — real, but no longer dominant once inference time (13-27s on
  longer answers) is counted. A daemon would eliminate the import tax but
  cannot touch inference time, which is the actual largest cost.

**DECISION (2026-07-10): daemon is justified by the numbers but deliberately
DEFERRED, not built.** Reasoning, logged for the same reason every other
decision in this doc is: a persistent daemon is not a one-time build — it's
ongoing complexity (process lifecycle, crash recovery, keeping in-memory
state consistent when a repo gets re-indexed out from under a live daemon,
concurrent-request handling) layered on top of a V1 feature set that is
itself about to change significantly in V2 (episodic memory, OLORIN.md
loading) and V3/V4. Building persistent-process infrastructure now risks
designing it around V1's shape and having to redesign it once V2 changes
what actually needs to stay warm. The measured cost (~6-10s import tax per
invocation) is real but bounded — nothing in V1 is unusable because of it —
which makes it exactly the kind of cost worth deferring in favor of
features that expand what Olorin can actually do. Revisit once V2/V3 have
settled the feature set enough that a daemon's shape wouldn't need to be
redesigned around the next thing built on top of it.

- [x] If justified: design daemon shape — **superseded by the above; not
  proceeding now.** Revisit post-V2/V3.

### V2 — It remembers
- [x] Episodic memory (SQLite conversations) — write path + query path
  (`search_history` tool) both implemented and verified live
  2026-07-11; see `ENGINEERING_JOURNAL.md` for the full session,
  including a real bug found and fixed along the way (post-retrieval
  complexity scorer never actually received retrieved_chunks in the
  live agent path — see Section 13)
- [x] Repository Knowledge Capsules — pulled forward from V3 (see below
  and Section 10), implemented and verified live 2026-07-11: module-
  level summaries generated via Faramir at index time, injected into
  the system prompt for step 1 of every run only (context-budget
  design, Section 10)
- [x] OLORIN.md per-repo config loading — implemented and verified live
  2026-07-11, including the harder test: persistence past step 1,
  unlike Knowledge Capsules (see Section 8/10/13)
- [x] Indexing reframed as an automatic, hash-cheap precondition of
  `ask` ("index-on-demand") rather than a manual step — real product-
  experience design decision, not a checklist item originally listed
  here; see Section 8/13
- [x] Direct-address routing override ("Groq, ...", "Boromir, ...") —
  implemented and verified live 2026-07-12; surfaced two real bugs
  (forced-provider had no fallback, chunks had no size ceiling) fixed
  the same session, see Section 13
- [x] Engineering Journal (auto session logging) — implemented,
  live-verified, and fully closed 2026-07-12 (`memory/journal.py`,
  `cli.py journal` command, Section 10). Surfaced bug #13 ("Worked on"
  synthesis hallucinating implementation work) on first live run, fixed
  at the prompt layer, re-verified live and confirmed working — same
  bar as every other V2 feature in this project.
- [x] Entity memory (decision/concept tracking) — designed via a direct
  conversation (2026-07-16) resolving the open questions this item was
  originally pushed out for (population mechanism, type scope, dedup
  policy), then implemented and live-verified the same day. See Section
  10 for the full design and Section 13 for the two real issues found
  and fixed (a dedup-matching miss, an accepted sampling-variance
  tradeoff).
- [x] Cerebras provider (implemented, not a stub) — 2026-07-13, second
  cloud tier in the auto-routing cascade (Groq → Cerebras → local);
  live verification pending (config.py sanity check + a real `ask`
  once a key is added), see Section 16
- [ ] ~~Together AI provider~~ — deliberately dropped, 2026-07-13: its
  free tier was retired industry-side, project stays free-tier-only,
  see Section 5/13
- [x] Better complexity scoring — the step-1-always-scores-zero gap was
  fixed via capsule-seeding (2026-07-14); the multi-file-detection half
  was fixed 2026-07-15 (`retrieved_files` threaded through
  `score_complexity()`, scored as distinct-file-count beyond the first ×
  `MULTI_FILE_WEIGHT`) — both halves of this item are now closed and
  live-verified (see Section 13's 2026-07-15 entry). `COMPLEXITY_THRESHOLD=15`
  re-tuning against live post-fix data is the one piece still open, and
  it's not actionable yet — needs real accumulated traffic first, not
  more code.
- [ ] `python cli.py ask "what did I figure out about auth last week?"` works
- [x] `web_search` tool added to registry, available to Groq, Cerebras,
  and Boromir (NOT Faramir — gated, see below), offline-safe fallback to
  trained knowledge — 2026-07-13, Tavily/Jina/Serper/Exa cascade
  (`tools/websearch.py`)
- [x] Faramir raw tool-calling isolation test written and run
  (`test_faramir_tools.py`, 2026-07-13) — **FAILED**: she narrated about
  calling the tool instead of emitting a structured call, no malformed
  JSON for the fallback parser to catch. **Resolved by gating, not
  fixing:** `core/agent.py` now withholds tools entirely when
  `force_provider == "faramir"` — matches how she's already used
  elsewhere in the project (Capsules/Journal are both tool-free plain
  completions). `web_search` (and every other tool) is available to
  Groq, Cerebras, and Boromir only.

### V3 — It's fast and deep
- [ ] GPU embeddings properly benchmarked + README benchmark table
- [x] ~~Knowledge Capsules (pre-computed module summaries loaded before retrieval)~~ — pulled forward to V2, see above and Section 10
- [ ] **searxNcrawl-based self-hosted search + crawling** (well-specified candidate, deferred 2026-07-14 — see Section 13) — self-hosted SearXNG + Crawl4AI, genuinely free/uncapped, and adds a capability the current `web_search` cascade doesn't have at all: following a URL from search results and reading the actual page content, not just a snippet. Deferred for the same reason the V1.5 daemon was: real ongoing operational cost (a second service to run, a Playwright/Chromium browser stack) with no measured need yet — the four-backend free cascade (Tavily/Jina/Serper/Exa) proved itself working on its first live test the same day this was raised. Revisit once that cascade's combined free ceiling is actually threatened, or once page-crawling (not just search) becomes a real need.
- [x] "Why is this file important?" (import graph analysis, reference
  frequency) — implemented 2026-07-18, **live-verified the same day**:
  `python cli.py index .` correctly extracted and stored import edges
  for this real repo (147 chunks, 8 files reindexed), and `python
  cli.py ask "Boromir, why is core/agent.py important?"` ran the
  `file_importance` tool end-to-end through the real agent loop and
  produced a correct, grounded answer (fact-checked against
  `core/agent.py`'s actual imports). One real bug found by the live run
  and fixed same-day: bare `from . import X` imports weren't resolving
  at all (only the module part was captured, never the imported name),
  traced to a Python object-identity comparison on tree-sitter nodes
  that silently never matched — fixed with a byte-offset comparison
  instead, re-verified in isolation before being reapplied to the real
  file. See Section 13 for the full story. Go/Java deliberately out of
  scope for V1 of this feature (see `indexer/imports.py`'s module
  docstring) — not a partial build, a scoped one.
- [x] Git integration: `git diff` → agent explains what changed —
  implemented 2026-07-18: `tools/git_diff.py` wraps `git diff` via
  subprocess, scoped to `repo_root`, read-only (never commits/stages/
  checks out). Defaults to uncommitted changes (`git diff HEAD`) when no
  ref is given; accepts an explicit ref or range (`"HEAD~3"`,
  `"A..B"`) otherwise. Splits the diff into per-file chunks so it feeds
  `Agent._accumulate_retrieved_chunks()`'s existing multi-file
  complexity signal (Section 7/13) the same way `search_codebase`
  already does — no special-casing needed. **Security, resolved via a
  direct design conversation before code:** a model-supplied `ref`
  reaches a subprocess argv, so it's validated against a strict
  allowlist regex plus an explicit leading-dash rejection (defends
  against option-injection shapes like `--upload-pack=evil`), and every
  git invocation adds a literal `--` separator before the (empty)
  pathspec, git's own standard idiom for the same purpose. Verified
  directly against a real option-injection-shaped string, a real
  multi-file diff, and a real non-git directory, all in an isolated
  sandboxed environment with a throwaway git repo — **not yet
  live-verified against this real repo's actual agent loop**, wired
  into `tools/registry.py` (8th tool) and `core/agent.py`'s system
  prompt ("eight tools") but pending a real `cli.py ask` run. See
  Section 16 for the exact next commands.
- [ ] Document ingestion (PDF → same ChromaDB index as code)
- [ ] Cross-repo querying (index multiple repos, query across them)

### V4 — It's a system
- [ ] Faramir/Boromir specialist routing (reasoning vs coding)
- [ ] LangGraph refactor of `core/agent.py`
- [ ] Context inheritance (project-aware loading, auto-switch on repo change)
- [ ] System tray icon (`pystray`) — lives in taskbar
- [ ] Global hotkey (summon from anywhere, `keyboard` library)
- [ ] File watcher (`watchdog`) — auto-reindex on file changes
- [ ] Daily briefing (morning summary of active repos + yesterday's work)

---

## 12. Tech Stack (Complete)

| Layer | Technology | Notes |
|---|---|---|
| Agent loop | Python, raw ReAct | Written from scratch, no framework |
| LLM abstraction | `openai` SDK (Groq), native `ollama` SDK (local) | Ollama's `/v1` OpenAI-compat endpoint is unreliable for tool calling (experimental, confirmed via testing) — native `/api/chat` used instead. See Section 13. |
| Cloud LLM | Groq `llama-3.3-70b-versatile` | Primary, 500+ tok/s |
| Local LLM (code) | Ollama `boromir` (qwen3:8b, `think:false`) | 16K ctx, 100% GPU, native tool-calling |
| Local LLM (reason) | Ollama `faramir` (deepseek-r1:8b) | 16K ctx, reasoning model |
| Code parsing | `tree-sitter` Python bindings | AST-aware chunking |
| Embeddings | `sentence-transformers` `bge-small-en-v1.5` | GPU (CUDA), ~130MB |
| Vector store | `chromadb` | File-based, no server |
| File walking | Rust (`ignore` + `rayon` crates) | Parallel, .gitignore aware |
| Persistence | `sqlite3` (stdlib) | Cache + memory + journal |
| CLI | `typer` | Clean, typed CLI |
| Config | `python-dotenv` | `.env` for API keys |
| Future orchestration | LangGraph | V4 refactor of agent loop |
| Windows tray | `pystray` | V4 |
| File watching | `watchdog` | V4 |

---

## 13. Key Design Decisions & Why

**Why raw ReAct before LangGraph?**
Build the loop yourself first so you can explain every line in an interview. LangGraph in V4 is a refactor of working code, not a black box you imported. You'll know exactly what each node represents because you built that mechanism by hand.

**Why Rust for file walking?**
CPU-bound, parallelizable, one-time cost per index. Python sequential walking of 10,000 files: 8-15 seconds. Rust parallel (`rayon`): under 1 second. Benchmark it, put real numbers in README. Same logic ripgrep used — use the `ignore` crate directly.

**Why post-retrieval complexity scoring?**
"Explain auth" sounds simple. But if retrieval returns 18 files and 8 middleware layers, it isn't. The retrieved context is the real signal, not the query keywords. Classify after you know what you're dealing with.

**Why local-first routing?**
~60% of queries (file reads, simple explanations, tool calls) don't need Groq. With local-first routing, 100K daily Groq tokens covers 6+ heavy sessions instead of 1.

**Why circuit breaker over retry?**
On 429, disabling a provider for 5 minutes costs 3 lines. Without it, every subsequent request hammers the rate-limited provider and you burn retries. Circuit breaker makes the fallback feel instant.

**Why keep base Ollama models?**
`boromir` and `faramir` use `FROM qwen2.5-coder:...` and `FROM deepseek-r1:...` in their Modelfiles. The named models are config layers on top of shared weights. Deleting the base breaks the named model. Both show as ~4.7GB/5.2GB in `ollama list` but only one copy of weights exists on disk.

**Why `web_search` is a shared tool, not a Groq-only feature?**
Confirmed by testing: Faramir (`deepseek-r1:8b`) confidently hallucinates on open-knowledge questions when answering from memory with no retrieval context — plausible-sounding, factually wrong. This isn't a config issue, it's a property of small parametric models: absent solid memorized facts, they generate fluent-sounding text rather than admitting uncertainty. But Groq's `llama-3.3-70b-versatile` has the exact same blind spot — it's just as ungrounded on live facts without a tool. So `web_search` is added to the shared tool registry, available to all three agents identically, rather than treated as a cost-avoidance substitute for Groq calls (those are orthogonal concerns — routing is driven by the complexity scorer, not by whether a question needs live facts). Offline, all three backends simply fall back to trained knowledge as before — no hard dependency on connectivity.

**Why test Faramir's tool-calling before wiring in `web_search`?**
`deepseek-r1:8b` emits a `<think>...</think>` reasoning block before its real output. Tool-calling parsers built for clean structured output can break on this — the model may talk about calling a tool inside its thinking instead of emitting an actual tool-call. Qwen-based Boromir is fine-tuned for function calling and doesn't share this failure mode. Don't assume parity; test Faramir in isolation first.

**Ollama tool-calling transport quirk (found and fixed at the root cause)**
Boromir (Qwen-based, fine-tuned for function calling) still hit an issue in practice: Ollama's OpenAI-compatible endpoint (`/v1/chat/completions`) sometimes returns a syntactically valid tool-call JSON payload in the plain `content` field instead of the structured `tool_calls` field, with `finish_reason` reported as `"stop"` instead of `"tool_calls"`. Confirmed via web search as a well-documented, ecosystem-wide issue — not specific to this project — reproduced independently in GitHub issues from Cline, OpenCode, and a Hermes-based agent framework, all hitting the identical symptom with qwen2.5-coder specifically. Ollama's own documentation marks the `/v1` endpoint as experimental. The model's reasoning was correct; the OpenAI-compat transport layer just failed to surface it in the expected shape.

**Root-cause fix:** switched `OllamaProvider` from the `openai` SDK (pointed at `/v1`) to the official `ollama` Python library against Ollama's native `/api/chat` endpoint, which correctly supports tool calling. This surfaced a second, related issue immediately: Ollama's native message schema disagrees with OpenAI's convention for tool-related messages — `tool_calls[].function.arguments` must be a `dict` (not a JSON string), and tool-result messages use a `tool_name` field instead of OpenAI's `tool_call_id`. Since `core/agent.py` builds message history in OpenAI-canonical shape (correct, since Groq needs it unmodified), `OllamaProvider` now performs bidirectional translation: native-response → OpenAI-shape on the way out (`ProviderResponse`), and OpenAI-shape → native-shape on the way in (`_translate_messages_for_native()`), both fully contained inside `providers/ollama_provider.py`.

A defensive fallback parser (`_try_parse_fallback_tool_call()`) is kept even after this fix — confirmed inert on a correctly-behaving response, so it costs nothing and guards against any future model/template combination that still misbehaves on either endpoint.

Deliberately NOT patched in `core/agent.py` at any point in this process — both issues were provider-specific transport/schema quirks, and per `BaseProvider`'s dependency-inversion principle, provider quirks must never leak past the provider that owns them. Zero changes were required to `core/agent.py`, `core/llm_client.py`, or `tools/registry.py` across two rounds of fixes — a concrete demonstration of the abstraction earning its keep.

**Fenced-JSON tool-call gap (found while wiring up the real `search_codebase`, fixed the same day)**
The native-API fix above closed the original transport quirk, but the defense-in-depth fallback parser it left behind (`_try_parse_fallback_tool_call()`) only matched bare `{...}` JSON. Once real retrieval was wired up and Boromir actually needed to call `search_codebase` against a live index, it emitted its stray tool-call JSON wrapped in a markdown code fence (` ```json ... ``` `) instead of bare JSON — a shape the fallback parser had never been tested against, so it silently fell through and the fenced text got returned as if it were a real final answer. Fixed by stripping a leading/trailing fence before the existing brace-matching check, entirely inside `providers/ollama_provider.py` — same architectural boundary as the original fix, no changes elsewhere. Lesson: a defensive fallback is only as good as the shapes it was tested against, not the shapes you assumed were exhaustive.

**Absolute-path bug in the Rust walker (found and fixed while building `cli.py`)**
`indexer_core/src/main.rs`'s JSON output contract (Section 8) always specified a repo-relative `path` field, but the implementation never actually enforced that — it emitted `path.to_string_lossy()` verbatim, which is relative or absolute purely depending on what the caller happened to pass as the walk root. This went unnoticed through the walker's own smoke test (which happened to use a relative `..` argument) and only surfaced once `cli.py`'s `index` command — which resolves the repo path to absolute via `os.path.abspath()` before invoking the walker — was built. Fixed at the Rust layer, the single source of truth regardless of what any given caller passes in: both the repo root and each file path are canonicalized, then the root is stripped as a prefix and the result forward-slash-normalized for cross-platform stability. Required wrapping the canonicalized repo root in an `Arc` (a first compile attempt hit `E0507` — a plain `PathBuf` can't be moved into a per-thread closure that the walker's parallel visitor invokes more than once), mirroring the `Arc<Mutex<...>>` pattern already used for the shared results vector.

**Boromir's base swapped from qwen2.5-coder to qwen3:8b (2026-07-09)**
Across one live session, Boromir (then qwen2.5-coder:7b-instruct-q4_K_M) produced three distinct malformed tool-call shapes — raw JSON in content, markdown-fenced JSON, and prose-prefixed bare JSON — each requiring a new special case in `_try_parse_fallback_tool_call()`. That's a pattern worth naming: patching the fallback parser for each new shape treats the symptom, not the cause, and there's no guarantee the fourth shape doesn't slip through the same way. Root cause: qwen2.5-coder has no native tool-calling training — it serializes tool descriptions into the prompt and decides for itself whether to emit structured output, which is fundamentally unreliable no matter how the parser is hardened. Verified via current benchmarks (searched, not assumed — training data alone would be stale for a model landscape this fast-moving) that Qwen3 has native tool calling built into its chat template, with Qwen3 8B repeatedly named the most reliable small local tool-calling model in independent 2026 testing. Switched Boromir's base to `qwen3:8b` — same VRAM class (~5.2GB vs. 4.7GB, both comfortable in the 8GB budget), Apache 2.0, with a genuine hybrid thinking/non-thinking toggle. Trade-off stated explicitly: qwen3:8b is general-purpose, not code-specialized like qwen2.5-coder was — acceptable because Boromir's actual job (calling `read_file`/`list_files`/`search_codebase` and reasoning over retrieved code) is comprehension and tool orchestration, not code generation, which is exactly the profile tool-call reliability should be weighted for.

**Discovered post-swap: Faramir and Boromir now share a base architecture, and why that's fine**
`ollama show` on both models reports `architecture: qwen3` after the swap — not a bug. DeepSeek's `deepseek-r1:8b` distill (Faramir) is R1's reasoning traces trained onto a Qwen3 base, so the shared label is real ancestry, not a misconfiguration. They remain distinct models: different license (MIT/DeepSeek vs. Apache 2.0/Qwen), different chat-template stop tokens (DeepSeek/R1 convention vs. ChatML — meaning prompt formatting genuinely isn't interchangeable despite the shared architecture label), different reported max context (131072 vs. 40960). The functionally important difference is deliberate, not incidental: added a `think: bool | None` parameter to `OllamaProvider.__init__()`, and `core/llm_client.py` now constructs Boromir with `think=False` (fast, task-focused execution is the whole point of its role) and Faramir with `think=True` (reasoning is the point). Without this, Boromir would have silently inherited whatever Ollama/Qwen3's default thinking behavior is — for a persona whose entire design intent is "no deliberation overhead before a tool call," that's not a neutral default, it's a regression waiting to happen.

**Post-retrieval complexity scorer never actually saw retrieved content (found + fixed 2026-07-11, building episodic memory)**
`Agent.run()` (`core/agent.py`) called `self.llm.chat(...)` without ever passing `retrieved_chunks` — on any step, including step 2+ after `search_codebase` had already returned real chunks into the message history. `score_complexity()` (`core/llm_client.py`) therefore always scored against an empty list, meaning Section 7's headline design goal — "classify complexity AFTER seeing retrieved chunks, not before, since the retrieved context is the real signal" — had genuinely never been exercised in the live `ask` path. It had only ever been proven in `llm_client.py`'s own standalone smoke test, which hardcodes `retrieved_chunks`. This is a V1-era gap, not something episodic memory introduced — it surfaced now because logging `complexity_score` into permanent history made the always-zero value visible and impossible to ignore for the first time, where before it was just an unused number nobody happened to look at closely.

**Fix:** added `Agent._accumulate_retrieved_chunks()`, called after every tool execution in the loop. It appends `search_codebase`'s returned chunk content and `read_file`'s returned file content into a running `retrieved_chunks` list that's threaded into every subsequent `llm.chat()` call within that same run. `list_files` and `search_history` results are deliberately excluded from this accumulation — filenames and past-conversation text aren't the code-context signal the scorer is meant to weigh, and folding them in would conflate orthogonal concerns for no benefit.

**Verified live:** `ask "How is Rust being used in project?"` — step 1 (before any retrieval) still correctly scored low and routed local; `search_codebase` returned 5 real chunks; step 2's `llm.chat()` call scored `complexity_score=42`, comfortably over `COMPLEXITY_THRESHOLD=15`, and the router escalated to Groq for the first time driven by real retrieved-content volume rather than query wording alone. `python cli.py history` confirmed the logged row shows `backend=groq complexity=42` — a real, trustworthy number, not a placeholder. Flagged, not treated as a regression: repos with large/talkative `search_codebase` results will now legitimately escalate to Groq more than V1's numbers implied, since the scorer is finally doing its actual job — worth keeping an eye on against the free-tier budget in Section 5.

**Why Knowledge Capsules were pulled forward from V3, and entity memory deferred instead (2026-07-11)**
Both were V2 candidates after episodic memory shipped. Entity memory's schema (Section 10) had sat as a stub since the doc's first draft, with none of its real design questions resolved — population mechanism (automatic LLM extraction vs. an explicit `remember_entity` tool the agent calls), type scope (does `"person"` even apply to a solo project? does `"repo"`-type overlap with the still-unbuilt OLORIN.md?), and update/staleness policy were all open. Knowledge Capsules, by contrast, had a concrete V3 description already ("pre-computed module summaries loaded before retrieval") and a clear, testable value proposition: give the agent a repo map before it decides whether to call a tool at all. Decided to build the better-specified, higher-payoff item out of strict roadmap order rather than build entity memory just because it was nominally "next" — same judgment as the V1.5 daemon deferral, applied in the opposite direction (building ahead of schedule instead of behind it), and logged with the same rigor.

**Knowledge Capsules' context-budget design (2026-07-11)**
The naive version of this feature — inject every module's summary into the system prompt on every step — has a real cost that isn't about repo size: the full `messages` list, system prompt included, gets resent on every step of the ReAct loop (there's no persistent server-side context between calls), and `read_file` alone can return up to ~5,000 tokens against Boromir/Faramir's 16K ctx. So capsule text left in place past step 1 competes directly with growing tool-result content for the same fixed budget — worst exactly on the multi-step queries where the budget is already tightest, and where capsules have stopped being useful anyway (the model's reasoning over concrete retrieved content by then, not trying to orient itself). Resolved by injecting capsules only into the very first system message of a run, then rebuilding `messages[0]` without them immediately after step 0's tool calls execute (`Agent.run()`, `core/agent.py`). Paired with a hard length cap enforced in the generation prompt itself ("3-4 sentences, under 100 words") so the one-time step-1 tax stays small and predictable regardless of module size. Per-query relevance filtering (only inject capsules for modules that seem related to the current question) was discussed and deliberately deferred — the real scaling answer for large repos, but solving a problem this repo's size (5-6 modules) doesn't have yet, same discipline as the daemon deferral.

**Verified live:** `ask "what is this project and how is it structured?"` resolved in a single step with zero tool calls, correctly synthesizing all seven modules' roles from capsule text alone — including correctly distinguishing `memory/capsules.py` (storage) from `indexer/capsules.py` (generation), which requires real synthesized understanding, not a lucky guess. A forced multi-step run (`ask "what does the score_complexity function do, read the actual code"`) confirmed the step-1-only stripping logic under real load: step 1 (capsules present) → `search_codebase` → step 2 (`messages[0]` correctly rebuilt without capsules, no malformed history, no crash) → `read_file` → step 3 → accurate final answer from Groq. A warm re-run of a similar orientation query came in at 13.3s, confirming an earlier 40s `ask` was Ollama's already-documented cold-start model-swap cost, not capsule injection overhead.

**Why OLORIN.md is never stripped, unlike Knowledge Capsules (2026-07-11)**
Both features inject content into the system prompt, and both were built the same session, but they resolve the step-1-only design question in opposite directions on purpose. Capsules are the agent's own generated orientation aid — useful for deciding what to do, safely discardable once the model is reasoning over real retrieved content. OLORIN.md is the opposite: explicit user-authored instruction ("never suggest SQLAlchemy") that has to hold for an entire run, not just the first decision. Stripping it after step 1 would mean the model could silently violate a stated project rule on any answer that took more than one step to produce — a correctness bug, not a missed optimization. `build_system_prompt()` threads `olorin_md` through unconditionally on every call a run makes, while `include_capsules` is the one flag that changes between step 1 and step 2+. Verified live specifically on a *step 2* answer (not just step 1) to confirm this distinction actually holds under real multi-step load, not just in the code.

**`list_files` had no result-size cap (bug #9, found 2026-07-11)**
Found while verifying OLORIN.md against a real, differently-shaped repo (Vorticity) that this project's own repo never exercised: `list_files({'pattern': '*'})` returned an entirely unbounded file list — Next.js build output, ML model checkpoints, notebook checkpoints, none of it excluded — that the local model then had to process in full, ballooning one step's latency to 115 seconds. Unlike `read_file`, which has always had a `max_chars` cap, `list_files` had never had an equivalent limit. Sharper than the latency alone: `list_files` results are deliberately excluded from `Agent._accumulate_retrieved_chunks()` (Section 7/13's retrieved_chunks fix) — filenames aren't code-context signal the complexity scorer should weigh — which means an oversized `list_files` result had zero chance to trigger a correct Groq escalation. It just silently cost real local latency, invisible to the one system built specifically to catch "this query got expensive." **Fix:** added `max_files=200` (mirroring `read_file`'s pattern) plus `total_matched`/`truncated` fields so the agent can tell "200 of 200" from "200 of 4,000" and react by narrowing its pattern. Expanded `IGNORED_DIRS` to cover `.next`, `dist`, `build`, `target`, `.ipynb_checkpoints`, `.pytest_cache`, `.mypy_cache` — the actual bulk of the repro. Verified live: the same repro completed in ~15s total afterward.

**Indexing reframed as index-on-demand, not a manual step (2026-07-11)**
Raised directly as a product-experience gap: requiring a separate `index` command before a new repo's first `ask` means the user has to know indexing exists as a concept at all — "think in terms of embeddings and vector databases" instead of "projects and questions." Resolved deliberately as *not* "reindex everything on every call" — the hash-based skip logic already built (Section 8) means a freshness check on an unchanged repo is nearly free (a fast walk + hash comparison), so folding it into every `ask` as an automatic precondition doesn't mean paying full indexing cost every time, only when something's genuinely new or changed. `cli.py`'s indexing pipeline was extracted into `_index_repo(repo_path, verbose)`, shared between the explicit `index` command (verbose, unchanged output) and `ask`'s silent automatic call. Honest, not hidden: because the daemon was deliberately deferred (V1.5), there's no background process to push first-time/changed-file work off the interactive path — a genuinely new or changed repo still pays real cost synchronously, before the agent starts reasoning. Addressed with explicit messaging (a "first time indexing" notice when a collection is empty, a one-line "repo changed" summary when real work happened) rather than either a silent multi-minute hang or noisy freshness-check chatter on every single call. `--skip-index` remains as an explicit escape hatch. **Verified live** across all three states (first-time, unchanged repeat, `--skip-index`) on a real never-before-seen repo.

**Direct-address routing: an explicit override, not the difficulty classifier this doc already rejected (2026-07-12)**
Raised directly: the complexity scorer routes on retrieved-content volume, not query wording (Section 7), which means there's no way to force a specific backend through phrasing alone. Wanted an inline override ("Groq, ...", "Boromir, ...") instead of only the `--provider` flag. Worth being precise about why this is a different decision than the one already made in this section: keyword-based *difficulty* classification was rejected in favor of post-retrieval scoring, because guessing how hard a question is from its wording is unreliable. This isn't that — `parse_direct_address()` doesn't guess anything; it recognizes a name the user deliberately typed at the start of their query and treats it exactly like the `--provider` flag, just expressed inline. Deliberately narrow (exact start of query only, four known names only) for the same reason the flag itself is explicit rather than inferred: a false-positive match would silently override routing on a decision the user never actually made. Precedence: an explicit `--provider`/`--force-local` flag always wins over an in-text address. The address is stripped from what the model sees but the original raw text is preserved for episodic memory logging, since the user really did type it.

**Forced-provider calls had no fallback (bug #10, found + fixed 2026-07-12)**
The very first live test of direct-address routing crashed the CLI outright — `ask "Groq, what is this project?"` hit a real Groq 413 (request too large for the free-tier 12,000 TPM limit) and the exception propagated all the way up as a raw traceback. `LLMClient._route()`'s auto-mode Groq attempt has always been wrapped in `try/except ProviderError` with a fallback to local; the forced-`groq` branch never was. This gap predates direct-address routing — the pre-existing `--provider groq` flag had the identical hole — it just went unnoticed because forcing Groq explicitly was rare until this session made it a natural, frequent thing to type. **Fix:** forced-`groq` now wraps its call in the same `try/except ProviderError` pattern as auto-mode, falling back to `self.default_local` on any failure and tripping the circuit breaker if it was a rate limit. `cli.py`'s `ask` also gained a top-level try/except around `agent.run()` as a safety net for whatever a forced-local failure has no further automatic fallback for without contradicting the user's explicit choice. **Verified live under a genuinely different failure mode than the one that found it** — a later run hit a malformed-tool-call `400` from Groq itself, and the same fallback caught it identically, good evidence the fix is generically robust rather than narrowly patched to the original 413.

**Chunks had no size ceiling (bug #11), and the fix's own bug (bug #12) — both found + fixed 2026-07-12**
`chunker.py`'s whole-file fallback (used for any language without a tree-sitter grammar, notably markdown) never had a size cap; its own docstring flagged this and explicitly deferred the fix to the embedder layer, which never actually implemented it. This project's own `OLORIN_PROJECT.md` and `ENGINEERING_JOURNAL.md`, grown very large from this session's own documentation work, became the concrete trigger for bug #10's 413 — a single whole-file chunk large enough to blow a request's token budget on its own. **Fix:** `indexer/embedder.py`'s new `_split_oversized_chunks()`, called inside `embed_chunks()`, splits any chunk over `MAX_CHUNK_CHARS` (2000 chars, roughly bge-small's own ~512-token limit — keeps what's embedded and what's stored/returned honestly in sync) into sequential sub-chunks. The first version of this split introduced its own bug immediately: it numbered each sub-chunk's lines relative to the chunk's own extracted text (always starting at 1), not the file's true absolute line numbers — meaning any two oversized chunks in the same file with roughly similar size would deterministically produce identical relative ranges ("1-55", "56-100", ...), colliding on `store.py`'s `file + lines` chunk ID and crashing ChromaDB's upsert with a `DuplicateIDError`. Not a rare coincidence — a guaranteed collision for any file with 2+ chunks needing splitting. **Fixed** by anchoring each sub-chunk's line numbers to the original chunk's true starting line (parsed from its own `lines` metadata). Also added a defense-in-depth guard in `upsert_chunks()` that de-duplicates by ID before calling Chroma's `upsert()` — which otherwise aborts an entire file's upsert, every legitimate chunk in the batch, on a single collision — as a backstop for any future collision cause, not a substitute for fixing root causes at the layer that produces them. **Verified live:** the guard fired correctly on the very next run, indexing completed without crashing, and the subsequent `ask` succeeded end-to-end.

**Known limitation, deliberately not fixed: retrieval precision after the line-budget chunk split (2026-07-12)**
`ask "Boromir, summarize what OLORIN_PROJECT.md says about the daemon deferral"` answered that the document "does not directly discuss" the daemon deferral — false; Section 11's entire V1.5 tier and this section's own dedicated entry cover it extensively. Likely retrieval missing the right chunk after bug #11's split, not the model fabricating — notably it did NOT hallucinate a wrong daemon-deferral narrative, it honestly reported what it could and couldn't find, the safer of the two failure modes. Plausibly exactly the caveat already logged in `_split_oversized_chunks()`'s own docstring: a blunt line-budget split can cut through a topical section instead of respecting document structure, hurting retrieval precision on long docs specifically. **Decision: logged as a known limitation, not investigated further this session.** A markdown/header-aware split would be the real fix if this proves to matter in practice — deliberately not chased now, consistent with this project's standing discipline of not building past what's actually been measured to be a problem.

**Engineering Journal automation, and why it reuses Capsules' generate/store split exactly (2026-07-12)**
Section 10's spec for auto-generated `journal/YYYY-MM-DD.md` files had sat unbuilt since V2 planning began, while this project's own `ENGINEERING_JOURNAL.md` kept being maintained by hand every session — the motivating example sitting in plain sight. Resolved as a pure read-and-reshape over existing episodic memory (Section 10): every field a journal entry needs (query, answer, tools, backend) is already written by `Agent.run()` on every completed run, so no new write path was needed, only a new query (`get_conversations_for_date()`) and a formatter. Storage deliberately follows ChromaDB's precedent (Section 8) rather than writing into the indexed repo: `~/.olorin/journal/<repo_hash>/YYYY-MM-DD.md`, keyed by the same sha256-of-resolved-path scheme, reimplemented locally in three lines rather than importing `indexer/store.py` and introducing a `memory/` → `indexer/` dependency that didn't previously exist.

The deterministic sections (Queries/Tools used/Backend) are pure aggregation and always run. "Worked on" is the one section that's a genuine synthesized theme rather than a reformatting of stored rows — generated via a single duck-typed-`llm_client` Faramir completion, deliberately mirroring `indexer/capsules.py`'s `generate_module_summary()` signature exactly, for the same underlying reason Capsules chose Faramir: this is prose synthesis over text, not tool orchestration, so Faramir's role per Section 2's lore fits and its known `<think>`-block tool-calling risk (Section 9) doesn't apply. Graceful degradation was treated as a requirement, not an afterthought: `llm_client=None` or any call failure falls back to a literal deduplicated query list rather than blocking the whole command, the same philosophy already applied to capsule generation failures during `index`.

**Deliberately not folded into `ask` the way indexing was.** Index-on-demand (Section 8) works as an automatic precondition specifically because the hash-skip logic makes a repeat check nearly free when nothing changed. Journal regeneration has no equivalent shortcut — the "Worked on" synthesis is a real LLM call every single time the command runs, because the whole point is reflecting the latest state of the day including whatever was just asked. Auto-triggering that on every `ask` would silently tax the interactive path for a document nobody's reading mid-session. Kept as an explicit `python cli.py journal` command instead — the same category of judgment as the V1.5 daemon deferral: not every automatable thing should be automated at every call site just because the mechanism now exists.

**Live verification surfaced a real bug: "Worked on" synthesis hallucinated implementation work (bug #13, found + fixed 2026-07-12).** The first live run against this repo's own real episodic memory (two logged queries, both asking Olorin to *explain or summarize existing material* — "read the actual walker code", "summarize what OLORIN_PROJECT.md says about the daemon deferral") produced a "Worked on" section describing implementation work that never happened: "Implement daemon deferral mechanism for processing tasks", "Handle background task execution delays properly". The daemon deferral was a documented *decision*, made and written up days earlier (Section 11's V1.5 tier) — nothing about it was implemented, and no query that day asked for implementation either. This was a content-quality bug, not a plumbing one: storage, formatting, and the deterministic sections were all correct, so "did the command run without error" alone would have missed it — the answer had to actually be read and fact-checked against what really happened, same discipline already applied everywhere else agent output gets verified in this project. Also the same underlying failure class Section 9 already names for bare parametric recall (confident, fluent, wrong) resurfacing in a different context: synthesis over sparse, underspecified real data, not zero-context trivia. **Fix, at the prompt layer:** `synthesize_worked_on()`'s prompt now explicitly instructs Faramir that a question asking to explain/read/summarize existing code or docs means the developer was reviewing that area, not building it — bullets should read "reviewed X" / "investigated Y", never "implemented"/"fixed" unless the question is actually about a change. Deliberately not fixed by suppressing LLM synthesis below some conversation-count threshold instead: that would hide the symptom on thin sessions without addressing why the model over-extrapolated in the first place, and would leave the identical risk unaddressed on larger sessions with the same kind of underspecified queries. **Re-verified live** — a re-run against the same real session produced correctly-grounded bullets ("Investigated the use of Rust...", "Summarized the OLORIN_PROJECT.md documentation..."), fix confirmed working.

**Together AI's free tier is retired — discovered, not assumed (2026-07-13).** Went to implement the originally-planned Groq → Together AI → Cerebras → local cascade (Section 7's V2 design) and verified current terms via web search rather than trusting Section 5's original "$25 free credits on signup" claim, which was already stale training-data-era information. Together's own pricing documentation (verified June 2026) confirms the signup credit was discontinued industry-side in July 2025; Together now requires a $5 minimum purchase upfront, no free trial. Cross-checked against several SEO/affiliate blog posts claiming "$100 free credits" still active — recognized as low-credibility recycled marketing content, not trusted over the vendor's own docs. **Decision, confirmed directly with Aryavart: stay free-tier-only.** `providers/together_provider.py` is deliberately not built — not a stub left for later, an active decision not to write a provider class for a paid service, logged with the same rigor as every other decision in this project rather than silently dropped from the roadmap. Cerebras absorbed the "second cloud tier" role instead; see below.

**Cerebras provider added (2026-07-13), and the real caveats that came with actually researching it.** `providers/cerebras_provider.py` mirrors `groq_provider.py` almost exactly — same OpenAI-compatible-endpoint pattern, concrete proof the `BaseProvider` abstraction (Section 7) does what its docstring always claimed: swapping in a new cloud provider is a new ~130-line class, zero changes to `core/agent.py` or `tools/registry.py`. Two things that only surfaced from checking Cerebras's actual current docs rather than assuming parity with Groq: the free tier caps context at **8,192 tokens**, tighter than Groq's 12,000 TPM budget that already caused bug #10's 413, and the free-tier model catalog **churns** — several independent sources within weeks of each other reported different available models, one blogger's chosen model reportedly vanishing mid-project. Handled by making `CEREBRAS_MODEL` env-overridable (same pattern as `GROQ_MODEL`/`BOROMIR_MODEL`/`FARAMIR_MODEL`) rather than hardcoding a value expected to stay valid indefinitely, and deliberately NOT adding pre-emptive request-size guarding for the 8K cap — a too-large request just raises `ProviderError` like any other failure and falls through the existing cascade to local, the same mechanism bug #10 already generalized. Adding speculative size-checking before it's ever been measured to actually bite would be exactly the kind of premature complexity this project has consistently avoided elsewhere (daemon deferral, capsule relevance-filtering deferral).

**Wiring Cerebras into the cascade — independent circuit breakers, not a shared one.** `LLMClient` gained `_cerebras_disabled_until`/`_cerebras_circuit_open()`/`_trip_cerebras_circuit()` as a direct parallel to Groq's existing fields rather than refactoring into a generic "circuit breaker per provider" abstraction — with only two cloud providers, a second named field-set mirrors the existing code's own style; a generic abstraction is the right move once a third cloud provider actually shows up, not before (same YAGNI discipline as everywhere else in this codebase). Auto-mode cascade: Cerebras is attempted only under the identical `should_try_groq` complexity gate Groq itself uses — it's a second attempt at the same cloud-escalation decision the scorer already made, not a separate routing tier with its own threshold — and only if `is_available()` (a real key is configured), checked explicitly in auto mode to avoid a silent, guaranteed-401 network call on every single query for anyone who hasn't opted in. The forced-provider branch (`--provider cerebras`, mirroring bug #10's fix for forced-Groq) skips that pre-check deliberately: a forced call is an explicit user request, so a missing key surfaces as a normal `ProviderError` through the real 401 and falls back to local the same way any other Cerebras failure would, rather than adding a second code path for the same outcome.

**`web_search` implemented (2026-07-13), closing a V2 item that had sat as a design decision without code since 2026-07-06 — cascade corrected same day after verifying real free-tier terms.** Four free-tier backends cascaded with the same dependency-inversion + graceful-fallback pattern already proven for LLM providers (Section 7), applied to search instead of chat. The first version of this module claimed "Tavily + Serper, 3,500 free searches/month combined" — **wrong**, caught the same day by Aryavart questioning it directly: Serper's 2,500 credits are a one-time signup grant, not a monthly allowance (confirmed via multiple independent sources, not just one blog). Corrected cascade: **Tavily** primary (1,000/month, genuinely recurring, no card, LLM-ready extracted text — chosen deliberately over building a separate fetch-and-parse layer, since Boromir's 16K context and already-fragile tool-call parsing benefit from small, pre-cleaned tool results the same way `list_files`'s cap (bug #9) and the chunk size ceiling (bugs #11/#12) did), **Jina** secondary (`s.jina.ai`, no card at all, ~1M free tokens per new key — one-time per key but large — also LLM-ready extracted text, replacing Serper's old slot; free tier is licensed non-commercial/CC-BY-NC, a real term worth knowing, fine for a portfolio project not being sold), **Serper** reserve (2,500 credits, one-time, raw SERP metadata only, kept for when the two backends above are both down/exhausted rather than a routine fallback), **Exa** opt-in last resort ($10 one-time credit, no card — the frequently-quoted "1,000/month recurring" figure only applies once a payment method is added, declined here for the identical reason Together AI was dropped; kept anyway despite the shallow free tier because its neural/semantic search scores meaningfully higher on multi-hop retrieval benchmarks than the other three). None configured returns an honest `{"error": ...}` steering the model back to trained knowledge, consistent with `search_codebase`'s/`search_history`'s existing honesty pattern for an unconfigured/empty state.

**Why the search-provider orchestration was chosen over a self-hosted option (SearXNG), discussed directly (2026-07-13).** Aryavart raised SearXNG (a self-hostable, free, no-API-key metasearch engine) as an alternative to combining several rate-limited free-tier APIs. Decided against it for now: running SearXNG means standing up and keeping alive a persistent local service (Docker, uptime, occasional upstream-engine blocking when Google/Bing notice they're being scraped through it) — real, ongoing operational complexity, the same category of cost the V1.5 daemon was deliberately deferred for (Section 11), and for the identical reason: no measured need for it yet. The four-backend cascade reuses an architecture this codebase has already proven three times over (`BaseProvider`-style abstraction + graceful fallback) and needs zero new infrastructure to operate. If usage ever actually threatens the combined free ceiling, that becomes a real, measured reason to revisit SearXNG — not a reason to build it speculatively now.

**Faramir tool-calling isolation test written, run, and FAILED — resolved by gating rather than fixing (2026-07-13), closing a gap that had sat open since 2026-07-06.** Section 9 flagged since the very first session that Faramir's `<think>` blocks could break tool-call parsing and that her raw tool-calling reliability should be tested in isolation before trusting it — but across seven sessions of building on top of that assumption, no such test was ever actually written or run. `test_faramir_tools.py` (repo root) sends a single trivial forced-tool-call prompt against a throwaway dummy tool, entirely isolated from the real agent loop and real tool registry. **Result: FAIL.** `finish_reason` came back `"stop"`, `tool_calls` was empty, and her content was plain prose narrating about calling the tool ("Let me pull up the latest weather from Tokyo using the `get_weather` tool...") rather than emitting a structured call — and critically, this produced **no malformed JSON for the existing fallback parser to catch**, since there was no JSON attempt anywhere in the content. That's a categorically different failure mode than the JSON-in-content quirks the fallback parser (`_try_parse_fallback_tool_call()`) was built for across bugs #1 and #5 — nothing to patch there, because the model never tried to emit structured output in the first place. **Decision, confirmed directly with Aryavart: gate tools away from Faramir entirely rather than chase a prompt-level or `think:false` fix.** `core/agent.py`'s `Agent.run()` now passes `tools=None` to `llm.chat()` whenever `force_provider == "faramir"`, and `build_system_prompt()`'s new `tools_available` flag swaps the five-tool listing for an explicit "you have no tools this turn" note — without that swap, showing her tools she can't reliably use would be actively misleading, not a harmless no-op. This isn't a new restriction in practice: it matches how Faramir has been used everywhere else in this project all along (Knowledge Capsule generation and Engineering Journal synthesis are both single plain completions with no tools, never a ReAct loop) — the gap was only ever in the direct-address/`--provider faramir` path, which had never actually been exercised with a tool-requiring query until this test.

**"Cerebras, ..." direct-address routing silently fell through to auto-mode instead of erroring (bug #14, found + fixed 2026-07-14).** First live end-to-end test of the forced-Cerebras path (`ask "Cerebras, what is this project?"`) didn't call Cerebras at all — the log showed `model=boromir` and, tellingly, `query='Cerebras, what is this project?'` with the address prefix still attached, proving `parse_direct_address()` never recognized it as an address in the first place. Root cause: when Cerebras was wired in (2026-07-13), only `--provider cerebras` was added; `parse_direct_address()`'s `_PROVIDER_ALIASES`/`_ADDRESS_PATTERN` were left untouched on the reasoning that "Cerebras" isn't part of Section 2's fantasy-lore roster (Olorin/Faramir/Boromir) — a real scoping decision, but one that was never actually communicated as a limitation, so it surfaced as a silent wrong-routing instead of an expected gap. **Worse than a missing feature:** the query wasn't rejected or flagged, it just quietly answered from the wrong backend, exactly the failure mode this project's "honest errors over silent wrong answers" discipline (search_codebase's not-indexed message, web_search's unconfigured message, journal's None-if-nothing-logged) has consistently tried to avoid — this one slipped through because it wasn't a missing-tool or missing-key case, just an incomplete parser update. **Fix:** added `"cerebras": "cerebras"` to `_PROVIDER_ALIASES` and `cerebras` to `_ADDRESS_PATTERN`, mapping directly to itself the same way `"groq"` does. Also tightened a related loose end found while fixing this: the step-1 `messages[0]` rebuild after tool execution wasn't passing `tools_available` through to `build_system_prompt()`, defaulting to `True` — harmless in practice today since Faramir (the only `tools_available=False` case) never reaches that code path at all, but inconsistent and worth closing explicitly.

**searxNcrawl considered as a genuinely-free search+crawl alternative, deliberately deferred to V3 (2026-07-14).** Aryavart proposed building on [DasDigitaleMomentum/searxNcrawl](https://github.com/DasDigitaleMomentum/searxNcrawl) (Crawl4AI + SearXNG, MIT-licensed) as a self-hosted, uncapped alternative to the four-backend `web_search` cascade — a stronger version of the SearXNG idea already considered and deferred on 2026-07-13, this time researched properly rather than dismissed on the same generic reasoning twice. Real finding: it's Python-importable directly (`crawler.crawl_page`/`crawler.crawl_site`, plus a plain SearXNG JSON search call), so integration wouldn't require speaking MCP protocol — it could be one more backend class in `tools/websearch.py`'s existing cascade pattern, not a rewrite. Its actual differentiator isn't cost, it's capability: Crawl4AI adds page *crawling* (following a URL from search results and reading the real content), which none of Tavily/Jina/Serper/Exa do at all today — worth naming as the real reason to eventually build this, separate from rate-limit avoidance. **Real cost, concretely (not generic hand-waving this time):** SearXNG itself still has to be deployed and kept alive as a separate service (this wrapper only calls out to a `SEARXNG_URL`, it doesn't bundle SearXNG), and crawling needs Playwright + a real Chromium browser install — meaningful memory/CPU pressure added to a machine already running two local LLMs, an embedding model, and ChromaDB. Also young (88 stars, single small team, no release since mid-April) — worth knowing as early-adopter risk, not a blocker. **Decision: logged as a well-specified V3 candidate (Section 11), not built now** — the measured-need bar from the original SearXNG deferral ("revisit once the free cascade's ceiling is actually threatened") hasn't been crossed; the four-backend cascade proved itself working on its very first live test the same day this was raised. Same category of judgment as the V1.5 daemon deferral: designed and ready to build, not forgotten, just not yet justified by measured need.

**Complexity scorer's step-1-always-zero gap fixed via capsule-seeding, scoped narrowly on purpose (2026-07-14).** Aryavart raised a real observation: local models were being triggered far more than the generous free cloud budgets (Groq ~100K TPD, Cerebras ~1M tokens/day) justified. Root cause, confirmed by tracing the actual mechanics rather than guessing: `score_complexity()` only ever sees `retrieved_chunks`, which started empty on step 1 of every single query by design — meaning step 1 *always* routed local regardless of real repo complexity, since there was structurally nothing yet to score. This wasn't a bug in the scorer's math, it was a gap in what it was ever shown. **Fix:** Knowledge Capsule text — already computed, already sitting in the system prompt — is real retrieved context about the repo, just retrieved from the capsule store instead of a live `search_codebase` call. `Agent.run()` now seeds `retrieved_chunks` with it before the loop starts, so step 1 can react to real repo richness instead of defaulting to zero every time. Deliberately framed as an *extension* of Section 7's post-retrieval principle, not a reversal of it — Section 13 already rejected keyword-based difficulty guessing from query wording; this is still entirely content-driven, the content just happens to already be in context. Also refactored `build_system_prompt()`'s `include_capsules: bool` into an explicit `capsules: list[dict] | None` parameter so the same fetched list feeds both the prompt and the scorer from one call site, rather than two independent fetches that could theoretically observe different snapshots.

**Scope deliberately narrowed, with Aryavart's explicit agreement:** the V2 roadmap item was "multi-file detection, planning intent" — this fix addresses neither directly, it closes the more fundamental gap underneath both (step 1 having no signal at all). Multi-file detection (scoring higher when retrieved content spans several distinct files/modules, not just raw chunk count) would need `retrieved_chunks`' shape to carry file attribution, a bigger refactor touching `_accumulate_retrieved_chunks()` and the scorer's signature — deliberately deferred until there's live data from this narrower fix to justify it, same "measure before building further" discipline as the V1.5 daemon gate. `COMPLEXITY_THRESHOLD=15` was set once, early, and has barely been exercised against real traffic since — Aryavart confirmed re-tuning it against real post-fix data is worth doing, once there's been enough traffic to tune against rather than guessing a new number blind.

**Observed alongside this, confirmed live 2026-07-14 (not just theorized): capsule-seeding fix verified working, and the Cerebras latency question resolved to a narrower, more useful finding than originally guessed.** Two live runs the same day confirmed the fix directly rather than inferring it: `ask "what is this project and how is it structured?"` (auto mode) attempted **Groq at step 1** — structurally impossible before this fix, since step 1 always scored zero. Along the way it also hit a genuinely fresh instance of the same Groq malformed-tool-call failure class bug #10 already documented (`400 tool_use_failed`, model emitting `<function=list_files{...}</function>` instead of a real structured call) — caught cleanly by the existing cascade, which correctly escalated to Cerebras rather than crashing or silently going straight to local, good further evidence the bug #10 fix generalizes rather than being narrowly patched to its original trigger.

On the Cerebras latency question: the original hypothesis ("gpt-oss-120b is just slow, possibly a hidden harmony reasoning channel") turned out to be too broad. Both live runs showed a consistent, narrower pattern — every *tool-call*-producing Cerebras step was fast (610ms, 967ms, 360ms, 389ms, 641ms across two runs), and the ONLY slow call in either run was the one producing a long final text answer (61,641ms) — compared directly against Groq producing a comparably substantive final answer in 812ms in the same run, a ~75x gap on the same kind of task. So the real, measured finding is: Cerebras isn't uniformly slow, it's specifically slow at long final-answer generation, not short tool calls. The hidden-reasoning-channel theory remains plausible as the *why* but wasn't pursued further — offered adding `input_tokens`/`output_tokens` to the routing log line to confirm it directly (a small, well-justified change), but Aryavart judged the latency pattern alone sufficient signal for now and declined it. Practical takeaway, not yet enforced anywhere in code: Cerebras looks like a reasonable escalation target for tool-call-heavy steps, but forcing it specifically for long-final-answer questions (e.g. via direct address) is measurably much slower than letting Groq handle those — worth keeping in mind, not yet acted on architecturally.

**Multi-file detection built (2026-07-15), closing the half of this item deferred the day before.** The gap named on 2026-07-14 was specific: `retrieved_chunks` was a flat `list[str]` with no file attribution, so there was nothing for a multi-file signal to be computed from. Fix: `Agent._accumulate_retrieved_chunks()` now also populates a parallel `retrieved_files: set[str]`, sourced from fields that already existed for other reasons — `search_codebase`'s chunk metadata already carries a `"file"` key (`indexer/store.py`, Section 8), `read_file`'s result already carries a top-level `"path"` key (`tools/reader.py`) — so no new tool output shape was needed, only a second accumulator reading fields already there. `Agent.run()` seeds `retrieved_files` from capsule module names on step 1, mirroring exactly how `retrieved_chunks` is already seeded from capsule summaries, and never clears it afterward for the identical reason `retrieved_chunks` isn't (Section 7's 2026-07-14 entry) — it only ever feeds `score_complexity()` math, never re-enters the actual messages sent to a model, so there's no context-budget reason to strip it.

`core/llm_client.py`'s `score_complexity()` gained a `retrieved_files` parameter and a new term: `(distinct_files - 1) * MULTI_FILE_WEIGHT` (weight `3`, a module constant, same tunable-without-code-changes philosophy as `COMPLEXITY_THRESHOLD` but not env-configurable — an internal scoring weight, not an operational knob). Deliberately `distinct_files - 1`, not `distinct_files` directly: touching exactly one file isn't a "spans multiple files" situation at all, so a single-file result should contribute zero from this term, not a baseline amount stacked on top of the existing chunk-count term for no new information. This is a genuinely different signal than chunk count alone — five chunks from one file (the same function explained five ways) is meaningfully less complex than five chunks spanning five files (a question that actually requires relating separate parts of the system), a distinction raw chunk count can't make on its own.

**Verified live, two ways.** Unit-level (`python -m core.llm_client`'s new third smoke-test block): identical query and chunk content scored `6` at one file vs. `12` at three files — confirms the term contributes exactly `(3-1)*3=6` on top of the unchanged base score, isolated from any live-call noise. Then a real `ask` (`"how do the agent loop and the LLM client work together to route a request?"`) correctly triggered a `search_codebase` call spanning `core/agent.py` and `core/llm_client.py`, routed to Groq, and `python cli.py history` confirmed the logged row: `complexity=89` — a real number reflecting capsule-seeded chunk volume, the multi-file term, and the "why"-adjacent planning-word match all stacking together, not a placeholder. Both halves of the V2 "better complexity scoring" item (Section 11) are now closed. `COMPLEXITY_THRESHOLD=15` re-tuning remains the one open piece, still correctly gated on accumulating real post-fix traffic rather than being guessed at again.

**Import graph ("why is this file important?") — designed via a direct conversation before any code, same discipline as entity memory (2026-07-18).** Three open questions resolved up front rather than guessed at: (1) language scope — multi-language via tree-sitter was chosen over Python-only, reusing `chunker.py`'s existing grammar infrastructure rather than adding a second parsing approach; (2) when to build it — at index time, cached, rather than on-demand, mirroring Capsules' philosophy of paying the cost once at index time rather than on every query; (3) how the agent accesses it — a new `file_importance` tool, not a system-prompt injection like Capsules, since import-graph data (potentially every file in a large repo) doesn't share Capsules' small-and-bounded property (Section 10's entity-memory entry already made this exact call for the identical reason).

**Scope deliberately narrowed to four language families, not the six `chunker.py` already knows how to chunk.** Extraction (`indexer/imports.py`) uses the same grammar-lookup-with-soft-fallback pattern as `chunker.py`, but Go and Java are left out entirely rather than partially wired up — resolving either correctly needs information this project doesn't parse (Go's module name from `go.mod`, Java's source root like `src/main/java/`), and extraction with no working resolution is dead code that always drops everything, not a partial feature. This is the same posture `chunker.py`'s own `CHUNKABLE_NODES` comment already takes ("deliberately conservative for V1") applied to a new module, not a new philosophy.

**Storage piggybacks on the existing per-file hash-skip loop instead of its own hash-tracking table, unlike Capsules.** Capsules need `needs_regeneration()` because module-level capsule generation doesn't naturally align with any single file's reindex decision — a capsule covers several files, any one of which changing should trigger regeneration. Import-graph edges are the opposite: they're keyed one-to-one to the same file `cli.py`'s `_index_repo()` loop already decided needs re-chunking via its existing hash comparison. Reusing that decision (`memory/import_graph.py`'s `set_file_imports()` is called immediately after a file's chunks are upserted, inside the same `if existing_hash != file_hash` branch) means zero new hash-tracking logic was needed — genuinely simpler than Capsules' design, not a corner cut, because the two features' natural granularity is different.

**Verification status, updated 2026-07-18 (live run completed after this section was first written).** Extraction and resolution logic was verified in isolation two ways — (1) node types and field names were confirmed against real tree-sitter parses rather than assumed from training-data memory; (2) `indexer/imports.py`'s own `__main__` smoke test exercises extraction + resolution together against hand-built examples with asserted expected output. **The subsequent live run against this real repo (`cli.py index .` + `cli.py ask "Boromir, why is core/agent.py important?"`) found a real bug the isolated smoke test's flawed assertions had masked:** bare `from . import X` imports (no module name after the dot, just an imported symbol list) were only ever contributing the bare `"."` module text to the raw-specs list — the actual imported name (`X`) was never captured, so a real submodule import like `from . import registry` could never resolve to the file it should. Root cause, once traced: `_extract_python()`'s walk used `child is module_node` to find children positioned after the module_name field, and tree-sitter's Python bindings don't preserve object identity across separate node accesses — `child_by_field_name("module_name")` and the same underlying node reached via `node.children` iteration are different Python objects even though they point at the same byte range, so the identity check silently never matched and the entire combined-name-capture branch never fired despite looking correct in review. **Fix:** compare `start_byte`/`end_byte` instead of object identity. The isolated smoke test's own assertions had actually encoded the bug's symptom as "expected" output (asserting a fixed resolved-file list regardless of which file was doing the importing, when relative-import resolution should legitimately differ by importer location) — corrected to test the same source against two different importer positions, matching real Python package semantics. Both fixes verified in a sandboxed environment before being reapplied to the real repo file. **End-to-end live verification, the actual bar this project holds every feature to:** `cli.py index .` ran cleanly against this real repo (147 chunks, 8 files reindexed, 5 capsules regenerated to account for the new files), and `cli.py ask "Boromir, why is core/agent.py important?"` correctly called `file_importance`, correctly reported `core/agent.py` as imported by exactly `cli.py` (matching the real repo), and correctly listed its own real imports (`config.py`, `core/llm_client.py`, `core/logging_config.py`, `memory/__init__.py`, `tools/registry.py`) — including `memory/__init__.py`, which is the technically-correct resolution target for `from memory import capsules as capsules_store`-style imports (importing a name from a package really does import that package's `__init__.py` first). This is the same class of finding this project has hit repeatedly (bugs #10, #13, #14, entity memory's dedup miss): logic that looks correct on inspection and even passes a shallow test can still be wrong in a way only a real run against real data surfaces — which is exactly why "unit-verified" and "live-verified" are tracked as two separate, non-substitutable bars throughout this document.

**Git integration (`git diff` → agent explains what changed) — three questions resolved directly before code, same discipline as import graph (2026-07-18).** (1) Default diff scope: uncommitted changes (`git diff HEAD` — working tree + staged vs. HEAD) rather than "last commit only" — "what have I changed?" is the more common real question than "what just happened?", and an explicit `ref` still covers the latter. (2) Scope narrowed to just `git_diff`, not a companion `git_log` tool — matches the roadmap item's literal wording rather than expanding it preemptively; a log/history tool is a legitimate, separate follow-up if it turns out to be needed, not assumed necessary now. (3) Security posture for a model-supplied `ref` flowing into a subprocess argv: a strict allowlist regex (alnum + `. _ / - ~ ^`) plus an explicit rejection of any ref starting with `-`, PLUS a literal `--` separator before the pathspec in every git invocation — deliberately layered rather than relying on either alone, since the regex alone doesn't rule out `--` itself being in the allowed charset for characters *other* than the leading one, and the `--` separator alone doesn't stop a leading-dash ref from being parsed as an option before that separator is even reached.

**Verified against a real option-injection-shaped string, not just reasoned about.** `git_diff(ref="--upload-pack=evil")` was tested directly (isolated sandbox, throwaway git repo) and confirmed rejected by the leading-dash check before ever reaching subprocess — the kind of security claim this project treats the same as any other implementation claim: verify it happened, don't assume the logic is airtight because it reads that way.

**Diff results are shaped as per-file chunks, not one flat string, specifically so they feed the existing multi-file complexity signal for free.** `_split_diff_by_file()` splits git's unified diff output on each `"diff --git a/X b/Y"` header into `{"file", "content"}` entries — the exact same shape `search_codebase`'s chunks already use, which means `Agent._accumulate_retrieved_chunks()` needed zero changes to also credit a multi-file diff toward `retrieved_files`' multi-file complexity term (Section 7/13). A diff touching five files legitimately is a more complex question than one touching a single file, the identical reasoning that motivated the multi-file scoring work in the first place — this tool just happens to produce content shaped the same way `search_codebase` always has, so the existing mechanism applies without modification.

**Verification status:** subprocess handling, ref validation, diff-splitting, and all four error paths (invalid ref, nonexistent ref, non-git directory, and the real multi-file-diff happy path) were verified against a real throwaway git repo in an isolated sandbox before writing the file to this real repo. **Not yet run against this repo's actual agent loop** — pending a real `cli.py ask` call that triggers `git_diff`, same open item as import graph had before its own live run caught bug #15. See Section 16 for the exact next command.

---

## 14. The Recruiter Story

*"Olorin is a local-first engineering assistant with persistent project memory. It indexes codebases using a Rust-parallelized file walker and tree-sitter AST chunking, stores embeddings in ChromaDB with GPU acceleration, and routes queries through a raw ReAct agent loop to Groq's frontier models online or specialist local models offline — Boromir (qwen2.5-coder) for code tasks, Faramir (deepseek-r1) for reasoning. It maintains episodic memory and Knowledge Capsules — pre-computed structural summaries of key modules — so answers come from understanding, not just retrieval. Built iteratively: raw agent loop first to understand the mechanics, then refactored to LangGraph for production-grade state management."*

---

## 15. Concepts You Can Explain in an Interview

| Concept | Where in Olorin |
|---|---|
| Provider abstraction / dependency inversion | `BaseProvider` + all providers |
| OpenAI function calling / tool use spec | `tools/registry.py`, agent loop |
| Cross-provider schema translation (bidirectional adapters) | `providers/ollama_provider.py` — native ↔ OpenAI-canonical message translation |
| Diagnosing root cause vs. patching symptoms | Ollama tool-calling transport quirk, Section 13 |
| ReAct agent pattern | `core/agent.py` |
| RAG (retrieval-augmented generation) | Full indexer + search pipeline |
| AST-aware chunking vs naive text splitting | `indexer/chunker.py` |
| Vector embeddings + similarity search | `indexer/embedder.py` + ChromaDB |
| Post-retrieval complexity routing | `core/llm_client.py` scorer |
| Circuit breaker pattern | `core/llm_client.py` |
| Rust parallelism (rayon) | `indexer_core/src/main.rs` |
| Cross-language integration (Rust→Python) | subprocess JSON bridge |
| LangGraph StateGraph, nodes, edges | V4 refactor |
| Episodic + entity memory design | `memory/` |
| Cache invalidation strategy | `memory/cache.py` |
| GPU vs CPU inference tradeoffs | Embedding model config |

---

## 16. Where Things Stand (as of last session) & Next Steps

**V1 is complete and the demo target has been verified live, end-to-end,
with the answer fact-checked against real source code. V1.5's decision
gate (measure before building a daemon) has also been reached with real
data, and Boromir's base has been swapped for tool-call reliability.**

**Done and verified live, end-to-end:**
- Local models: Boromir (`qwen3:8b`, `think:false` — swapped 2026-07-09 from qwen2.5-coder) and Faramir (`deepseek-r1:8b`, `think:true`), both 16K ctx
- `config.py`, `providers/base.py`, `providers/groq_provider.py`
- `providers/ollama_provider.py` — native `ollama` library, bidirectional message translation, fence-aware fallback parser, per-persona `think` control
- `core/llm_client.py` — post-retrieval complexity scorer, circuit breaker, SQLite query cache, structured logging
- `tools/reader.py` — `read_file` + `list_files`, path-traversal protected
- `tools/search.py` — real ChromaDB semantic search via `indexer/store.py`'s `query()`, honest "not indexed yet" error when a repo's collection is empty
- `tools/registry.py` — schemas + dispatch, `repo_root` injected into every tool call including `search_codebase`
- `core/agent.py` — hand-written ReAct loop
- **The full indexer pipeline (Section 8)** — Rust walker (relative-path-correct, 301ms benchmark), tree-sitter chunker, GPU embeddings (`bge-small-en-v1.5`, `HF_HUB_OFFLINE` set — no more Hub network check on load), ChromaDB store
- **`cli.py`** — `index <path>` and `ask "<query>" [--profile]`, both verified live
- **`core/profiling.py`** — 9-stage latency instrumentation (Section 11's V1.5 plan), wired through the full `ask` path

**V1 demo target achieved (2026-07-09):** `python cli.py index .` indexed 25 real files into 74 chunks in 10.23s; `python cli.py ask "how does the agent loop handle tool calls?"` correctly called `search_codebase`, retrieved real chunks from `core/agent.py`, and produced a grounded 8-point answer — verified accurate against the real source, no fabricated details.

**Fifteen real bugs found and fixed at the correct architectural layer across this project so far, plus a sixteenth found via the same live-verification discipline** (full stories in Section 13 and `ENGINEERING_JOURNAL.md`):
1. Ollama's OpenAI-compat endpoint silently failing tool calls (fixed by switching to native API), plus a follow-on message-schema mismatch (fixed with bidirectional translation) — both fully contained in `providers/ollama_provider.py`.
2. The `ignore` crate's `require_git` default silently disabling `.gitignore` outside an actual git repo.
3. A `.gitignore` file written with literal quote characters wrapped around every pattern, making every rule a silent no-op — same external symptom as bug #2, different root cause.
4. The Rust walker emitting absolute paths instead of the documented repo-relative contract — surfaced only once `cli.py` started passing an already-absolute repo path in; fixed by canonicalizing + stripping the prefix in `main.rs`.
5. The Ollama fallback tool-call parser only recognizing bare JSON, missing a markdown-fenced variant Boromir produced on a real `search_codebase` call — fixed by stripping the fence before the existing parse check.
6. The V1.5 profiling report itself double/triple/quadruple-counting nested spans (a single Ollama call recorded four overlapping ways, `total_request` summed on top of everything it already contained) — produced a nonsensical negative "unaccounted" number; fixed with an explicit top-level-vs-nested distinction and a true non-overlapping Ollama duration partition.
7. `sentence-transformers` silently pinging Hugging Face Hub on every embedder load despite a fully local cache — a real, measured 5.8s cost; fixed with `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`.
8. The post-retrieval complexity scorer (Section 7's headline design goal) never actually receiving `retrieved_chunks` in the live `ask` path — `Agent.run()` never threaded tool results back into `llm.chat()`, so every real query scored against an empty list regardless of how much was actually retrieved. Surfaced by episodic memory logging a permanent, visible `complexity_score=0` on every row; fixed with `Agent._accumulate_retrieved_chunks()`, verified live with a real Groq escalation (`complexity_score=42`) driven by actual chunk volume.
9. `list_files` had no result-size cap at all, unlike `read_file`'s `max_chars` — an unbounded file listing against a large real-world repo (build artifacts, ML checkpoints) ballooned one step's local inference latency to 115s, and was invisible to the complexity scorer by design (filenames are deliberately excluded from `retrieved_chunks`). Fixed with a `max_files=200` cap, `truncated`/`total_matched` fields, and an expanded noise-directory list; verified live at ~15s for the same repro.
10. Forced-provider calls (`--provider groq`, and the new direct-address routing) had no fallback at all — any Groq failure crashed the CLI with a raw traceback instead of degrading to local like auto-mode already does. Fixed by wrapping the forced-`groq` branch in the same `try/except ProviderError` pattern auto-mode uses, plus a top-level safety net in `cli.py`; verified live against two genuinely different Groq failure modes.
11. Chunks had no size ceiling at all — `chunker.py`'s whole-file fallback (markdown, notably) could produce one arbitrarily large chunk, and this project's own growing docs became the concrete trigger for bug #10's request-too-large error. Fixed with `embedder.py`'s `_split_oversized_chunks()`.
12. That fix's own bug: sub-chunks were numbered with line ranges relative to the chunk's own text instead of the file's true absolute lines, causing deterministic Chroma ID collisions (`DuplicateIDError`) for any file with 2+ oversized chunks. Fixed by anchoring to real file line numbers, plus a defense-in-depth dedup guard in `upsert_chunks()`.
13. Engineering Journal automation's "Worked on" synthesis hallucinating implementation work that never happened — two real logged queries asking Olorin to *explain/summarize* existing code and docs got synthesized into fabricated "implemented X"/"fixed Y" bullets, extrapolated from a documented decision's name rather than grounded in what was actually asked. A content-quality bug, not a plumbing one — storage and formatting were correct, so it only surfaced by reading and fact-checking the actual output. Fixed at the prompt layer (`memory/journal.py`'s `synthesize_worked_on()`): explicit instruction that explain/read/summarize questions mean the developer was reviewing, not building. **Re-verified live** — a re-run against the same real session produced correctly-grounded bullets, fix confirmed working.
14. "Cerebras, ..." direct-address routing silently fell through to auto-mode and answered from the wrong backend (Boromir) instead of erroring or routing correctly — the parser was never updated when `--provider cerebras` shipped a day earlier. Found on the very first live end-to-end test; fixed by adding `cerebras` to `parse_direct_address()`'s alias map and regex, mapping directly to itself like `groq` does.
15. The import graph's bare `from . import X` extraction (V3, `indexer/imports.py`) silently captured only the module part (`"."`), never the imported name — so a real submodule import like `from . import registry` could never resolve to a file. Root cause: `_extract_python()` used `child is module_node` (Python object identity) to find children positioned after the module_name field, but tree-sitter's Python bindings hand back a fresh wrapper object on every access, so the identity check silently never matched even for the same underlying node — the entire combined-name-capture branch never fired despite looking correct in review. Found by a live `cli.py index .` + `cli.py ask` run, not by the isolated smoke test written alongside the original code, whose own assertions had actually encoded the bug's symptom as "expected" (a fixed resolved-file list that didn't account for relative-import resolution legitimately differing by importer location). Fixed by comparing `start_byte`/`end_byte` instead of identity; both the fix and the corrected smoke-test assertions were re-verified in isolation before being reapplied to the real repo file. See Section 13's 2026-07-18 entry for the full story.
16. `git_diff`'s (V3, `tools/git_diff.py`) truncation logic used `break` instead of `continue` when a file's diff didn't fit the remaining `max_chars` budget — meaning a single large file appearing early in git's own (alphabetical) file ordering silently starved every smaller file behind it, rather than just being skipped itself. Found live against this real repo's own accumulated, uncommitted V1→V3 diff (Aryavart hadn't committed since V1): `files_changed=12` but `files_shown=0`, even though several individual files (`config.py`, `tools/reader.py`, `providers/base.py`) were well under the 20,000-char budget on their own — `ENGINEERING_JOURNAL.md`, alphabetically first and enormous after a full day of doc updates, hit the cap and the loop simply stopped rather than moving on. Not caught by the sandbox verification the tool shipped with, since that test's synthetic repo never had a diff large enough to exceed `max_chars` in the first place — a real gap in what the isolated test actually exercised, not a flaw in the reasoning. Fixed by changing `break` to `continue`: a file that doesn't fit is skipped, not treated as an end-of-budget signal for the whole diff. Reproduced the exact failure shape in a fresh sandbox test (one huge file ordered first, one small file after) before applying the fix, confirmed the fix resolves it, then re-ran the tool's full original test suite to confirm nothing else regressed.

**Boromir's base swapped from qwen2.5-coder to `qwen3:8b` (2026-07-09):** three distinct malformed tool-call shapes in one session (bare JSON, markdown-fenced JSON, prose-prefixed JSON) made it clear the fallback parser was chasing symptoms of a model with no native tool-calling training, not a fixable parsing gap. Qwen3:8b has native tool-calling support and is independently benchmarked as the most reliable small local tool-calling model in current testing, at the same VRAM cost. Discovered afterward that Faramir and Boromir now share a Qwen3 base architecture (DeepSeek's R1 distill is trained on top of Qwen3) but remain functionally distinct — different license, different chat-template tokens, and now explicit per-persona `think` control (`False` for Boromir, `True` for Faramir) added to `OllamaProvider`, which hadn't existed at all before this session.

**V1.5 measurement complete, daemon justified but deliberately deferred
(2026-07-10):** profiling confirmed `agent_import_chain` (import overhead)
is real (17-30% of wall clock on realistic tool-using queries) but no
longer dominant once inference time is counted — a daemon would remove the
import tax but can't touch inference time, the actual largest cost.
Decided not to build it now: a persistent daemon is ongoing complexity
(lifecycle, crash recovery, state consistency across re-indexing), and
building it before V2's feature set (episodic memory, OLORIN.md loading)
settles risks designing it around the wrong shape. The ~6-10s tax is real
but bounded — nothing in V1 is unusable because of it. Revisit post-V2/V3.
See Section 11's V1.5 tier for the full reasoning.

**Next up: V2 features** — episodic memory and Knowledge Capsules both
complete (see below), OLORIN.md per-repo config loading and Engineering
Journal automation still open. Entity memory deliberately deferred
pending a real design pass. See Section 11's V2 tier.

**Episodic memory shipped (2026-07-11):** `memory/history.py` implements
Section 10's `conversations` table live — every `Agent.run()` now logs
query, answer, tools used, backend, and (thanks to bug #8 above) a
trustworthy complexity score, at every return path including non-happy-
path exits. Queryable via `python cli.py history [--search]` and via a
new fourth agent tool, `search_history` (keyword-overlap, not semantic —
see Section 10's scope note), which the agent correctly chose over
`search_codebase` on its own for a memory-shaped question in live
testing.

**Repository Knowledge Capsules shipped (2026-07-11), pulled forward from
V3:** module-level summaries (`memory/capsules.py` storage,
`indexer/capsules.py` generation via Faramir) computed at `index` time
and injected into the agent's system prompt for step 1 of every run only
(context-budget design — see Section 10/13). Verified live: a
zero-tool-call, single-step answer to an orientation-style question,
correctly synthesizing all seven modules from capsule text alone, plus a
real multi-step run confirming the step-1-only stripping logic holds up
under actual tool-calling load. Entity memory was discussed alongside
this and deliberately deferred (Section 10/13) — its design questions
weren't resolved yet, and Capsules had the clearer payoff.

**OLORIN.md loading shipped (2026-07-11):** `core/agent.py`'s
`load_olorin_md()` reads per-repo config once per `Agent` instance and
threads it through every step's system prompt — deliberately never
stripped, unlike Capsules, since it's user-authored instruction that has
to hold for a whole run (Section 8/13). Verified live specifically on a
step-2 answer, not just step 1, to confirm that distinction actually
holds under real multi-step load.

**A real bug (`list_files` had no result cap) found while verifying
OLORIN.md against a real large repo, fixed same day (bug #9)** — see
Section 13 for the full story, including its notable invisibility to the
complexity scorer by design.

**Indexing reframed as index-on-demand (2026-07-11):** raised directly as
a product-experience gap — a real user shouldn't need to know indexing
exists as a separate step. `_index_repo()` extracted from `cli.py`'s
`index` command and now runs automatically as a precondition of every
`ask`, cheap when nothing's changed thanks to the existing hash-skip
logic, with honest messaging (not silence) when real work happens.
`--skip-index` remains for advanced use. Verified live across all three
states on a real never-before-seen repo. See Section 8/13 for the full
design conversation and the tradeoff it doesn't eliminate (no daemon
means first-time/changed-file cost is still paid synchronously).

Remaining V2 items (as of 2026-07-12): entity memory's design pass
(pushed to a later, finishing-stage version per user direction,
2026-07-12) and better complexity scoring — both since closed, see the
2026-07-15 and 2026-07-16 entries further down. Engineering Journal
automation and the Cerebras/web_search work are both done, pending only
live verification — see below.

**Direct-address routing shipped (2026-07-12), and three real bugs it
surfaced:** `core/agent.py`'s `parse_direct_address()` lets a query name
its backend inline ("Groq, ...", "Boromir, ...") — an explicit user
override functionally identical to the `--provider` flag, not the
keyword-based difficulty classification this doc already rejected
elsewhere. Surfaced bug #10 (forced-provider calls had no fallback,
crashing on the very first live test) and bugs #11/#12 (chunks had no
size ceiling; the fix for that had its own duplicate-ID bug) — all three
fixed and verified live the same session, full stories in Section 13.
Also surfaced a known limitation, deliberately not fixed: a retrieval
miss on a real question, likely from the blunt line-budget chunk split
cutting through document structure — logged, not chased, consistent
with this project's standing discipline around what's actually been
measured to matter.

**Engineering Journal automation implemented and live-verified (2026-07-12),
closing out Section 11's V2 checklist:** `memory/journal.py` + `cli.py
journal` automate exactly what this project's own `ENGINEERING_JOURNAL.md`
had been doing by hand — a pure read-and-reshape over existing episodic
memory, no new write path. Deterministic sections (Queries/Tools
used/Backend) are plain aggregation; "Worked on" is a single Faramir
synthesis call, duck-typed and gracefully degrading to a literal query
list on failure, mirroring Knowledge Capsules' generate/store split
exactly (Section 10/13). Storage follows ChromaDB's precedent —
`~/.olorin/journal/<repo_hash>/`, never inside the indexed repo.
Deliberately kept as an explicit end-of-session command rather than
auto-triggered on `ask`, since (unlike index-on-demand) journal
regeneration has no hash-skip shortcut and pays a real LLM call every
time it runs. **Both the no-LLM fallback path and the live Faramir
synthesis path were verified live** against this real repo — and that
same live run caught a real bug (#13): the "Worked on" synthesis
hallucinated implementation work ("implemented the daemon deferral
mechanism") from queries that had only asked Olorin to *explain/
summarize* existing material. A content-quality bug, not a plumbing
one — everything ran without error, so it only surfaced by actually
reading the output against what really happened. Fixed at the prompt
layer, not with a suppression heuristic; one more live re-run to confirm
the fix is next session's first action.

**`ENGINEERING_JOURNAL.md` split into an archive (2026-07-12):** the
running build log passed ~1000 lines, so 2026-07-06 through 2026-07-12's
first entry were moved verbatim into `ENGINEERING_JOURNAL_ARCHIVE_01.md`
— nothing lost or changed, purely a readability split. The live
`ENGINEERING_JOURNAL.md` now starts from 2026-07-12 (continued) onward;
future sessions should share both files for full context, same as
sharing this doc.

**Cerebras provider + `web_search` tool implemented, Together AI
deliberately dropped, Faramir tool-calling gated (2026-07-13):**
researched current terms rather than trusting Section 5's stale "$25
free Together credits" claim — Together's signup credit was retired
industry-side in July 2025, now requires a $5 minimum purchase, so
`providers/together_provider.py` stays unbuilt by active decision,
project remains free-tier-only. `providers/cerebras_provider.py` fills
the second-cloud-tier role instead, wired into `LLMClient`'s
auto-routing cascade (Groq → Cerebras → local) with its own independent
circuit breaker — genuinely free but with a real 8,192-token context cap
and a churning model catalog, both handled honestly rather than assumed
away. `tools/websearch.py` implements `web_search` as a four-backend
cascade (Tavily → Jina → Serper → Exa) — corrected the same day after
Aryavart questioned the original "3,500/month combined" claim and it
turned out wrong (Serper's credits are one-time, not monthly); Jina and
Exa were added specifically to replace that lost recurring capacity,
with Exa deliberately kept card-free (opt-in, one-time-credit-only,
same free-tier-only principle as dropping Together AI). A self-hosted
alternative (SearXNG) was discussed and deliberately not built, same
daemon-deferral reasoning (Section 13). `test_faramir_tools.py` finally
closed the standing Faramir tool-calling gap Section 9 had flagged since
the very first session — **run this time, and it FAILED**: she narrated
about calling a tool instead of emitting a structured call, with no
malformed JSON for the existing fallback parser to even catch. Resolved
by gating tools away from her entirely (`core/agent.py`) rather than
chasing a fix, which turns out to match how she was already used
everywhere else in the project (Capsules/Journal are both tool-free).

**Live-verified the same session:** `python config.py` confirmed all
new keys (Cerebras, Tavily, Serper) load correctly; `python
test_faramir_tools.py` confirmed the Faramir failure directly (not
inferred). **Still pending:** the Cerebras provider and the four
web-search backends themselves haven't had a real live call yet —
config loading and the Faramir test are necessary but not sufficient
verification. Next session should run `python -m
providers.cerebras_provider`, `python -m tools.websearch`, and a real
`cli.py ask "Cerebras, ..."` plus a web-search-triggering query before
any of this gets checked off as fully proven.

**Cerebras/`web_search`/bug #14 all live-verified for real (2026-07-14):**
`python -m providers.cerebras_provider` and `python -m tools.websearch`
both confirmed working against real keys (Tavily answered on the first
try). The first real `ask "Cerebras, ..."` test caught bug #14 (direct-
address routing silently fell through to Boromir instead of calling
Cerebras) — found and fixed the same session, re-verified with a correct
run that genuinely routed to Cerebras, called tools, and produced an
accurate (if slow) final answer. See Section 13 for the full bug story.

**Complexity scorer's step-1-always-zero gap fixed (2026-07-14):**
Aryavart's observation — local models triggered far more than the free
cloud budgets justified — traced to a real structural gap: step 1 of
every query always scored complexity as zero, since `retrieved_chunks`
started empty until a tool actually ran. `Agent.run()` now seeds it with
Knowledge Capsule content (already-retrieved repo context, not query
guessing) so step 1 can route to cloud when the repo genuinely warrants
it. Deliberately scoped narrow — multi-file detection and
`COMPLEXITY_THRESHOLD` re-tuning both deferred pending live data from
this fix first. The same test run that motivated this also surfaced an
unresolved, honestly-flagged caveat: Cerebras's `gpt-oss-120b` took ~4
minutes across 5 steps for a question Boromir answers from capsules in
one, with per-call latencies suspiciously close to 60 seconds — possibly
its hidden "harmony" reasoning channel burning real time/tokens invisibly.
Not yet investigated further; worth checking real token counts before
trusting that routing more queries to Cerebras is unambiguously a win.

**Complexity scorer's step-1-always-zero gap fixed (2026-07-14):**
Aryavart's observation — local models triggered far more than the free
cloud budgets justified — traced to a real structural gap: step 1 of
every query always scored complexity as zero, since `retrieved_chunks`
started empty until a tool actually ran. `Agent.run()` now seeds it with
Knowledge Capsule content (already-retrieved repo context, not query
guessing) so step 1 can route to cloud when the repo genuinely warrants
it. Deliberately scoped narrow — multi-file detection and
`COMPLEXITY_THRESHOLD` re-tuning both deferred pending live data from
this fix first. **Verified live the same day, not just theorized:** an
auto-mode `ask` attempted Groq at step 1 for the first time ever
(structurally impossible before this fix), confirming the fix works.
Along the way, a genuinely fresh instance of bug #10's Groq malformed-
tool-call failure class (`400 tool_use_failed`) was caught cleanly by
the existing cascade and escalated to Cerebras correctly. The Cerebras-
latency question from the fix's initial discovery also got resolved to
a narrower, more useful finding: Cerebras is fast for tool-call steps
(sub-second) but consistently ~75x slower than Groq specifically on
long final-answer generation (61.6s vs. Groq's 812ms on a comparable
answer) — a real, measured routing consideration, not yet enforced in
code. Token-count logging to confirm the underlying cause was proposed
and declined (Aryavart judged the latency pattern alone sufficient
signal) — full account in Section 13.

**All four `web_search` backends live-verified individually (2026-07-15):** `test_websearch_backends.py` (repo root) calls Tavily/Jina/Serper/Exa directly, bypassing cascade order — since Tavily always wins first in normal operation, Jina/Serper/Exa had never actually been exercised live before this, only config-checked. All four returned real, correctly-shaped results on the first run against `"what is the current stable version of Python"`: Tavily (3 results, LLM-ready extracted text), Jina (3 results via `s.jina.ai`'s `data` array, confirming the defensive `.get()` parsing was correct), Serper (3 results, raw SERP snippets as expected), Exa (3 results, `highlights` field populated and correctly joined — confirming the highlights-over-full-text request shape works as documented). No response-shape mismatches found in any of the three previously-unverified backends. `tools/websearch.py` is now fully live-verified end-to-end, not just config-checked — closes the last open item from Section 11's V2 `web_search` checklist entry.

**Multi-file complexity detection built and verified (2026-07-15):** closed the half of the "better complexity scoring" item deferred the day before. `Agent._accumulate_retrieved_chunks()` now also populates `retrieved_files: set[str]`, and `score_complexity()` gained a `(distinct_files - 1) * MULTI_FILE_WEIGHT` term — both from fields (`file`/`path`) that already existed on tool results for other reasons, no new tool output shape needed. Verified at the unit level (identical query/chunks scored 6 at one file vs. 12 at three) and live (a real cross-file `ask` logged `complexity=89`). Section 11's "better complexity scoring" item is fully closed; `COMPLEXITY_THRESHOLD=15` re-tuning is the one piece still gated on accumulating real traffic.

**Entity memory designed, built, and live-verified (2026-07-16):** resolved the item's original open question directly — "object relations in a repo" (code structure, already a separate V3 item) vs. "the agent's relation to things" (persistent cross-session knowledge of decisions/concepts). Built the latter: extraction runs after every clean-finish `Agent.run()`, using the SAME backend that just answered (reuses the complexity scorer's routing decision, no new gate), with Faramir deliberately excluded (same structured-output unreliability `test_faramir_tools.py` already proved). One real dedup-matching bug found and fixed live (blended name+description ratio missed a same-decision pair with near-identical names but differently-phrased descriptions; fixed with two independent match signals). One real tradeoff surfaced and deliberately accepted, not fixed: Groq's un-set `temperature` means identical extraction prompts can return different results across calls — confirmed directly via an isolated replay (`debug_entity_extraction.py`) that returned rich candidates where a live run had returned none. Threading `temperature` through the whole provider stack to fix this was proposed and declined (a missed extraction just gets caught next time the same decision comes up) in favor of cheap permanent observability instead. Final live run committed 3 real entities end-to-end and confirmed the agent calling `search_entities` on its own in a real ReAct loop for the first time. See Section 10 for the full design and Section 13 for both real findings.

**Environment reminder for next session:** Python 3.11.9 venv in `olorin/venv`. Installed: `openai`, `python-dotenv`, `ollama`, `tree-sitter`, `tree-sitter-language-pack`, `torch` (CUDA build, cu121), `sentence-transformers`, `chromadb`, `typer`, `requests`. Rust toolchain installed via `rustup`; `indexer_core` builds clean with `cargo build --release`. Ollama models: `boromir` built on `qwen3:8b`, `faramir` on `deepseek-r1:8b`. `.env` has real keys for Groq, Cerebras, Tavily, Jina, Serper, and Exa. **Faramir's tool-calling gate, Cerebras provider, the full `web_search` cascade (all four backends), bug #14's fix, multi-file complexity detection, and entity memory are all live-verified** — no further action needed on any of those specifically. **Section 11's entire V2 tier is now closed.**

**Import graph ("why is this file important?") implemented AND live-verified 2026-07-18, V3's first item closed.** `indexer/imports.py`, `memory/import_graph.py`, `tools/importance.py` all written and wired into `cli.py`'s `_index_repo()` loop and `tools/registry.py` (seventh tool). Real `cli.py index .` run against this repo correctly extracted and stored import edges (147 chunks, 8 files reindexed); real `cli.py ask "Boromir, why is core/agent.py important?"` correctly called `file_importance` through the actual agent loop and produced an accurate, fact-checkable answer. One real bug (#15) found by that live run and fixed same-day — see Section 13 for the full story.

**Git integration (`git diff` → agent explains what changed) implemented 2026-07-18, V3's second item — one real bug (#16) found by the first live smoke test and fixed same-day; the two full agent-loop verification steps are still outstanding.** `tools/git_diff.py` built, wired into `tools/registry.py` (8th tool) and `core/agent.py`'s system prompt ("eight tools"). `python -m tools.git_diff` run against this real repo immediately surfaced a real truncation bug: `files_changed=12` but `files_shown=0`, because the loop used `break` instead of `continue` when a file didn't fit the `max_chars` budget — `ENGINEERING_JOURNAL.md` (alphabetically first, huge after a full day of doc work) hit the cap and silently starved every smaller file behind it, even ones that would have fit easily on their own. Reproduced in an isolated sandbox with a synthetic huge-file-first repo, fixed by changing `break` to `continue`, re-verified against both the new reproduction case and the tool's original test suite. See bug #16 in this section's numbered list and Section 13 for the full story. **Still open, by design (Aryavart hasn't committed since V1, so a clean before/after test needs a real commit first):** (1) a real `cli.py ask` that triggers `git_diff` through the actual agent loop; (2) a ref-based query (e.g. against a specific commit) to confirm the non-default path. **First actions next session, in order:** (1) the two `git_diff` agent-loop verification steps above, once ready; (2) once there's a reasonable amount of real traffic logged, revisit `COMPLEXITY_THRESHOLD=15` with actual data instead of the original guess; (3) V3's remaining items — GPU benchmark table, document ingestion — are unstarted and well-specified; searxNcrawl remains correctly deferred pending measured need.

---

*Document version: 2.0 — July 2026*  
*Continue in new chat by sharing this file (and `ENGINEERING_JOURNAL.md` for full session-by-session detail)*
