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
| **Groq** | console.groq.com | 1,000 RPD, 100K TPD (llama-3.3-70b) | V1 — implement now |
| **Together AI** | api.together.xyz | $25 free credits on signup | V2 — stub in V1 |
| **Cerebras** | cloud.cerebras.ai | Free tier, no card | V2 — stub in V1 |

**Groq free tier reality for `llama-3.3-70b-versatile`:**
- 30 RPM, 1,000 requests/day, 12,000 TPM, 100,000 TPD
- ~25 substantial queries/day (at ~4,000 tokens/query)
- With smart local-first routing: effectively 6+ heavy sessions/day
- 429 errors handled by circuit breaker → cascade to local

**Groq risk note:** Following NVIDIA's $20B deal (Dec 2025), ~90% of Groq engineering moved to NVIDIA. GroqCloud continues operating but long-term trajectory is uncertain. The provider abstraction in `LLMClient` means swapping providers is a one-line change.

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
│   ├── together_provider.py   # Together AI (stub, V2)
│   └── cerebras_provider.py   # Cerebras (stub, V2)
├── tools/
│   ├── __init__.py
│   ├── registry.py            # Tool definitions in OpenAI JSON schema
│   ├── search.py              # search_codebase tool
│   └── reader.py              # read_file, list_files tools
├── indexer/
│   ├── __init__.py
│   ├── chunker.py             # tree-sitter AST chunking
│   ├── embedder.py            # sentence-transformers (GPU)
│   └── store.py               # ChromaDB read/write
├── indexer_core/              # Rust file-walker
│   ├── Cargo.toml
│   └── src/
│       └── main.rs
├── memory/
│   ├── __init__.py
│   ├── cache.py               # SQLite query cache
│   ├── history.py             # Episodic conversation memory
│   └── entities.py            # Entity memory (repos, people, concepts)
├── config.py                  # Keys, model names, paths, thresholds
├── cli.py                     # Entry point (typer-based)
├── requirements.txt
├── OLORIN.md                  # Per-repo config (loaded per project)
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

### Provider cascade (V1)
```
Groq (llama-3.3-70b-versatile)
    ↓ 429 / timeout / offline / circuit open
Ollama local_model (boromir or faramir)
    ↓ always available
```

### Provider cascade (V2, when stubs are implemented)
```
Groq → Together AI → Cerebras → Ollama local
```

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
python cli.py ask "..." --provider ollama
python cli.py ask "..." --provider auto   # default
python cli.py ask "..." --force-local     # never call cloud
```

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

### GPU embeddings
```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer(
    "BAAI/bge-small-en-v1.5",
    device="cuda"  # RTX 4060 — several times faster than CPU
)
```

### OLORIN.md (per-repo config)
Each repo can have an `OLORIN.md` file at its root. Loaded automatically at session start.

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
| `list_files` | `(pattern: str = "*")` | List repo files by extension/pattern |

### V2/V3 Tool: `web_search`
| Tool | Signature | What it does |
|---|---|---|
| `web_search` | `(query: str, n_results: int = 5)` | Live web search, available to **any** backend (Groq, Boromir, Faramir) |

**Why this exists:** Local models (Faramir especially) hallucinate confidently on open-knowledge questions outside their training data or parametric memory — confirmed by direct testing (deepseek-r1:8b fabricated plausible-sounding but incorrect details about Faramir/Boromir's relationship in Tolkien's text when asked from memory with no retrieval context). Adding `web_search` as a standard tool closes that gap for **all three agents equally** — not just Groq.

**Key clarification:** this is not a cost-saving lever against Groq usage. The complexity scorer (Section 7) routes on retrieval size / query complexity, which is orthogonal to whether a question needs live facts. Groq's `llama-3.3-70b-versatile` has no inherent internet access either — without this tool it's just as capable of confidently getting live facts wrong as Faramir is. Giving *all* backends the same tool means local models can match or beat an ungrounded Groq call on live-fact questions, and everything still degrades gracefully to trained-knowledge-only when offline.

**Known risk — reasoning-model tool calling:** `deepseek-r1:8b` wraps output in `<think>...</think>` before its final answer, which can break tool-call parsing that expects clean structured output (the model may narrate *about* calling a tool inside its reasoning instead of emitting an actual tool-call token). Qwen-based Boromir is fine-tuned for function calling and doesn't have this issue. **Action item:** test Faramir's raw tool-calling reliability with a trivial forced tool-call prompt before wiring `web_search` (or any tool) into its agent loop — do not assume parity with Boromir.

**Placement:** deliberately deferred out of V1. V1's story is codebase understanding via RAG (Section 14); a general-knowledge tool is a second product surface bolted onto the first, and belongs once the ReAct loop + tool registry pattern already exists and is proven (V2/V3), not while that foundation is still being built.

---

## 10. Memory System

### Episodic memory (SQLite)
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

### Entity memory (SQLite)
```sql
CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    type TEXT,            -- "repo" | "file" | "person" | "concept"
    name TEXT,
    description TEXT,
    metadata TEXT,        -- JSON
    last_updated TEXT
);
```

### Engineering Journal (auto-generated)
Every session writes `journal/YYYY-MM-DD.md`:
```markdown
# 2026-07-05

## Worked on
- Olorin agent loop

## Queries
- "what does the auth module do?" → found auth.py middleware chain
- "where are database connections handled?" → src/db/connection.py

## Tools used
- search_codebase (4x), read_file (2x)

## Backend
- Groq (2 queries), Boromir (6 queries)
```

---

## 11. Build Roadmap

### V1 — It works
- [x] `config.py` + `.env` setup
- [x] `BaseProvider` interface
- [x] `GroqProvider` (working — verified live against llama-3.3-70b-versatile)
- [x] `OllamaProvider` (working — verified live against both Boromir and Faramir)
- [ ] `TogetherProvider` (stub)
- [ ] `CerebrasProvider` (stub)
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
- [ ] Episodic memory (SQLite conversations)
- [ ] OLORIN.md per-repo config loading
- [ ] Engineering Journal (auto session logging)
- [ ] Entity memory (repo/file/concept tracking)
- [ ] Together AI + Cerebras providers (implemented, not just stubs)
- [ ] Better complexity scoring (multi-file detection, planning intent)
- [ ] `python cli.py ask "what did I figure out about auth last week?"` works
- [ ] Test `deepseek-r1:8b` (Faramir) raw tool-calling reliability before adding tools to its loop
- [ ] `web_search` tool added to registry, available to all three backends (offline-safe fallback to trained knowledge)

### V3 — It's fast and deep
- [ ] GPU embeddings properly benchmarked + README benchmark table
- [ ] Knowledge Capsules (pre-computed module summaries loaded before retrieval)
- [ ] "Why is this file important?" (import graph analysis, reference frequency)
- [ ] Git integration: `git diff` → agent explains what changed
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

**Seven real bugs found and fixed at the correct architectural layer across this project so far** (full stories in Section 13 and `ENGINEERING_JOURNAL.md`):
1. Ollama's OpenAI-compat endpoint silently failing tool calls (fixed by switching to native API), plus a follow-on message-schema mismatch (fixed with bidirectional translation) — both fully contained in `providers/ollama_provider.py`.
2. The `ignore` crate's `require_git` default silently disabling `.gitignore` outside an actual git repo.
3. A `.gitignore` file written with literal quote characters wrapped around every pattern, making every rule a silent no-op — same external symptom as bug #2, different root cause.
4. The Rust walker emitting absolute paths instead of the documented repo-relative contract — surfaced only once `cli.py` started passing an already-absolute repo path in; fixed by canonicalizing + stripping the prefix in `main.rs`.
5. The Ollama fallback tool-call parser only recognizing bare JSON, missing a markdown-fenced variant Boromir produced on a real `search_codebase` call — fixed by stripping the fence before the existing parse check.
6. The V1.5 profiling report itself double/triple/quadruple-counting nested spans (a single Ollama call recorded four overlapping ways, `total_request` summed on top of everything it already contained) — produced a nonsensical negative "unaccounted" number; fixed with an explicit top-level-vs-nested distinction and a true non-overlapping Ollama duration partition.
7. `sentence-transformers` silently pinging Hugging Face Hub on every embedder load despite a fully local cache — a real, measured 5.8s cost; fixed with `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`.

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

**Next up: V2 features** — episodic memory, OLORIN.md per-repo config
loading, Engineering Journal automation, entity memory. See Section 11's
V2 tier.

**Environment reminder for next session:** Python 3.11.9 venv in `olorin/venv`. Installed: `openai`, `python-dotenv`, `ollama`, `tree-sitter`, `tree-sitter-language-pack`, `torch` (CUDA build, cu121), `sentence-transformers`, `chromadb`, `typer`. Rust toolchain installed via `rustup`; `indexer_core` builds clean with `cargo build --release`. Ollama models: `boromir` now built on `qwen3:8b`, `faramir` on `deepseek-r1:8b`. Nothing further needs installing — next session is V2 feature work (episodic memory / OLORIN.md / journal automation), not new dependencies.

---

*Document version: 1.6 — July 2026*  
*Continue in new chat by sharing this file (and `ENGINEERING_JOURNAL.md` for full session-by-session detail)*
