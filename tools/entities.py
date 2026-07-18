"""
tools/entities.py — search_entities tool.

Thin wrapper around memory/entities.py's search_entities(), same shape
as tools/history.py's search_history wrapper — honest "nothing found"
error rather than a bare empty list, matching the honesty pattern this
project uses consistently (search_codebase's "not indexed yet",
web_search's "no backend configured").
"""

from memory import entities as entities_store


def search_entities(query: str, repo_root: str, n_results: int = 5) -> dict:
    """
    Search entity memory — decisions and concepts previously extracted
    and committed for this repo (memory/entities.py). Not code search;
    see that module's docstring for the scope decision.

    Args:
        query: Natural-language description of what you're looking for.
        repo_root: Absolute path to the repo — scopes results, same as
                   every other repo-scoped tool. Injected by
                   execute_tool(), never trusted from model-supplied
                   arguments.
        n_results: Max number of entities to return.

    Returns:
        dict — {"results": [{"type", "name", "description",
        "last_updated"}, ...]} on a match, or {"error": ..., "results":
        []} if nothing matched (or nothing has been remembered yet).
    """
    records = entities_store.search_entities(query, repo_path=repo_root, limit=n_results)

    if not records:
        return {
            "error": (
                "No matching entities found for this repo (or nothing "
                "has been remembered yet)."
            ),
            "results": [],
        }

    results = [
        {
            "type": r["type"],
            "name": r["name"],
            "description": r["description"],
            "last_updated": r["last_updated"],
        }
        for r in records
    ]

    return {"results": results}
