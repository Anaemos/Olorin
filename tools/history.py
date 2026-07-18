"""
tools/history.py — search_history tool (V2).

Thin wrapper around memory/history.py's search_history(), reshaping the
result into the {results: [...]} shape the agent/tool-calling layer
expects, with an honest "nothing logged yet" error when there's no
episodic memory for this repo — same philosophy as tools/search.py's
"not indexed yet" error for search_codebase.

See memory/history.py's module docstring for the keyword-overlap-vs-
semantic-search scope decision this tool inherits.
"""

from memory import history as history_store


def search_history(query: str, repo_root: str, n_results: int = 5) -> dict:
    """
    Search past conversations logged against this repo.

    Args:
        query: Natural-language query — matched by shared keywords against
               each past conversation's stored question + answer text.
        repo_root: Absolute path to the repo — scopes results to past
                   conversations logged against this same repo. Injected
                   by execute_tool(), never trusted from model-supplied
                   arguments, same as every other tool.
        n_results: Max number of past conversations to return.

    Returns:
        dict — {"results": [{"timestamp", "user_query", "agent_response",
        "backend_used", "tools_used"}, ...]} on a match, or
        {"error": ..., "results": []} if nothing matched (or nothing has
        been logged for this repo yet).
    """
    records = history_store.search_history(query, repo_path=repo_root, limit=n_results)

    if not records:
        return {
            "error": (
                "No matching past conversations found for this repo "
                "(or nothing has been logged yet)."
            ),
            "results": [],
        }

    results = [
        {
            "timestamp": r["timestamp"],
            "user_query": r["user_query"],
            "agent_response": r["agent_response"],
            "backend_used": r["backend_used"],
            "tools_used": r["tools_used"],
        }
        for r in records
    ]

    return {"results": results}
