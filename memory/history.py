"""
memory/history.py — Episodic memory (SQLite), Section 10 of OLORIN_PROJECT.md.

Implements the `conversations` table exactly as specified: every agent run
gets logged with its query, final answer, which tools fired, which backend
answered, and the post-retrieval complexity score that drove routing (when
one was actually computed — see log_conversation()'s docstring on when
that's None).

This module owns storage + retrieval. It does NOT decide *when* to log —
that's core/agent.py's job (called once per Agent.run(), after the loop
ends, regardless of which exit path was taken). Keeping the decision of
"when" out of this file mirrors memory/cache.py's split: LLMClient decides
whether a request is cacheable, cache.py just knows how to get()/set().

SCOPE NOTE — search_history() is keyword-overlap search (shared-word count
between the query and each row's stored text), not semantic/embedding
search. A proper semantic search over past conversations would need its
own embedding + vector-store design (a second Chroma collection? reuse the
code collection with a type filter?) — a bigger decision than this V2 pass
warrants. Keyword overlap is a real, useful first cut that gets "what did
I ask about auth last week?"-style recall working now; revisit with
embeddings only if keyword recall proves too weak in practice.
"""

import json
import re
import sqlite3
from datetime import datetime
from typing import Optional

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    repo_path TEXT,
    user_query TEXT,
    agent_response TEXT,
    tools_used TEXT,      -- JSON array
    backend_used TEXT,    -- "groq" | "boromir" | "faramir"
    complexity_score INTEGER
);
"""

# Words shorter than this are too common (a, is, the, ...) to be useful
# signal in keyword-overlap matching — filtered out of both the query and
# implicitly via the overlap-count scoring in search_history() below.
_MIN_KEYWORD_LEN = 3


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def _row_to_record(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["tools_used"] = json.loads(record["tools_used"] or "[]")
    return record


def log_conversation(
    repo_path: str,
    user_query: str,
    agent_response: str,
    tools_used: list[str],
    backend_used: str,
    complexity_score: Optional[int] = None,
) -> int:
    """
    Records one completed Agent.run() to episodic memory.

    Args:
        repo_path: Absolute path to the repo the query was run against.
        user_query: The raw question the user asked.
        agent_response: The final answer text returned to the user.
        tools_used: Tool names called during the run, in call order
                    (duplicates kept — mirrors the "search_codebase (4x),
                    read_file (2x)" style already used in the Engineering
                    Journal's per-session summaries).
        backend_used: "groq" | "boromir" | "faramir" — whichever backend
                    produced the final answer (see core/agent.py's
                    _log_conversation() for how this is derived from
                    ProviderResponse).
        complexity_score: The post-retrieval score that drove routing
                    (core/llm_client.py's score_complexity()), when one
                    was actually computed. None for forced-provider runs
                    (--provider groq/boromir/faramir, --force-local) and
                    for cache hits, where the scorer never ran — logging
                    None here is the honest answer, not a guess.

    Returns:
        The new row's id.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO conversations
                (timestamp, repo_path, user_query, agent_response,
                 tools_used, backend_used, complexity_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(),
                repo_path,
                user_query,
                agent_response,
                json.dumps(tools_used or []),
                backend_used,
                complexity_score,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_recent(repo_path: Optional[str] = None, limit: int = 10) -> list[dict]:
    """
    Most recent conversations, newest first. Scoped to repo_path if given,
    else across all indexed repos.
    """
    conn = _connect()
    try:
        if repo_path:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE repo_path = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (repo_path, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def get_conversations_for_date(repo_path: str, date: str) -> list[dict]:
    """
    All conversations logged for repo_path on a given calendar date
    ("YYYY-MM-DD", local time — matches the date prefix of the isoformat
    timestamps log_conversation() writes). Powers Engineering Journal
    automation (memory/journal.py, Section 10): a session's journal entry
    is exactly "everything logged for this repo today."

    Returned oldest-first (chronological) — unlike get_recent()'s
    newest-first ordering, a journal entry reads better as a timeline of
    what happened during the session than as a reverse-chronological feed.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE repo_path = ? "
            "AND substr(timestamp, 1, 10) = ? ORDER BY timestamp ASC",
            (repo_path, date),
        ).fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def search_history(query: str, repo_path: Optional[str] = None, limit: int = 5) -> list[dict]:
    """
    Keyword-overlap search over past conversations — see module docstring
    for why this isn't semantic search in V2.

    Scoring: each past conversation gets a score equal to the number of
    distinct query keywords (length >= _MIN_KEYWORD_LEN) found anywhere in
    its stored user_query + agent_response text. Rows with a score of 0
    are dropped. Ties are broken by recency, since candidates are fetched
    in timestamp-DESC order and Python's sort is stable.

    Returns:
        Up to `limit` records, highest keyword-overlap first.
    """
    keywords = {
        w.lower() for w in re.findall(r"[a-zA-Z0-9_]+", query)
        if len(w) >= _MIN_KEYWORD_LEN
    }
    if not keywords:
        return []

    conn = _connect()
    try:
        if repo_path:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE repo_path = ? "
                "ORDER BY timestamp DESC",
                (repo_path,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY timestamp DESC"
            ).fetchall()
    finally:
        conn.close()

    scored = []
    for row in rows:
        record = _row_to_record(row)
        haystack = f"{record['user_query']} {record['agent_response']}".lower()
        overlap = sum(1 for kw in keywords if kw in haystack)
        if overlap > 0:
            scored.append((overlap, record))

    # Stable sort on -overlap preserves the existing timestamp-DESC order
    # for equal scores, so ties resolve to "most recent first" for free.
    scored.sort(key=lambda pair: -pair[0])

    return [record for _, record in scored[:limit]]


if __name__ == "__main__":
    # Manual smoke test: python -m memory.history
    test_repo = "/fake/repo/for/smoke/test"

    row_id = log_conversation(
        repo_path=test_repo,
        user_query="how does auth work in this repo?",
        agent_response="Auth is handled in middleware/auth.py via a JWT check.",
        tools_used=["search_codebase", "read_file"],
        backend_used="boromir",
        complexity_score=7,
    )
    print(f"Logged conversation id={row_id}")

    log_conversation(
        repo_path=test_repo,
        user_query="what does the config module do?",
        agent_response="config.py centralizes env vars and thresholds.",
        tools_used=["read_file"],
        backend_used="boromir",
        complexity_score=3,
    )

    print("\n--- get_recent(test_repo, limit=5) ---")
    for r in get_recent(test_repo, limit=5):
        print(f"  [{r['timestamp']}] {r['backend_used']} | {r['user_query']}")

    print("\n--- search_history('auth', test_repo) ---")
    for r in search_history("auth", test_repo):
        print(f"  MATCH: {r['user_query']} -> {r['agent_response']}")

    print("\n--- search_history('nonexistent gibberish query', test_repo) (expect empty) ---")
    print(search_history("nonexistent gibberish query", test_repo))
