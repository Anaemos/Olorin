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

from tools.reader import list_files, read_file
from tools.search import search_codebase

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
            "name": "list_files",
            "description": (
                "List files in the repository matching a glob pattern "
                "(e.g. '*.py', '*.md', '*' for everything). Automatically "
                "skips noise directories like .git, node_modules, venv."
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
        tool_name: One of "search_codebase", "read_file", "list_files".
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

    if tool_name == "read_file":
        return read_file(path=arguments.get("path", ""), repo_root=repo_root)

    if tool_name == "list_files":
        return list_files(repo_root=repo_root, pattern=arguments.get("pattern", "*"))

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
