"""
memory/cache.py — SQLite query cache.

Implements the cache schema and key strategy from OLORIN_PROJECT.md
Section 7 exactly:

  key = sha256(query + context_hash + model_name)
  TTL: 24 hours
  Invalidated automatically if the repo is re-indexed, because that
  changes context_hash, which changes the key — no explicit
  invalidation logic needed, it's a natural consequence of the key design.

V1 SCOPE NOTE: caching only applies to tool-free responses. A response
that involved tool calls is inherently tied to live state (file contents,
search results) at the moment it ran — caching that would risk serving
stale tool output as if it were fresh. This module doesn't enforce that
rule itself; LLMClient decides whether to consult the cache before calling
in and only calls get()/set() for tool-free requests.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import config

CACHE_TTL_HOURS = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_cache (
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
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.MEMORY_DB_PATH)
    conn.execute(_SCHEMA)
    return conn


def compute_context_hash(retrieved_chunks: Optional[list[str]]) -> str:
    """
    Hashes the retrieved context so the cache key changes automatically
    whenever the underlying repo is re-indexed and retrieval results
    change — this is what makes cache invalidation "free" per Section 7,
    no explicit invalidation logic required.
    """
    retrieved_chunks = retrieved_chunks or []
    joined = "||".join(retrieved_chunks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_key(query: str, context_hash: str, model_name: str) -> str:
    raw = f"{query}{context_hash}{model_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(key: str) -> Optional[dict]:
    """
    Returns the cached row as a dict if present AND within TTL, else None.
    Expired-but-present rows are treated as a miss (not deleted here —
    a lazy cleanup pass could sweep those later; not worth the complexity
    in V1).
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM query_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None

        columns = [d[0] for d in conn.execute(
            "SELECT * FROM query_cache WHERE key = ?", (key,)
        ).description]
        record = dict(zip(columns, row))

        cached_at = datetime.fromisoformat(record["timestamp"])
        if datetime.now() - cached_at > timedelta(hours=CACHE_TTL_HOURS):
            return None  # expired — treat as miss

        return record
    finally:
        conn.close()


def set(
    key: str,
    query: str,
    answer: str,
    provider: str,
    model: str,
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
    retrieved_files: Optional[list[str]],
    context_hash: str,
) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO query_cache
                (key, query, answer, provider, model, latency_ms,
                 input_tokens, output_tokens, retrieved_files,
                 context_hash, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key, query, answer, provider, model, latency_ms,
                input_tokens, output_tokens,
                json.dumps(retrieved_files or []),
                context_hash, datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    # Manual smoke test: python -m memory.cache
    ctx_hash = compute_context_hash(["def foo(): pass"])
    key = compute_key("what does foo do?", ctx_hash, "boromir")

    print(f"Cache miss check: {get(key)}")  # should be None first run

    set(
        key=key, query="what does foo do?", answer="It does nothing.",
        provider="ollama", model="boromir", latency_ms=1234,
        input_tokens=10, output_tokens=5,
        retrieved_files=["src/foo.py"], context_hash=ctx_hash,
    )

    print(f"Cache hit check: {get(key)}")  # should return the row now
