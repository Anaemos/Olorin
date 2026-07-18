"""
tools/registry.py — Tool schema definitions + dispatch, OpenAI function-
calling format (Section 9 of OLORIN_PROJECT.md).

Two things live here:
  1. TOOL_SCHEMAS — the JSON schema list passed to LLMClient.chat(tools=...)
     so the model knows what tools exist and how to call them.
  2. execute_tool() — the dispatch function core/agent.py calls with a
     parsed tool_call to actually run the corresponding Python function.

Keeping schema definition and dispatch in the same file means adding a
new tool later (e.g. web_search in V2/V3) is a two-part change entirely
within this one module: add the schema, add a dispatch branch.
"""

import json

from tools.entities import search_entities
from tools.git_diff import git_diff
from tools.history import search_history
from tools.importance import file_importance
from tools.reader import list_files, read_file
from tools.search import search_codebase
from tools.websearch import web_search

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_codebase",
            "description": (
                "Semantic search over the indexed codebase. Returns the "
                "most relevant code chunks for a natural-language query. "
                "If the repo hasn't been indexed yet, returns an error "
                "directing you to use list_files/read_file instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query.",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "Max number of chunks to return.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live web for current information outside "
                "the indexed codebase and outside your training data "
                "(current events, library/API changes, version numbers, "
                "anything time-sensitive). Use this instead of guessing "
                "when a question needs up-to-date or general-knowledge "
                "facts the codebase itself won't contain. Returns an "
                "honest error if no search backend is configured — in "
                "that case, answer from trained knowledge and say so."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max number of results to return.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the full contents of a single file, given its path "
                "relative to the repository root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path, e.g. 'src/auth.py'.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": (
                "Search past conversations you've had about this repo. "
                "Useful for questions like 'what did I figure out about X "
                "before?' or 'have I asked about this already?'. Matches "
                "by keyword overlap, not semantic similarity — try a few "
                "different phrasings if the first search comes up empty. "
                "Returns an error if nothing has been logged for this repo yet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language description of what you're looking for.",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "Max number of past conversations to return.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_entities",
            "description": (
                "Search entity memory — decisions and concepts previously "
                "identified as worth remembering for this repo (e.g. 'what "
                "did we decide about the daemon?', 'what's the status of "
                "X?'). Not code search and not conversation search — this "
                "only finds things explicitly extracted as a decision or "
                "concept from a past exchange. Matches by keyword overlap. "
                "Returns an error if nothing has been remembered yet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language description of what you're looking for.",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "Max number of entities to return.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_importance",
            "description": (
                "Answers 'why is this file important?' using the repo's "
                "import graph — how many other files import it (blast "
                "radius if changed) and what it itself imports. Useful "
                "before suggesting a change to a file, or when asked "
                "which files are central/foundational vs. leaf/entry-"
                "point scripts. Covers Python, Rust, JavaScript, "
                "TypeScript, C, and C++ only (Go/Java imports aren't "
                "resolved yet). Returns an error if the repo hasn't been "
                "indexed since this feature was added."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repo-relative file path, e.g. 'core/agent.py'.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "Explains what changed via git. With no ref, shows "
                "uncommitted changes (working tree + staged, vs HEAD) — "
                "'what have I changed but not committed yet?'. Pass a ref "
                "(a commit SHA, branch name, or a range like "
                "'abc123..def456') to diff something else instead. "
                "Read-only — never commits, stages, or checks out "
                "anything. Returns an error if the repo isn't a git "
                "repository or the ref is invalid."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": (
                            "Optional git revision or range, e.g. "
                            "'HEAD~3', 'main..feature-branch'. Omit for "
                            "uncommitted changes."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files in the repository matching a glob pattern "
                "(e.g. '*.py', '*.md', '*' for everything). Automatically "
                "skips noise/build directories like .git, node_modules, "
                "venv, .next, dist, build. Capped at 200 results — check "
                "the 'truncated' field in the response and narrow your "
                "pattern (e.g. '*.py' instead of '*') if it's true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern. Defaults to '*'.",
                        "default": "*",
                    },
                },
                "required": [],
            },
        },
    },
]


def execute_tool(tool_name: str, arguments: dict, repo_root: str) -> dict:
    """
    Dispatches a tool call by name to its implementation.

    Args:
        tool_name: One of "search_codebase", "search_history", "search_entities",
                   "read_file", "list_files", "web_search", "file_importance",
                   "git_diff".
        arguments: Parsed arguments dict (already json.loads'd from the
                   provider's raw tool_call.arguments string by the caller).
        repo_root: Absolute path to the repo being queried — injected here
                   rather than trusted from model-supplied arguments, so
                   the agent can never be tricked into pointing read_file/
                   list_files at a different directory via a crafted
                   tool-call argument.

    Returns:
        dict — JSON-serializable tool result. Unknown tool names return
        an {"error": ...} dict rather than raising, since a malformed or
        hallucinated tool-call name shouldn't crash the agent loop.
    """
    if tool_name == "search_codebase":
        return search_codebase(
            query=arguments.get("query", ""),
            repo_root=repo_root,
            n_results=arguments.get("n_results", 5),
        )

    if tool_name == "search_history":
        return search_history(
            query=arguments.get("query", ""),
            repo_root=repo_root,
            n_results=arguments.get("n_results", 5),
        )

    if tool_name == "search_entities":
        return search_entities(
            query=arguments.get("query", ""),
            repo_root=repo_root,
            n_results=arguments.get("n_results", 5),
        )

    if tool_name == "read_file":
        return read_file(path=arguments.get("path", ""), repo_root=repo_root)

    if tool_name == "list_files":
        return list_files(repo_root=repo_root, pattern=arguments.get("pattern", "*"))

    if tool_name == "file_importance":
        return file_importance(path=arguments.get("path", ""), repo_root=repo_root)

    if tool_name == "git_diff":
        return git_diff(repo_root=repo_root, ref=arguments.get("ref") or None)

    if tool_name == "web_search":
        # repo_root deliberately unused here, unlike every other tool —
        # web_search has no notion of a repo scope to inject, it's a pure
        # external lookup. Accepted as a parameter anyway so execute_tool()'s
        # uniform dispatch signature doesn't need a special case per tool.
        return web_search(
            query=arguments.get("query", ""),
            max_results=arguments.get("max_results", 5),
        )

    return {"error": f"Unknown tool: {tool_name}"}


def parse_tool_call_arguments(raw_arguments: str) -> dict:
    """
    Providers return tool_call.arguments as a raw JSON string (per the
    OpenAI function-calling spec). Centralizing the parse here means one
    place handles malformed JSON from a model, rather than every caller
    needing its own try/except.
    """
    try:
        return json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        return {}


if __name__ == "__main__":
    # Manual smoke test: python -m tools.registry
    import os

    test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print("--- execute_tool('list_files') ---")
    result = execute_tool("list_files", {"pattern": "*.py"}, repo_root=test_root)
    print(f"Found {result['count']} files")

    print("\n--- execute_tool('read_file') ---")
    result = execute_tool("read_file", {"path": "config.py"}, repo_root=test_root)
    print(f"Read {len(result.get('content', ''))} chars" if "content" in result else result)

    print("\n--- execute_tool('search_codebase') (expects 'not indexed' error unless this repo has been indexed) ---")
    result = execute_tool("search_codebase", {"query": "auth logic"}, repo_root=test_root)
    print(result)

    print("\n--- execute_tool('unknown_tool') ---")
    result = execute_tool("bogus_tool", {}, repo_root=test_root)
    print(result)
