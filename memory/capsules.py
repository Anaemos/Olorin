"""
memory/capsules.py — Repository Knowledge Capsule storage.

Pulled forward from V3 to V2 (OLORIN_PROJECT.md Section 11) — reprioritized
ahead of entity memory because the value/complexity tradeoff was clearly
better: capsules give the agent a repo map before it decides whether to
call any tool at all, using infrastructure (hash-based skip logic, the
memory.db SQLite file) that already exists, versus entity memory's open
design questions (population mechanism, type scope) that hadn't been
resolved yet.

This module owns storage + the regeneration check only. Generation
(actually calling an LLM to synthesize a summary) lives in
indexer/capsules.py — same store/generate split already used elsewhere
(memory/history.py vs the agent-loop code that decides when to log;
indexer/store.py vs tools/search.py).

Granularity is module-level (one capsule per top-level repo directory),
not file-level — see OLORIN_PROJECT.md Section 10 for why: file-level
capsules don't scale with repo size and duplicate what search_codebase
already does at chunk granularity, while module-level capsules stay a
small, bounded set regardless of repo size and match V3's original
"pre-computed module summaries" framing.
"""

import json
import sqlite3
from datetime import datetime
from typing import Optional

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capsules (
    repo_path TEXT,
    module TEXT,
    summary TEXT,
    source_files TEXT,   -- JSON array of {"path": ..., "hash": ...}
    last_updated TEXT,
    PRIMARY KEY (repo_path, module)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def _row_to_record(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["source_files"] = json.loads(record["source_files"] or "[]")
    return record


def get_capsule(repo_path: str, module: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM capsules WHERE repo_path = ? AND module = ?",
            (repo_path, module),
        ).fetchone()
        return _row_to_record(row) if row else None
    finally:
        conn.close()


def get_all_capsules(repo_path: str) -> list[dict]:
    """Every capsule for a repo, ordered by module name for stable display."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM capsules WHERE repo_path = ? ORDER BY module",
            (repo_path,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def upsert_capsule(
    repo_path: str,
    module: str,
    summary: str,
    source_files: list[dict],
) -> None:
    """
    source_files: [{"path": "core/agent.py", "hash": "..."}, ...] — every
    code file currently belonging to this module, used both for the
    "linked back to indexed files" traceability and as the basis for
    needs_regeneration()'s comparison on the next index run.
    """
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO capsules (repo_path, module, summary, source_files, last_updated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(repo_path, module) DO UPDATE SET
                summary = excluded.summary,
                source_files = excluded.source_files,
                last_updated = excluded.last_updated
            """,
            (
                repo_path, module, summary,
                json.dumps(source_files), datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def needs_regeneration(repo_path: str, module: str, current_files: list[dict]) -> bool:
    """
    True if no capsule exists yet for this module, or if the set of
    (path, hash) pairs currently in the module differs from what the
    stored capsule was generated against — same hash-comparison
    philosophy as cli.py's per-file reindex-skip logic (Section 8),
    applied at module granularity instead of file granularity.
    """
    existing = get_capsule(repo_path, module)
    if existing is None:
        return True

    existing_set = {(f["path"], f["hash"]) for f in existing["source_files"]}
    current_set = {(f["path"], f["hash"]) for f in current_files}
    return existing_set != current_set


if __name__ == "__main__":
    # Manual smoke test: python -m memory.capsules
    test_repo = "/fake/repo/for/smoke/test"
    files_v1 = [{"path": "core/agent.py", "hash": "abc123"}]

    print(f"needs_regeneration (no capsule yet): {needs_regeneration(test_repo, 'core', files_v1)}")

    upsert_capsule(
        test_repo, "core",
        "The core module implements the ReAct agent loop and LLM routing.",
        files_v1,
    )
    print(f"needs_regeneration (unchanged): {needs_regeneration(test_repo, 'core', files_v1)}")

    files_v2 = [{"path": "core/agent.py", "hash": "def456"}]  # hash changed
    print(f"needs_regeneration (file changed): {needs_regeneration(test_repo, 'core', files_v2)}")

    print("\n--- get_all_capsules ---")
    for c in get_all_capsules(test_repo):
        print(f"  [{c['module']}] {c['summary']} (files: {c['source_files']})")
