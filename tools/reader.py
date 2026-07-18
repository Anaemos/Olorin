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


def list_files(repo_root: str, pattern: str = "*", max_files: int = 200) -> dict:
    """
    Lists files in repo_root matching a glob pattern, skipping common
    noise/build directories that would otherwise flood results and waste
    the agent's context.

    Real bug found + fixed (2026-07-11): this had no result-size cap at
    all, unlike read_file's max_chars. Verified live against a large
    real-world repo (Next.js build output + ML model checkpoints, not
    excluded by the noise-directory list at the time) that the unbounded
    file list ballooned local inference latency to 115s — the model had
    to process a genuinely huge tool result before it could answer.
    Notably, list_files results are deliberately excluded from
    Agent._accumulate_retrieved_chunks() (filenames aren't retrieval
    content the complexity scorer should weigh — see core/agent.py's
    docstring on that), so an oversized result never even got the chance
    to correctly escalate to Groq; it just silently cost real local
    latency instead. Capping the tool's own output addresses that
    directly, rather than leaning on the scorer to catch a problem it was
    deliberately designed not to see.

    Args:
        repo_root: Absolute path to the indexed repository.
        pattern: Glob pattern, e.g. "*.py", "*.md", "*" for everything.
        max_files: Hard cap on returned files. Matches beyond this are
                   dropped, not silently mixed into a huge list — the
                   agent is told via "truncated" so it can narrow the
                   pattern instead of assuming it saw every file.

    Returns:
        dict with {"files": list[str], "count": int, "total_matched": int,
        "truncated": bool} — paths relative to repo_root. "count" is how
        many are actually returned; "total_matched" is how many existed
        before the cap, so the agent can tell "200 of 200" from "200 of
        4,000" and react accordingly.
    """
    IGNORED_DIRS = {
        ".git", "node_modules", "venv", ".venv", "__pycache__", "chroma_db",
        # Build/artifact directories — added 2026-07-11 alongside the
        # size cap above; these were the actual bulk of the 115s repro.
        ".next", "dist", "build", "target", ".ipynb_checkpoints",
        ".pytest_cache", ".mypy_cache",
    }

    matches = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]

        for filename in files:
            if fnmatch.fnmatch(filename, pattern):
                rel_path = os.path.relpath(os.path.join(root, filename), repo_root)
                matches.append(rel_path.replace(os.sep, "/"))

    matches.sort()
    total_matched = len(matches)
    truncated = total_matched > max_files
    if truncated:
        matches = matches[:max_files]

    return {
        "files": matches,
        "count": len(matches),
        "total_matched": total_matched,
        "truncated": truncated,
    }


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
