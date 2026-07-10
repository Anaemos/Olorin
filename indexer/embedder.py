# indexer/embedder.py — GPU-accelerated chunk embeddings via sentence-transformers.
#
# Takes the chunk dicts produced by indexer/chunker.py and attaches a dense
# vector embedding to each one, ready for ChromaDB storage. Uses BAAI's
# bge-small-en-v1.5 — small enough (~130MB) to fit comfortably in the
# RTX 4060's 8GB VRAM budget alongside a resident Ollama model (see
# OLORIN_PROJECT.md Section 3's VRAM budget table).

from __future__ import annotations

import os

# HF_HUB_OFFLINE (must be set before sentence_transformers/huggingface_hub
# import, since some of their internal config is read at import time, not
# just at call time): by default, sentence-transformers pings Hugging Face
# Hub on every model load to check for a newer revision, even when the
# model is already fully cached locally. Confirmed as a real, measurable
# cost via the V1.5 profiling work (OLORIN_PROJECT.md Section 11) — a
# live --profile run showed embedder_init taking 5.8s, dwarfing the
# ~1-2s this module's own docstring/comments expected, correlating
# exactly with the "unauthenticated requests to the HF Hub" warning.
# bge-small-en-v1.5 is already downloaded (verified live back on
# 2026-07-08), so forcing offline/local-cache-only loading is a real fix,
# not a workaround — the network check was never buying anything once
# the model is cached. TRADE-OFF, stated explicitly rather than hidden:
# if the local HF cache is ever cleared, loading will fail loudly
# (a clear, debuggable error) instead of silently re-downloading —
# acceptable for a local-first tool where the model is a one-time setup
# step, not something expected to change under us.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import sys
import time

import torch
from sentence_transformers import SentenceTransformer

from core import profiling

MODEL_NAME = "BAAI/bge-small-en-v1.5"

# BGE's asymmetric retrieval convention: query text gets a fixed
# instruction prefix, passage/document text does not. This isn't optional
# styling — omitting it on the query side measurably hurts retrieval
# quality for BGE models specifically, since it's how the model was
# fine-tuned. Getting this backwards (or applying it to both sides, or
# neither) is a common, silent correctness bug in BGE-based RAG systems —
# nothing errors, results just get quietly worse.
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

_model: SentenceTransformer | None = None


def _get_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    # Deliberately not a hard failure — GPU embedding is the design goal
    # (Section 12: "GPU (CUDA), ~130MB"), but Olorin should still work,
    # just slower, if CUDA torch wasn't installed correctly or the
    # machine has no GPU. Fail soft, log loud — a silent CPU fallback
    # would be a confusing performance regression to debug later.
    print(
        "WARNING: CUDA not available — falling back to CPU embeddings. "
        "If you expected GPU, check that torch was installed with "
        "--index-url https://download.pytorch.org/whl/cu121 "
        "(see OLORIN_PROJECT.md Section 16).",
        file=sys.stderr,
    )
    return "cpu"


def get_model() -> SentenceTransformer:
    """Lazily loads and caches the embedding model. Loading is the slow
    part (~1-2s even on GPU) — every call site should share this one
    instance rather than reloading per-file during a full repo index."""
    global _model
    if _model is None:
        device = _get_device()
        with profiling.span("embedder_init"):
            _model = SentenceTransformer(MODEL_NAME, device=device)
    return _model


def embed_chunks(chunks: list[dict], batch_size: int = 32) -> list[dict]:
    """
    Embeds a list of chunk dicts (as produced by indexer/chunker.py) in
    batches, attaching an "embedding" key to each in place. Batching
    matters more here than it would on CPU — GPU throughput is dominated
    by keeping the pipeline fed, not per-item call overhead.

    No query instruction prefix is applied here — these are documents/
    passages, not search queries. See embed_query() for the query side.
    """
    if not chunks:
        return []

    model = get_model()
    texts = [c["content"] for c in chunks]

    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,  # lets ChromaDB use dot product as cosine similarity
    )

    for chunk, embedding in zip(chunks, embeddings):
        chunk["embedding"] = embedding.tolist()

    return chunks


def embed_query(query: str) -> list[float]:
    """
    Embeds a search query — NOT a code chunk. Applies BGE's required
    query instruction prefix. Kept as a separate function from
    embed_chunks() rather than a boolean flag, so it's structurally
    impossible to accidentally embed a document with the query prefix,
    or a query without one.
    """
    model = get_model()
    with profiling.span("query_embedding"):
        embedding = model.encode(
            QUERY_INSTRUCTION + query,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
    return embedding.tolist()


if __name__ == "__main__":
    # Smoke test: python -m indexer.embedder
    # Loads the model, embeds a couple of fake chunks + a query, reports
    # device used, embedding dimensionality, and timing — enough to catch
    # a silent CPU fallback or a broken install without needing real data.
    test_chunks = [
        {
            "content": "def authenticate(user, pw):\n    return check_password(user, pw)",
            "type": "function", "name": "authenticate", "file": "auth.py",
            "language": "python", "lines": "1-2", "repo": ".",
        },
        {
            "content": "class Config:\n    def __init__(self):\n        self.debug = False",
            "type": "class", "name": "Config", "file": "config.py",
            "language": "python", "lines": "1-3", "repo": ".",
        },
    ]

    device = _get_device()
    print(f"Device: {device}")

    start = time.time()
    result = embed_chunks(test_chunks)
    elapsed = time.time() - start
    print(f"Embedded {len(result)} chunks in {elapsed:.3f}s")
    print(f"Embedding dimensionality: {len(result[0]['embedding'])}")

    q_start = time.time()
    q_embedding = embed_query("how does authentication work?")
    q_elapsed = time.time() - q_start
    print(f"Embedded 1 query in {q_elapsed:.3f}s, dim {len(q_embedding)}")
