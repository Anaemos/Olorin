"""
benchmark_embeddings.py — GPU embedding benchmark (V3, "GPU embeddings
properly benchmarked + README benchmark table", OLORIN_PROJECT.md
Section 11).

Standalone diagnostic script, same category as test_faramir_tools.py/
test_websearch_backends.py/debug_entity_extraction.py — run directly,
not imported. Produces real numbers for the "GPU vs CPU inference
tradeoffs" line item already listed in Section 15's "Concepts You Can
Explain in an Interview" table, and closes V3's one remaining unstarted
roadmap item.

Deliberately doesn't reuse indexer/embedder.py's get_model() singleton —
that cache always returns whatever device _get_device() picked once, at
first call. Benchmarking needs explicit control over GPU vs CPU within
the same run, so this script constructs its own SentenceTransformer
instances directly instead of fighting that caching design.

Usage: python benchmark_embeddings.py
Takes roughly 1-3 minutes total (model loads twice — once per device —
plus the actual timed runs). Prints a markdown table at the end, ready
to paste directly into a README or OLORIN_PROJECT.md.
"""

from __future__ import annotations

import os
import statistics
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import torch
from sentence_transformers import SentenceTransformer

MODEL_NAME = "BAAI/bge-small-en-v1.5"
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

# Realistic chunk-sized text, not toy strings — representative of what
# a real search_codebase chunk actually looks like (see indexer/
# embedder.py's MAX_CHUNK_CHARS=2000), since throughput on tiny 20-char
# strings wouldn't be representative of real indexing workloads.
_SAMPLE_CHUNK = '''
def score_complexity(query: str, retrieved_chunks: list[str] | None = None, retrieved_files: set[str] | list[str] | None = None) -> int:
    """
    Post-retrieval complexity scorer. Deliberately scores AFTER retrieval,
    not on the raw query alone. "Explain auth" sounds trivial as a string,
    but if retrieval surfaces 18 files and 8 middleware layers, the
    retrieved context is the real signal of difficulty, not the five
    words the user typed. Returns a raw score; the caller compares it
    against config.COMPLEXITY_THRESHOLD to decide whether to escalate to
    a cloud provider or stay local.
    """
    retrieved_chunks = retrieved_chunks or []
    retrieved_files = retrieved_files or []
    score = 0
    score += len(retrieved_chunks) * 2
    score += sum(len(c) for c in retrieved_chunks) // 500
    score += len(query.split()) // 10
    lowered = query.lower()
    score += sum(2 for w in PLANNING_WORDS if w in lowered)
    distinct_files = len(set(retrieved_files))
    if distinct_files > 1:
        score += (distinct_files - 1) * MULTI_FILE_WEIGHT
    return score
'''.strip()

_SAMPLE_QUERY = "how does the complexity scorer decide when to escalate to a cloud provider?"

BATCH_SIZES = [1, 8, 32, 128]
QUERY_TRIALS = 10


def _load_model(device: str) -> SentenceTransformer:
    print(f"Loading {MODEL_NAME} on {device}...")
    start = time.perf_counter()
    model = SentenceTransformer(MODEL_NAME, device=device)
    elapsed = time.perf_counter() - start
    print(f"  loaded in {elapsed:.2f}s")
    return model, elapsed


def _benchmark_batch_encode(model: SentenceTransformer, batch_size: int, n_chunks: int) -> dict:
    texts = [_SAMPLE_CHUNK] * n_chunks
    # One untimed warmup pass — first call on a fresh model/device pays a
    # one-time CUDA kernel compilation / cache-warming cost that isn't
    # representative of steady-state throughput. Excluded from the timed
    # result deliberately, same reasoning as any GPU benchmark's warmup
    # convention.
    model.encode(texts[:min(4, n_chunks)], batch_size=batch_size, show_progress_bar=False, convert_to_numpy=True)

    start = time.perf_counter()
    model.encode(texts, batch_size=batch_size, show_progress_bar=False, convert_to_numpy=True, normalize_embeddings=True)
    elapsed = time.perf_counter() - start

    return {
        "batch_size": batch_size,
        "n_chunks": n_chunks,
        "elapsed_s": elapsed,
        "chunks_per_sec": n_chunks / elapsed if elapsed > 0 else float("inf"),
    }


def _benchmark_query_latency(model: SentenceTransformer) -> dict:
    # Warmup, same reasoning as above.
    model.encode(QUERY_INSTRUCTION + _SAMPLE_QUERY, convert_to_numpy=True)

    latencies_ms = []
    for _ in range(QUERY_TRIALS):
        start = time.perf_counter()
        model.encode(QUERY_INSTRUCTION + _SAMPLE_QUERY, convert_to_numpy=True, normalize_embeddings=True)
        latencies_ms.append((time.perf_counter() - start) * 1000)

    return {
        "mean_ms": statistics.mean(latencies_ms),
        "min_ms": min(latencies_ms),
        "max_ms": max(latencies_ms),
        "stdev_ms": statistics.stdev(latencies_ms) if len(latencies_ms) > 1 else 0.0,
    }


def run_benchmark(device: str) -> dict:
    model, load_s = _load_model(device)

    batch_results = []
    for bs in BATCH_SIZES:
        # n_chunks scales with batch_size so every configuration processes
        # at least a few full batches, not a single partial one — a
        # batch_size=128 run given only 128 chunks would show mostly
        # fixed overhead, not steady-state throughput.
        n_chunks = max(bs * 4, 32)
        print(f"  encoding {n_chunks} chunks at batch_size={bs}...")
        result = _benchmark_batch_encode(model, bs, n_chunks)
        batch_results.append(result)
        print(f"    {result['chunks_per_sec']:.1f} chunks/sec")

    print("  benchmarking query embedding latency...")
    query_result = _benchmark_query_latency(model)
    print(f"    mean {query_result['mean_ms']:.1f}ms (min {query_result['min_ms']:.1f}, max {query_result['max_ms']:.1f})")

    return {
        "device": device,
        "load_s": load_s,
        "batch_results": batch_results,
        "query_result": query_result,
    }


def print_markdown_table(results: list[dict]) -> None:
    print("\n" + "=" * 70)
    print("MARKDOWN TABLE (paste directly into README.md / OLORIN_PROJECT.md)")
    print("=" * 70 + "\n")

    print(f"**Embedding model:** `{MODEL_NAME}` (384-dim, ~130MB)")
    print(f"**Hardware:** see OLORIN_PROJECT.md Section 3\n")

    print("| Device | Model load | Batch size | Chunks/sec | Query embed (mean) |")
    print("|---|---|---|---|---|")
    for r in results:
        for i, b in enumerate(r["batch_results"]):
            load_col = f"{r['load_s']:.2f}s" if i == 0 else ""
            query_col = f"{r['query_result']['mean_ms']:.1f}ms" if i == 0 else ""
            device_col = r["device"].upper() if i == 0 else ""
            print(f"| {device_col} | {load_col} | {b['batch_size']} | {b['chunks_per_sec']:.1f} | {query_col} |")

    if len(results) == 2:
        gpu_best = max(r["chunks_per_sec"] for r in results[0]["batch_results"])
        cpu_best = max(r["chunks_per_sec"] for r in results[1]["batch_results"])
        if cpu_best > 0:
            speedup = gpu_best / cpu_best
            print(f"\n**GPU speedup at best batch size: {speedup:.1f}x** ({gpu_best:.1f} vs {cpu_best:.1f} chunks/sec)")


if __name__ == "__main__":
    print("Olorin embedding benchmark (V3, OLORIN_PROJECT.md Section 11)")
    print("=" * 70)

    all_results = []

    if torch.cuda.is_available():
        print(f"\nCUDA device: {torch.cuda.get_device_name(0)}")
        print("\n--- GPU ---")
        all_results.append(run_benchmark("cuda"))
    else:
        print("\nCUDA not available on this machine — skipping GPU benchmark.")

    print("\n--- CPU (forced, for comparison) ---")
    all_results.append(run_benchmark("cpu"))

    print_markdown_table(all_results)
