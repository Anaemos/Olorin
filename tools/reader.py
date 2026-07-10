"""
tools/reader.py — read_file and list_files tools (V1).

Both tools are scoped to a single repo_root, passed in at construction time
(from the CLI's `python cli.py ask ... <repo_path>` argument, eventually).
Every path is resolved and checked against repo_root to prevent path
traversal outside the indexed project — an agent should never be able to
read arbitrary files elsewhere on the user's machine just because a tool
argument said `../../../../etc/passwd` or similar.
"""

import fnmatch
import os


class PathSecurityError(Exception):
    """Raised when a requested path resolves outside repo_root."""


def _resolve_safe(repo_root: str, relative_path: str) -> str:
    repo_root_abs = os.path.abspath(repo_root)
    target_abs = os.path.abspath(os.path.join(repo_root_abs, relative_path))

    # os.path.commonpath raises on mixed drive letters (Windows) if paths
    # don't share a root — treat that as "outside repo_root" too.
    try:
        common = os.path.commonpath([repo_root_abs, target_abs])
    except ValueError:
        raise PathSecurityError(
            f"Path '{relative_path}' resolves outside repo root."
        )

    if common != repo_root_abs:
        raise PathSecurityError(
            f"Path '{relative_path}' resolves outside repo root."
        )

    return target_abs


def read_file(path: str, repo_root: str, max_chars: int = 20000) -> dict:
    """
    Reads a file's contents, scoped to repo_root.

    Args:
        path: Relative path from repo_root (e.g. "src/auth.py").
        repo_root: Absolute path to the indexed repository.
        max_chars: Truncation limit to avoid blowing the context window
                   on an accidentally huge file (e.g. a bundled JS file).

    Returns:
        dict with either {"content": str, "truncated": bool} or {"error": str}
    """
    try:
        abs_path = _resolve_safe(repo_root, path)
    except PathSecurityError as e:
        return {"error": str(e)}

    if not os.path.isfile(abs_path):
        return {"error": f"File not found: {path}"}

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return {"error": f"Failed to read {path}: {e}"}

    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]

    return {"content": content, "truncated": truncated, "path": path}


def list_files(repo_root: str, pattern: str = "*") -> dict:
    """
    Lists files in repo_root matching a glob pattern, skipping common
    noise directories (.git, node_modules, venv, __pycache__) that would
    otherwise flood results and waste the agent's context.

    Args:
        repo_root: Absolute path to the indexed repository.
        pattern: Glob pattern, e.g. "*.py", "*.md", "*" for everything.

    Returns:
        dict with {"files": list[str]} — paths relative to repo_root.
    """
    IGNORED_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "chroma_db"}

    matches = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]

        for filename in files:
            if fnmatch.fnmatch(filename, pattern):
                rel_path = os.path.relpath(os.path.join(root, filename), repo_root)
                matches.append(rel_path.replace(os.sep, "/"))

    return {"files": sorted(matches), "count": len(matches)}


if __name__ == "__main__":
    # Manual smoke test: python -m tools.reader
    test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print("--- list_files('*.py') ---")
    result = list_files(test_root, "*.py")
    print(f"Found {result['count']} files")
    for f in result["files"][:10]:
        print(f"  {f}")

    print("\n--- read_file('config.py') ---")
    result = read_file("config.py", test_root)
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"Read {len(result['content'])} chars, truncated={result['truncated']}")

    print("\n--- Path traversal attempt (should be blocked) ---")
    result = read_file("../../../../etc/passwd", test_root)
    print(result)
