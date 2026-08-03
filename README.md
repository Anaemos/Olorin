# Olorin

A local-first engineering assistant with persistent project memory. Olorin indexes your codebases, reads your documents, remembers your decisions, and lets you query all of it through natural conversation — fully offline when needed, frontier-grade when online.

Named after Gandalf's true Valinórean name — the scholar form of Gandalf, who spent his time observing and learning quietly before carrying a staff. The version that *knows*, not the version that performs.

**Not a chatbot wrapper. Not another RAG project.** A system with real architectural decisions at every level: provider abstraction, AST-aware indexing, post-retrieval routing, persistent memory, and a Rust-accelerated pipeline. Full design rationale — every decision, every bug found and fixed, every trade-off considered and rejected — lives in [`OLORIN_PROJECT.md`](./OLORIN_PROJECT.md) and `ENGINEERING_JOURNAL.md` (plus its archive chain, `ENGINEERING_JOURNAL_ARCHIVE_01.md` → `_03.md`).

## What it does

- Indexes a codebase with a Rust-parallelized file walker + tree-sitter AST-aware chunking
- Answers questions about that code via a hand-written ReAct agent loop, routing between a frontier cloud model (Groq) and a local model (Ollama) based on query complexity — post-retrieval, not on the raw query text
- Remembers past conversations, tracks decisions and concepts across sessions, and gives every file an import graph so it can explain *why* a file matters, not just what it contains
- Explains `git diff` output, ingests PDFs into the same searchable index as your code, crawls a full linked page when a search snippet isn't enough, and can search across multiple repos at once
- Runs entirely offline when it needs to, and degrades gracefully everywhere — every honest error tells you what to do next instead of failing silently

## Architecture, in one paragraph

> *"Olorin is a local-first engineering assistant with persistent project memory. It indexes codebases using a Rust-parallelized file walker and tree-sitter AST chunking, stores embeddings in ChromaDB with GPU acceleration, and routes queries through a raw ReAct agent loop to Groq's frontier models online or a specialist local model offline — one Ollama checkpoint, two personas (Boromir for code, Faramir for reasoning), differentiated at the application layer rather than as separate models. It maintains episodic memory, entity memory, and Knowledge Capsules — pre-computed structural summaries of key modules — so answers come from understanding, not just retrieval. Built iteratively: raw agent loop first to understand the mechanics, and deliberately kept hand-rolled through V4 rather than migrated to a framework — by the time a LangGraph refactor was on the table, the loop had accumulated enough carefully-tuned, live-verified routing and memory logic that the migration's only real payoff was narrative, not functional, so I said no and documented why."*

A raw ReAct agent loop — no LangChain, no LangGraph, every line explainable — routes between **Olorin** (Groq `openai/gpt-oss-120b`, frontier reasoning), **Boromir** (local, fast tool execution), and **Faramir** (local, reasoning/synthesis) using a post-retrieval complexity scorer, with independent circuit breakers per cloud provider and a Cerebras second-tier fallback. Boromir and Faramir are the same loaded Ollama checkpoint (`local:latest`, `qwen3:8b`-based) — differentiated by the `think` flag, sampling params, and system-prompt voice, not by separate model files. Code gets indexed via a Rust file walker into tree-sitter AST chunks, embedded with `bge-small-en-v1.5` on GPU, and stored in ChromaDB. See [`OLORIN_PROJECT.md`](./OLORIN_PROJECT.md) for the complete design document.

## Prerequisites

- **Python 3.11+** (built and verified against 3.11.9)
- **[Ollama](https://ollama.com)**, installed and on `PATH` — runs the local model
- **Rust toolchain** ([rustup.rs](https://rustup.rs)) — to build `indexer_core`, the Rust file walker. Or skip this: grab the prebuilt `indexer_core.exe` from [Releases](https://github.com/Anaemos/Olorin/releases) instead of building from source.
- **An NVIDIA GPU with CUDA** — optional. Embeddings fall back to CPU automatically if none is found (slower, not broken — see `indexer/embedder.py`'s `_get_device()`)
- **A free [Groq](https://console.groq.com) API key** — the only required cloud credential. Cerebras, Tavily, Jina, Serper, and Exa are all optional, free-tier, no-card-required extras (see `OLORIN_PROJECT.md` Section 5)

## Quickstart

```powershell
git clone <this-repo>
cd olorin
.\setup.ps1
```

`setup.ps1` creates the venv, installs `requirements.txt`, checks for/pulls the Ollama model, and prompts for your API keys to write `.env`. It's safe to re-run — every step checks before acting.

Manual equivalent, if you'd rather see each step:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium          # needed for the crawl_page tool

# Ollama model (see OLORIN_PROJECT.md Section 4 for the full walkthrough)
ollama pull qwen3:8b
ollama show qwen3:8b --modelfile > local.modelfile
# add "PARAMETER num_ctx 16384" to local.modelfile, then:
ollama create local:latest -f local.modelfile

# Rust file walker
cd indexer_core
cargo build --release
cd ..
# or skip this entirely: download indexer_core.exe from
# https://github.com/Anaemos/Olorin/releases and place it at
# indexer_core\target\release\indexer_core.exe

# .env — GROQ_API_KEY is required, everything else is optional
echo "GROQ_API_KEY=your_key_here" > .env
```

Then:

```powershell
python cli.py index <path>              # index a repo
python cli.py ask "<question>"          # ask about it
python cli.py ask "<question>" --repos <other-repo>   # ask across multiple repos
```

`ask` runs indexing automatically as a cheap freshness check before every query, so in practice you rarely need to call `index` yourself — see `OLORIN_PROJECT.md` Section 8.

## Embedding benchmark

GPU embeddings via `sentence-transformers`, `bge-small-en-v1.5` (384-dim, ~130MB), measured on an NVIDIA RTX 4060 Laptop GPU (8GB VRAM) — see [`OLORIN_PROJECT.md`](./OLORIN_PROJECT.md) Section 3 for the full hardware profile. Run it yourself with `python benchmark_embeddings.py`.

| Device | Model load | Batch size | Chunks/sec | Query embed (mean) |
|---|---|---|---|---|
| CUDA | 0.95s | 1 | 157.8 | 8.0ms |
| | | 8 | 325.6 | |
| | | 32 | 376.4 | |
| | | 128 | **381.2** | |
| CPU | 0.10s | 1 | 23.5 | 15.1ms |
| | | 8 | 27.5 | |
| | | 32 | **28.7** | |
| | | 128 | 27.3 | |

**GPU speedup at best batch size: 13.3x** (381.2 vs. 28.7 chunks/sec).

Two things worth calling out:

- **Batching matters far more on GPU than CPU.** GPU throughput scales from 157.8 → 381.2 chunks/sec (2.4x) between batch size 1 and 128, since the bottleneck there is keeping the pipeline fed, not per-item overhead. CPU throughput is essentially flat (23.5 → 28.7 chunks/sec) across the same range — there's no parallel pipeline to feed. This is why `indexer/embedder.py` batches chunk embedding during indexing rather than embedding one chunk at a time.
- **Query embedding latency (8.0ms GPU vs. 15.1ms CPU) matters more than the raw throughput numbers for interactive use.** Every `ask` embeds exactly one query, on the hot path between the user's question and the first tool call — this is the number that actually affects how snappy the tool feels to use, not batch throughput (which only matters during `index`).

CUDA model load (0.95s) is *slower* than CPU load (0.10s) here — expected, and not a regression: that's the one-time cost of moving weights onto the GPU and initializing the CUDA context, paid once per process (see `indexer/embedder.py`'s `get_model()` singleton, which ensures this happens exactly once per indexing/query session, not per chunk).

## Full documentation

- [`OLORIN_PROJECT.md`](./OLORIN_PROJECT.md) — complete design document: architecture, every design decision and why, full build roadmap
- `ENGINEERING_JOURNAL.md` (+ its archive chain, `ENGINEERING_JOURNAL_ARCHIVE_01.md` → `_03.md`) — session-by-session build log, including every bug found and how it was fixed
