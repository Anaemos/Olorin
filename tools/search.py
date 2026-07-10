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
"""

from indexer.store import get_collection, query as store_query


def search_codebase(query: str, repo_root: str, n_results: int = 5) -> dict:
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

    Returns:
        dict — {"chunks": [{"content", "file", "lines", "type", "name",
        "distance"}, ...]} on success, or {"error": ..., "chunks": []}
        if the repo hasn't been indexed yet.
    """
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

    chunks = [
        {
            "content": r["content"],
            "file": r["metadata"].get("file"),
            "lines": r["metadata"].get("lines"),
            "type": r["metadata"].get("type"),
            "name": r["metadata"].get("name"),
            "distance": r["distance"],
        }
        for r in results
    ]

    return {"chunks": chunks}
