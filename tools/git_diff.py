"""
tools/git_diff.py — git_diff agent tool (V3, "git diff -> agent explains
what changed", OLORIN_PROJECT.md Section 11).

Wraps `git diff` via subprocess, scoped to repo_root. Read-only by
design, matching every other tool in this project — never commits,
stages, checks out, or otherwise mutates repo state.

SECURITY: a ref argument here can be model-supplied (the agent decides
what to ask for), and it flows into a subprocess argv list. Passing
subprocess a list (not shell=True) already rules out shell injection,
but a ref STARTING WITH "-" is a separate, real risk class: git would
interpret something like "--upload-pack=evil" or "-O/some/path" as an
option rather than a revision, potentially doing something the caller
never intended. Defended two ways, deliberately layered rather than
relying on either alone: (1) a strict allowlist regex + an explicit
leading-dash rejection before the ref ever reaches subprocess, and
(2) a literal "--" separator between the ref and the (empty) pathspec
list in every git invocation — git's own standard idiom for "everything
before this is revisions/options, everything after is paths," which
also means a ref that somehow slipped past validation still can't be
interpreted as a trailing pathspec option. Verified directly (not
assumed) against a real option-injection-shaped string
("--upload-pack=evil") before this was considered done.
"""

import re
import subprocess

_MAX_REF_LENGTH = 200
_MAX_STAT_CHARS = 2000

# Allowlist covers everything a real git ref can legitimately contain:
# branch/tag names, commit SHAs, and revision syntax like "HEAD~3",
# "HEAD^2", "a1b2c3..d4e5f6" (ranges). Deliberately does NOT allow
# spaces, semicolons, or shell metacharacters — none of those appear in
# valid git ref syntax, so disallowing them costs no real functionality.
_REF_PATTERN = re.compile(r"^[A-Za-z0-9_./\-~^]+$")


class GitError(Exception):
    """Raised for any git-related failure — caught at the tool boundary
    and turned into an honest {"error": ...} result, same pattern as
    every other tool's failure handling."""


def _validate_ref(ref: str) -> None:
    if len(ref) > _MAX_REF_LENGTH:
        raise GitError(f"ref is too long ({len(ref)} chars, max {_MAX_REF_LENGTH}).")
    if ref.startswith("-"):
        # Belt-and-suspenders beyond the regex below: "-" is itself an
        # allowed character mid-string (e.g. "feature-branch"), so this
        # catches specifically the leading-dash option-injection shape
        # the regex alone wouldn't reject.
        raise GitError(f"Invalid ref '{ref}': refs may not start with '-'.")
    if not _REF_PATTERN.match(ref):
        raise GitError(
            f"Invalid ref '{ref}': only letters, numbers, and . _ / - ~ ^ "
            "are allowed."
        )


def _run_git(args: list[str], repo_root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise GitError("git command timed out.")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "not a git repository" in stderr.lower():
            raise GitError("This repo is not a git repository.")
        # Truncated, not swallowed — the agent should see enough of
        # git's own message (e.g. "unknown revision") to explain a bad
        # ref back to the user, without an unbounded stderr dump.
        raise GitError(f"git failed: {stderr[:500]}")

    return result.stdout


def _split_diff_by_file(diff_text: str) -> list[dict]:
    """
    Splits a unified diff into per-file chunks, keyed by each file's own
    path (parsed from its "diff --git a/X b/Y" header) — verified
    against a real multi-file git diff before trusting it, same
    discipline as indexer/imports.py's tree-sitter checks. Returning
    per-file {"file", "content"} chunks (rather than one flat diff
    string) means this tool's result naturally feeds
    Agent._accumulate_retrieved_chunks() the same way search_codebase's
    chunks already do — a diff touching several files should score as
    more complex than one touching a single file, and the existing
    multi-file complexity signal (Section 7/13) picks that up for free
    with no special-casing needed here.
    """
    if not diff_text:
        return []

    parts = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    chunks = []
    for part in parts:
        if not part.strip():
            continue
        match = re.match(r"^diff --git a/(.+?) b/(.+)$", part, flags=re.MULTILINE)
        file_path = match.group(2) if match else "unknown"
        chunks.append({"file": file_path, "content": part})
    return chunks


def git_diff(repo_root: str, ref: str | None = None, max_chars: int = 20000) -> dict:
    """
    Explains what changed, via `git diff`.

    Args:
        repo_root: Absolute repo path — injected by execute_tool(),
                   never trusted from model-supplied arguments, same as
                   every other tool.
        ref: Optional revision or range (e.g. "HEAD~3", "abc123..def456",
             a branch name). None (the default) means uncommitted
             changes — working tree + staged, compared against HEAD
             (`git diff HEAD`) — "what have I changed but not committed
             yet," the most common real use case. A range like "A..B" is
             passed straight through to git as a single argument; git's
             own diff syntax already handles that natively, so there's
             no range-parsing to get wrong here.
        max_chars: Aggregate cap across all per-file chunks combined
                   (not per-file) — mirrors read_file's max_chars/
                   truncated pattern. Whole files are dropped once the
                   running total would exceed the cap, rather than
                   truncating mid-file-diff, so every chunk the agent
                   does see is a complete, coherent diff for that file.

    Returns:
        dict — {"ref", "stat", "chunks": [{"file", "content"}, ...],
        "files_changed", "files_shown", "truncated"} on success, or
        {"error": ...} for an invalid ref, a missing git binary, a repo
        that isn't a git repository, or any other git failure. "stat"
        (git's own `--stat` summary) is always included in full
        regardless of "truncated" — even when the unified diff itself
        gets cut off, the agent still knows the complete scope of what
        changed.
    """
    if ref:
        try:
            _validate_ref(ref)
        except GitError as e:
            return {"error": str(e)}

    diff_ref = ref or "HEAD"
    ref_display = ref or "HEAD (uncommitted changes)"

    try:
        stat_output = _run_git(["diff", diff_ref, "--stat", "--"], repo_root)
        diff_output = _run_git(["diff", diff_ref, "--"], repo_root)
    except GitError as e:
        return {"error": str(e)}

    all_chunks = _split_diff_by_file(diff_output)
    total_files = len(all_chunks)

    shown_chunks = []
    running_total = 0
    for c in all_chunks:
        if running_total + len(c["content"]) > max_chars:
            # This file alone doesn't fit in what's left of the budget
            # — skip just this one, don't give up on the rest of the
            # diff. Found live (2026-07-18) against this real repo's own
            # accumulated V1->V3 diff: with `break` here instead of
            # `continue`, a single large file appearing early in git's
            # own (alphabetical) ordering — ENGINEERING_JOURNAL.md, in
            # the run that caught this — silently starved every smaller
            # file behind it, producing files_shown=0 out of 12 changed
            # files despite several of them (config.py, tools/reader.py,
            # providers/base.py) being individually well under max_chars
            # on their own. `continue` lets smaller files later in the
            # list still get included even when an earlier file didn't fit.
            continue
        shown_chunks.append(c)
        running_total += len(c["content"])

    return {
        "ref": ref_display,
        "stat": stat_output.strip()[:_MAX_STAT_CHARS] or "(no changes)",
        "chunks": shown_chunks,
        "files_changed": total_files,
        "files_shown": len(shown_chunks),
        "truncated": len(shown_chunks) < total_files,
    }


if __name__ == "__main__":
    # Manual smoke test: python -m tools.git_diff
    # Runs against THIS repo directly — real git, real HEAD, whatever
    # is currently uncommitted (harmless: git_diff() is read-only).
    import os

    test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print("--- git_diff(uncommitted changes) ---")
    result = git_diff(test_root)
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"ref={result['ref']}")
        print(f"files_changed={result['files_changed']} files_shown={result['files_shown']} truncated={result['truncated']}")
        print(f"stat:\n{result['stat']}")

    print("\n--- git_diff(invalid ref, option-injection shape) ---")
    result = git_diff(test_root, ref="--upload-pack=evil")
    print(result)
    assert "error" in result

    print("\n--- git_diff(nonexistent ref) ---")
    result = git_diff(test_root, ref="not-a-real-ref-xyz123")
    print(result)
    assert "error" in result

    print("\n--- git_diff(not a git repo) ---")
    result = git_diff("/tmp")
    print(result)
    assert "error" in result
