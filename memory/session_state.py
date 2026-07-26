"""
memory/session_state.py — global "last active repo" tracking (V4,
context inheritance / auto-switch on repo change, 2026-07-23).

The design question this answers: cli.py's `ask` already defaults
`--path` to "." (cwd), and index-on-demand already means cd-into-repo-
and-ask works today with zero flags — so most of "auto-switch on repo
change" was already solved by existing behavior before this module
existed. The real, narrower gap: there's no memory of "what repo was I
just working on" for the case where the CURRENT directory isn't that
repo at all (you're in ~, or a script's cwd, or — the actual reason
this matters — a future no-terminal surface like a system tray icon or
global hotkey that has no cwd concept whatsoever). This module exists
to close exactly that gap, nothing more.

Deliberately NOT stored the way every other piece of state in this
project is (Chroma collections keyed by sha256-of-resolved-path, journal
dirs under the same scheme, the shared SQLite memory DB's per-repo
tables) — all of those are correctly namespaced PER REPO. This is the
one piece of state that must NOT be repo-scoped: it's "which repo is
currently the global 'active' one," a single scalar shared across the
whole tool, not per-project data. A tiny standalone JSON file is
simpler and more honestly scoped for that than adding a table to the
shared SQLite DB (which would need an artificial singleton-row pattern)
or, worse, a per-repo table for something that's explicitly global.

Written by cli.py's `ask` and `index` commands on every successful run
(real "I was working on this project" activity) — deliberately NOT by
`history`/`journal`/`entities`. Those are read-only browsing commands;
glancing at a different repo's journal or entity list shouldn't silently
switch what "active" means out from under you the next time you run
`ask` from an unfamiliar directory.
"""
from __future__ import annotations

import json
import os
import time

_STATE_DIR = os.path.join(os.path.expanduser("~"), ".olorin")
_STATE_PATH = os.path.join(_STATE_DIR, "session_state.json")


def get_last_active_repo() -> str | None:
    """
    Returns the last-recorded active repo's absolute path, or None if no
    session state has ever been written (first-ever run, or the file was
    deleted/never created). Never raises: a missing or corrupt state file
    is treated identically to "no prior state" — same graceful-
    degradation posture as every other memory read path in this project
    (search_history's empty-table case, search_entities' empty-table
    case). This is a convenience feature; it should never be able to
    crash a real `ask` invocation.
    """
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    repo = data.get("last_repo")
    return repo if isinstance(repo, str) and repo else None


def set_last_active_repo(repo_path: str) -> None:
    """
    Records repo_path (expected already-absolute — callers resolve via
    os.path.abspath() before this point, same as every other repo_path
    in this codebase) as the currently-active repo.

    Best-effort: a write failure (read-only home directory, permissions,
    disk full) is swallowed, never raised. This mirrors memory/
    journal.py's synthesize_worked_on() graceful-degradation stance —
    a convenience feature failing quietly is strictly better than it
    being the reason a real query fails.
    """
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(
                {"last_repo": repo_path, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                fh,
            )
    except OSError:
        pass


if __name__ == "__main__":
    # Standalone smoke test, same pattern as every other standalone
    # diagnostic in this project (test_faramir_tools.py, debug_entity_
    # extraction.py, etc.) — no repo, no LLM, just this module's own
    # read/write round-trip.
    print("--- get_last_active_repo() with no prior state (or real prior state, if this machine has used Olorin before) ---")
    print(get_last_active_repo())

    print("\n--- set + get round-trip ---")
    set_last_active_repo("/tmp/fake-repo-for-smoke-test")
    result = get_last_active_repo()
    print(result)
    assert result == "/tmp/fake-repo-for-smoke-test"

    print("\n--- corrupt-file graceful handling ---")
    with open(_STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write("not valid json {{{")
    result = get_last_active_repo()
    print(result)
    assert result is None

    print("\nAll assertions passed.")
