"""
memory/import_graph.py — import-graph storage ("why is this file
important?", V3, OLORIN_PROJECT.md Section 11).

Stores directed edges (importer -> imported) as extracted by
indexer/imports.py, and answers "who imports this file" / "what does
this file import" queries against them. Same store/generate split
already used everywhere else in this project (memory/capsules.py vs.
indexer/capsules.py; memory/entities.py's storage half vs. its own
extraction logic) — this module only knows how to persist and query
edges, never how to parse a file's imports.

No separate hash-tracking table, unlike Capsules' needs_regeneration().
Edge storage instead piggybacks directly on the SAME per-file hash
comparison cli.py's _index_repo() already runs to decide whether a file
needs re-chunking (Section 8) — a file only ever calls
set_file_imports() when it's already been determined to be new/changed,
so there's no reason for import-graph freshness to duplicate that check
with its own stored hash. A file that gets skipped as unchanged simply
keeps whatever edges were written the last time it WAS (re)indexed —
identical staleness behavior to a skipped file's chunks, and an
identical acceptance of that tradeoff (see indexer/store.py's
delete_file() docstring: an edited file's old chunks are cleared before
new ones are written; imports follow the same replace-on-reindex pattern
here via set_file_imports()).

KNOWN LIMITATION, same class as store.py's own (not fixed here, not
introduced here either): if a file is deleted from the repo entirely
(not just changed), _index_repo()'s current walker-driven loop has no
delete-detection for chunks OR imports — a removed file's old edges (as
both importer and imported-target) can go stale. Pre-existing gap in
this project's indexing loop, not something this feature makes worse;
out of scope to fix as part of adding the import graph.
"""

import sqlite3

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_edges (
    repo_path TEXT NOT NULL,
    importer TEXT NOT NULL,
    imported TEXT NOT NULL,
    PRIMARY KEY (repo_path, importer, imported)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def set_file_imports(repo_path: str, importer_path: str, imported_paths: list[str]) -> None:
    """
    Replaces every stored edge FROM importer_path with the freshly
    resolved set. Delete-then-insert, not a diff/merge — mirrors
    indexer/store.py's delete_file()-before-upsert pattern for chunks:
    an edited file's import list can shrink as well as grow, and there's
    no cheaper correct way to express "these are no longer imported"
    than clearing first. Called once per (re)indexed file from cli.py's
    _index_repo() loop, immediately after that file's chunks are
    upserted — unchanged files never reach this function at all (module
    docstring).
    """
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM import_edges WHERE repo_path = ? AND importer = ?",
            (repo_path, importer_path),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO import_edges (repo_path, importer, imported) VALUES (?, ?, ?)",
            [(repo_path, importer_path, imported) for imported in imported_paths],
        )
        conn.commit()
    finally:
        conn.close()


def get_importers(repo_path: str, file_path: str) -> list[str]:
    """
    Every file that imports file_path — the actual "why is this file
    important" signal: a high count means many other files depend on it,
    i.e. changing it has a wide blast radius.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT importer FROM import_edges WHERE repo_path = ? AND imported = ? ORDER BY importer",
            (repo_path, file_path),
        ).fetchall()
        return [r["importer"] for r in rows]
    finally:
        conn.close()


def get_imports(repo_path: str, file_path: str) -> list[str]:
    """
    Every file file_path itself imports — the reverse direction. Useful
    alongside importer count: a file with many importers AND many
    imports of its own is a central hub; a file with many importers and
    few/none of its own is closer to a foundational leaf (e.g. config.py).
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT imported FROM import_edges WHERE repo_path = ? AND importer = ? ORDER BY imported",
            (repo_path, file_path),
        ).fetchall()
        return [r["imported"] for r in rows]
    finally:
        conn.close()


def has_any_edges(repo_path: str) -> bool:
    """
    Cheap existence check for tools/importance.py's honest-error path —
    distinguishes "this repo has never had its import graph built" (a
    real "not indexed yet"-shaped error) from "this file legitimately
    has zero internal importers/imports" (a valid, non-error answer —
    see get_importers()/get_imports() on a real leaf/entry-point file).
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM import_edges WHERE repo_path = ? LIMIT 1",
            (repo_path,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


if __name__ == "__main__":
    # Manual smoke test: python -m memory.import_graph
    # Hand-built edges, not a real repo walk — pure storage/query logic
    # check, independent of indexer/imports.py's extraction correctness
    # (that's covered by indexer/imports.py's own smoke test).
    test_repo = "/fake/repo/for/smoke/test"

    set_file_imports(test_repo, "core/agent.py", ["core/llm_client.py", "config.py", "tools/registry.py"])
    set_file_imports(test_repo, "core/llm_client.py", ["config.py"])
    set_file_imports(test_repo, "cli.py", ["core/agent.py", "config.py"])

    print(f"Importers of config.py: {get_importers(test_repo, 'config.py')}")
    assert set(get_importers(test_repo, "config.py")) == {"core/agent.py", "core/llm_client.py", "cli.py"}
    print(f"Importers of core/agent.py: {get_importers(test_repo, 'core/agent.py')}")
    print(f"Imports of core/agent.py: {get_imports(test_repo, 'core/agent.py')}")
    print(f"has_any_edges: {has_any_edges(test_repo)}")
    assert has_any_edges(test_repo) is True
    assert has_any_edges("/some/other/never/indexed/repo") is False

    # Re-index core/agent.py with a shrunk import list — confirms
    # delete-then-insert correctly drops the old tools/registry.py edge.
    set_file_imports(test_repo, "core/agent.py", ["config.py"])
    imports_after = get_imports(test_repo, "core/agent.py")
    importers_of_registry_after = get_importers(test_repo, "tools/registry.py")
    print(f"After re-index, imports of core/agent.py: {imports_after}")
    print(f"Importers of tools/registry.py (should be empty now): {importers_of_registry_after}")
    assert imports_after == ["config.py"], imports_after
    assert importers_of_registry_after == [], importers_of_registry_after

    print("\nAll assertions passed.")
