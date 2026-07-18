"""
tools/importance.py — file_importance tool (V3, "why is this file
important?", OLORIN_PROJECT.md Section 11).

Thin wrapper around memory/import_graph.py's get_importers()/
get_imports(), reshaping into the tool-result shape the agent expects
plus a plain-language verdict line — same honest-error philosophy as
tools/search.py/tools/history.py for an unindexed/empty state.
"""

from memory import import_graph


def file_importance(path: str, repo_root: str) -> dict:
    """
    Answers "why is this file important?" using the repo's import graph.

    Args:
        path: repo-relative file path to ask about, e.g. "core/agent.py".
        repo_root: Absolute repo path — injected by execute_tool(), never
                   trusted from model-supplied arguments, same as every
                   other tool.

    Returns:
        dict — {"file", "imported_by": [...], "imported_by_count",
        "imports": [...], "imports_count", "verdict"} on success, or
        {"error": ...} if this repo's import graph hasn't been built yet
        (distinct from "this specific file has zero edges", which is a
        valid, non-error answer — see import_graph.has_any_edges()).
    """
    if not import_graph.has_any_edges(repo_root):
        return {
            "error": (
                "No import graph found for this repo. Run "
                "'python cli.py index <path>' first (or re-index if the "
                "existing index predates this feature)."
            ),
        }

    importers = import_graph.get_importers(repo_root, path)
    imports = import_graph.get_imports(repo_root, path)

    if not importers and not imports:
        verdict = (
            f"No internal import relationships found for {path} — either "
            "it isn't imported by (or doesn't import) anything else in "
            "this repo, or the path doesn't match what's in the index "
            "(double-check it against list_files)."
        )
    elif not importers:
        verdict = (
            f"{path} isn't imported by any other file in this repo "
            f"(it imports {len(imports)}) — likely an entry point, "
            "script, or leaf module rather than shared infrastructure."
        )
    else:
        blast_radius = (
            "high" if len(importers) >= 5 else
            "moderate" if len(importers) >= 2 else
            "low"
        )
        preview = ", ".join(importers[:5])
        more = ", ..." if len(importers) > 5 else ""
        verdict = (
            f"{path} is imported by {len(importers)} file"
            f"{'s' if len(importers) != 1 else ''} in this repo "
            f"({preview}{more}) — {blast_radius} blast radius if changed."
        )

    return {
        "file": path,
        "imported_by": importers,
        "imported_by_count": len(importers),
        "imports": imports,
        "imports_count": len(imports),
        "verdict": verdict,
    }
