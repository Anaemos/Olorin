# Olorin

A local-first engineering assistant with persistent project memory. Olorin indexes your codebases, reads your documents, remembers your decisions, and lets you query all of it through natural conversation — fully offline when needed, frontier-grade when online.

Named after Gandalf's true Valinórean name — the scholar form of Gandalf, who spent his time observing and learning quietly before carrying a staff. The version that *knows*, not the version that performs.

**Not a chatbot wrapper. Not another RAG project.** A system with real architectural decisions at every level: provider abstraction, AST-aware indexing, post-retrieval routing, persistent memory, and a Rust-accelerated pipeline. Full design rationale — every decision, every bug found and fixed, every trade-off considered and rejected — lives in [`OLORIN_PROJECT.md`](./OLORIN_PROJECT.md) and [`ENGINEERING_JOURNAL.md`](./ENGINEERING_JOURNAL.md).

## What it does

- Indexes a codebase with a Rust-parallelized file walker + tree-sitter AST-aware chunking
- Answers questions about that code via a hand-written ReAct agent loop, routing between a frontier cloud model (Groq) and local models (Ollama) based on query complexity — post-retrieval, not on the raw query text
- Remembers past conversations, tracks decisions and concepts across sessions, and gives every file an import graph so it can explain *why* a file matters, not just what it contains
- Explains `git diff` output, ingests PDFs into the same searchable index as your code, and can search across multiple repos at once
- Runs entirely offline when it needs to, and degrades gracefully everywhere — every honest error tells you what to do next instead of failing silently

## Architecture, in one paragraph

Three LLM personas (Lord of the Rings naming, because why not): **Olorin** (Groq `llama-3.3-70b-versatile`, frontier reasoning), **Boromir** (local `qwen3:8b`, fast tool execution), **Faramir** (local `deepseek-r1:8b`, reasoning/synthesis). A raw ReAct agent loop — no LangChain, no LangGraph, every line explainable — routes between them using a post-retrieval complexity scorer, with independent circuit breakers per cloud provider and a Cerebras second-tier fallback. Code gets indexed via a Rust file walker into tree-sitter AST chunks, embedded with `bge-small-en-v1.5` on GPU, and stored in ChromaDB. See [`OLORIN_PROJECT.md`](./OLORIN_PROJECT.md) for the complete design document.

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

## Setup

See [`OLORIN_PROJECT.md`](./OLORIN_PROJECT.md) Sections 4–5 for full Ollama model setup and API key requirements (Groq is required; Cerebras, Tavily, Jina, Serper, and Exa are all optional, free-tier, no-card-required fallbacks).

```bash
python cli.py index <path>              # index a repo
python cli.py ask "<question>"          # ask about it
python cli.py ask "<question>" --repos <other-repo>   # ask across multiple repos
```

## Full documentation

- [`OLORIN_PROJECT.md`](./OLORIN_PROJECT.md) — complete design document: architecture, every design decision and why, full build roadmap
- [`ENGINEERING_JOURNAL.md`](./ENGINEERING_JOURNAL.md) — session-by-session build log, including every bug found and how it was fixed (eighteen so far)
