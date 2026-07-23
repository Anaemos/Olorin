"""
tools/search.py — search_codebase tool.

LIVE (V1): thin wrapper around indexer/store.py's query(), which handles
embedding the query (via indexer/embedder.py's embed_query — BGE's query
instruction prefix applied there, not here) and running the actual
ChromaDB similarity search. This module's job is narrow: resolve the
right collection for repo_root, call query(), and reshape the result
into the flat {chunks: [...]} shape the agent/tool-calling layer expects.

If the repo hasn't been indexed yet (collection exists but is empty, or
doesn't exist), this returns an explicit, honest error rather than a
silent empty chunk list — same philosophy as the old stub — so the
agent knows to fall back to list_files + read_file instead of assuming
there's just nothing relevant in the codebase.

Cross-repo querying (V3, "cross-repo querying", Section 11): search_
codebase is the one tool that became cross-repo aware — every other
tool (read_file, list_files, git_diff, file_importance, search_history,
search_entities) deliberately stayed single-repo, a scope decision made
directly with Aryavart before writing any code. See indexer/store.py's
query_multi() for the actual cross-collection merge logic.
"""

from indexer.store import get_collection, query as store_query, query_multi


def search_codebase(
    query: str,
    repo_root: str,
    n_results: int = 5,
    extra_repo_roots: list[str] | None = None,
) -> dict:
    """
    Semantic search over the indexed codebase.

    Args:
        query: Natural-language search query.
        repo_root: Absolute path to the repo being queried — used to look
                   up the correct Chroma collection (see indexer/store.py's
                   path-hash-keyed collection naming). Injected by
                   execute_tool(), same as read_file/list_files, never
                   trusted from model-supplied arguments.
        n_results: Max number of chunks to return.
        extra_repo_roots: Optional additional repo paths to search
                   alongside repo_root (V3, cross-repo querying). When
                   provided, dispatches to indexer/store.py's
                   query_multi() instead of the single-collection query()
                   path — everything else (the "not indexed" honest
                   error, chunk reshaping) stays identical, this only
                   changes WHICH collection(s) get queried and how
                   results get merged. None/empty (the default, and
                   every call site that predates this feature) behaves
                   EXACTLY as before — zero change to the single-repo path.

    Returns:
        dict — {"chunks": [{"content", "file", "lines", "type", "name",
        "repo", "distance"}, ...]} on success, or {"error": ..., "chunks": []}
        if the repo(s) haven't been indexed yet. "repo" is included on
        every chunk regardless of single- or multi-repo mode — harmless
        extra context in the common single-repo case, and the field that
        actually makes a merged cross-repo result list attributable.
    """
    all_repos = [repo_root] + list(extra_repo_roots) if extra_repo_roots else [repo_root]

    if len(all_repos) == 1:
        collection = get_collection(repo_root)
        if collection.count() == 0:
            return {
                "error": (
                    f"No indexed chunks found for this repo ({repo_root}). "
                    "Run 'python cli.py index <path>' first, or use "
                    "list_files and read_file instead for now."
                ),
                "chunks": [],
            }
        results = store_query(collection, query, n_results=n_results)
    else:
        results = query_multi(all_repos, query, n_results=n_results)
        if not results:
            return {
                "error": (
                    f"No indexed chunks found across any of {len(all_repos)} "
                    "repos. Run 'python cli.py index <path>' for each, or "
                    "use list_files and read_file instead for now."
                ),
                "chunks": [],
            }

    chunks = [
        {
            "content": r["content"],
            "file": r["metadata"].get("file"),
            "lines": r["metadata"].get("lines"),
            "type": r["metadata"].get("type"),
            "name": r["metadata"].get("name"),
            "repo": r["metadata"].get("repo"),
            "distance": r["distance"],
        }
        for r in results
    ]

    return {"chunks": chunks}
