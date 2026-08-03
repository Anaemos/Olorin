"""
cli.py — Olorin's entry point (Section 7's CLI flags + Section 8's
"remaining before V1 demo" item, per OLORIN_PROJECT.md Section 16).

Two commands:

    python cli.py index <path>       — walk, chunk, embed, and store a repo
    python cli.py ask "<query>" [path]   — run the ReAct agent against an
                                            already-indexed (or unindexed —
                                            it'll fall back to list_files/
                                            read_file) repo

`index` orchestrates the full Section 8 pipeline:

    Rust walker (subprocess, JSON on stdout)
        -> hash comparison against what's already in Chroma
        -> tree-sitter chunker (only for changed/new files)
        -> GPU embedder
        -> ChromaDB upsert

The hash comparison is the actual payoff of the Rust walker reporting a
SHA-256 per file (Section 8) and store.py exposing get_indexed_file_hash():
re-running `index` on a repo where nothing changed should do almost no
work, because every file's fresh hash matches what's already stored.

HONESTY NOTE for --profile: the timestamp taken as this module's first
executable line (_MODULE_START below) is NOT the same as true OS-level
interpreter startup — the OS loading python.exe, dynamic libraries, and
running site.py all happen before this line runs, and no in-process timer
can see that. See core/profiling.py's module docstring for how to get the
real number externally.
"""

from __future__ import annotations

import time

_MODULE_START = time.perf_counter()

import json
import os
import subprocess
import sys

import typer

from core import profiling

app = typer.Typer(help="Olorin — local-first engineering assistant.")

# Extensions treated as "code" for Knowledge Capsule grouping (Section
# 10/13). Deliberately its own allowlist rather than trusting the
# walker's language field — keeps capsule generation decoupled from
# chunker.py's internal language-support matrix, which this module has
# no need to know about.
_CODE_EXTENSIONS = {
    ".py", ".rs", ".js", ".ts", ".jsx", ".tsx",
    ".go", ".java", ".c", ".cpp", ".h", ".hpp", ".rb", ".cs",
}

# Where the Rust walker binary lands after `cargo build --release`.
# Matches the standard Cargo output layout; not user-configurable in V1
# since indexer_core/ is a fixed part of this repo's own layout (as
# opposed to the *indexed* repo, which is arbitrary).
_INDEXER_BINARY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "indexer_core", "target", "release",
    "indexer_core.exe" if os.name == "nt" else "indexer_core",
)


# Matches server/app.py's own default (--port). Duplicated as a literal
# here rather than imported, since server/app.py imports _index_repo
# from this module -- importing back from server.app at module load
# time would be circular. See _try_server_ask()'s docstring.
_SERVER_PORT = 8756


def _try_server_ask(payload: dict, timeout: float = 0.3) -> dict | None:
    """
    Checks for an already-running Olorin Server (server/app.py, V5 Phase
    2) on localhost and, if reachable, routes the request through it
    instead of building a fresh in-process Agent. Returns None -- not an
    error dict -- on any connection failure, so the caller falls back to
    the existing direct path unchanged. This must never turn "no server
    running" (the overwhelmingly common case for a one-shot `ask` call)
    into a visible error; the server is an opportunistic fast path, not
    a requirement.

    Deferred stdlib import (urllib), matching this module's existing
    pattern of not paying for anything an --help or non-server call
    doesn't need.
    """
    import urllib.error
    import urllib.request

    try:
        health_req = urllib.request.Request(f"http://127.0.0.1:{_SERVER_PORT}/health")
        with urllib.request.urlopen(health_req, timeout=timeout) as resp:
            health = json.loads(resp.read())
        if health.get("status") != "ok":
            return None
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None

    try:
        body = json.dumps(payload).encode("utf-8")
        ask_req = urllib.request.Request(
            f"http://127.0.0.1:{_SERVER_PORT}/ask",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Generous timeout here, deliberately unlike the 0.3s health
        # check above -- a real query (indexing + ReAct loop) can
        # legitimately take tens of seconds, per this project's own
        # measured latencies (Section 13).
        with urllib.request.urlopen(ask_req, timeout=300) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None


def _run_walker(repo_path: str) -> list[dict]:
    """
    Invokes the Rust file walker as a subprocess and parses its JSON
    stdout. Section 8: the walker only reports metadata (path, size,
    language, hash, modified) — no file content — so this function's
    output still needs each file's content read separately before
    chunking.
    """
    if not os.path.isfile(_INDEXER_BINARY):
        typer.secho(
            f"Indexer binary not found at {_INDEXER_BINARY}. "
            "Build it first: cd indexer_core && cargo build --release",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    result = subprocess.run(
        [_INDEXER_BINARY, repo_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        typer.secho(f"Walker failed: {result.stderr}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    return json.loads(result.stdout)


@app.command()
def index(path: str = typer.Argument(..., help="Path to the repo to index.")):
    """
    Index a repo: walk -> hash-skip unchanged files -> chunk -> embed -> store.

    Thin wrapper over _index_repo(verbose=True) — the real orchestration
    now lives there so `ask` can run the exact same pipeline silently as
    an automatic freshness check (see ask()'s docstring for why).
    """
    repo_path = os.path.abspath(path)
    if not os.path.isdir(repo_path):
        typer.secho(f"Not a directory: {repo_path}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    _index_repo(repo_path, verbose=True)

    # Context inheritance (V4, 2026-07-23) — see memory/session_state.py's
    # module docstring. Explicitly indexing a repo is real "I'm working
    # on this project" activity, same as a successful `ask`, so it
    # updates the same global pointer `ask`'s fallback reads from.
    from memory import session_state
    session_state.set_last_active_repo(repo_path)


def _index_repo(repo_path: str, verbose: bool = True) -> dict:
    """
    Core indexing pipeline: walk -> hash-skip unchanged files -> chunk ->
    embed -> store, plus Knowledge Capsule generation. Extracted out of
    the `index` command (2026-07-11) so `ask` can run it automatically as
    a freshness precondition, not just as an explicit separate step —
    see the design conversation logged in ENGINEERING_JOURNAL.md and
    OLORIN_PROJECT.md Section 8/13 for why indexing shouldn't be a step
    the user has to remember to run.

    verbose=False (ask()'s usage) suppresses all the file-by-file/capsule-
    by-capsule output that verbose=True (the explicit `index` command)
    prints — the hash-skip logic means most `ask`-triggered calls do
    genuinely nothing, and printing a wall of "unchanged, skipped" noise
    on every single question would be worse than the manual-step problem
    this replaces. Callers decide what to tell the user based on the
    returned stats instead.

    Returns:
        dict: {"walked": int, "reindexed": int, "skipped": int,
        "total_chunks": int, "capsules_generated": int,
        "capsules_skipped": int, "elapsed": float, "is_first_index": bool}
    """
    # Deferred imports: these pull in torch/sentence-transformers/chromadb,
    # which are slow to import and unnecessary for `ask` or `--help` when
    # nothing actually needs indexing.
    from indexer.chunker import chunk_file
    from indexer.documents import extract_pdf_chunks
    from indexer.embedder import embed_chunks
    from indexer.imports import extract_raw_imports, resolve_imports
    from indexer.store import get_collection, get_indexed_file_hash, delete_file, upsert_chunks
    from indexer.capsules import generate_module_summary
    from memory import capsules as capsules_store
    from memory import import_graph as import_graph_store
    from core.llm_client import LLMClient

    start = time.time()
    if verbose:
        typer.echo(f"Walking {repo_path} ...")
    files = _run_walker(repo_path)
    if verbose:
        typer.echo(f"Walker found {len(files)} files ({time.time() - start:.2f}s)")

    collection = get_collection(repo_path)
    is_first_index = collection.count() == 0

    skipped = 0
    reindexed = 0
    total_chunks = 0

    # Full repo-relative path set, used purely as an existence check by
    # indexer/imports.py's resolve_imports() (V3, "why is this file
    # important?", Section 11) — built once here from the walker's own
    # output, not a second filesystem scan, so import resolution stays
    # consistent with whatever the rest of this pipeline already treats
    # as "the files in this repo."
    repo_files = {f["path"] for f in files}

    for f in files:
        rel_path = f["path"]
        file_hash = f["hash"]
        language = f["language"]

        existing_hash = get_indexed_file_hash(collection, rel_path)
        if existing_hash == file_hash:
            skipped += 1
            continue

        abs_path = os.path.join(repo_path, rel_path)

        # Document ingestion (V3, "PDF -> same ChromaDB index as code",
        # Section 11): detected by extension here, not by the Rust
        # walker's "language" field. The walker reports "unknown" for
        # .pdf (indexer_core/src/main.rs's detect_language() has no PDF
        # case) — extending it would mean a `cargo build --release` this
        # session has no way to run remotely. Checking the extension
        # directly on the Python side instead means zero changes to the
        # walker and zero dependency on rebuilding it: the walker already
        # discovers .pdf files fine (it walks and hashes every non-
        # gitignored file regardless of type), just under
        # language="unknown", which this branch simply doesn't trust.
        is_pdf = rel_path.lower().endswith(".pdf")

        if is_pdf:
            content = None  # PDFs never go through the text-read path below
            chunks = extract_pdf_chunks(abs_path, rel_path, repo=repo_path)
        else:
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError as e:
                if verbose:
                    typer.secho(f"  skip (read error) {rel_path}: {e}", fg=typer.colors.YELLOW)
                continue

        # Full re-chunk on any change: clear this file's old chunks first
        # so stale line-range entries from a shrunk/reshaped file don't
        # linger (see store.py's delete_file() docstring).
        if existing_hash is not None:
            delete_file(collection, rel_path)

        if not is_pdf:
            chunks = chunk_file(rel_path, content, language, repo=repo_path)

        for c in chunks:
            c["file_hash"] = file_hash
        chunks = embed_chunks(chunks)
        total_chunks += upsert_chunks(collection, chunks)

        # Import graph (V3, "why is this file important?", Section 11):
        # code-only — a PDF has no imports to extract. Skipped explicitly
        # rather than relying on extract_raw_imports() harmlessly
        # returning [] for an unmapped language ("pdf" isn't in
        # LANGUAGE_MAP either way), so the "not applicable to documents"
        # intent is visible here rather than an implicit fallthrough.
        # mechanical, no LLM call, so it rides along with every
        # (re)indexed code file rather than needing its own hash-tracking
        # table — see memory/import_graph.py's module docstring for why
        # that's a deliberate simplification, not a missing feature.
        # Best-effort: an extraction failure on one file (e.g. a genuine
        # parse error) shouldn't lose the chunking/embedding work that
        # already succeeded above for that same file.
        if not is_pdf:
            try:
                raw_imports = extract_raw_imports(content, language)
                resolved_imports = resolve_imports(rel_path, raw_imports, language, repo_files)
                import_graph_store.set_file_imports(repo_path, rel_path, resolved_imports)
            except Exception as e:
                if verbose:
                    typer.secho(f"  import graph failed for {rel_path}: {e}", fg=typer.colors.YELLOW)

        reindexed += 1
        if verbose:
            typer.echo(f"  indexed {rel_path} ({len(chunks)} chunks)")

    elapsed = time.time() - start
    if verbose:
        typer.secho(
            f"\nDone in {elapsed:.2f}s — {reindexed} file(s) (re)indexed, "
            f"{skipped} unchanged file(s) skipped, {total_chunks} chunks written.",
            fg=typer.colors.GREEN,
        )

    # --- Knowledge Capsules (module-level summaries, Section 10/13) ----
    # Group every code file (by extension — see _CODE_EXTENSIONS) by its
    # top-level directory. Loose root-level files (config.py, cli.py)
    # fall under a synthetic "root" module.
    modules: dict[str, list[dict]] = {}
    for f in files:
        rel_path = f["path"]
        ext = os.path.splitext(rel_path)[1]
        if ext not in _CODE_EXTENSIONS:
            continue
        module = rel_path.split("/")[0] if "/" in rel_path else "root"
        modules.setdefault(module, []).append({"path": rel_path, "hash": f["hash"]})

    llm_client = LLMClient()
    capsules_generated = 0
    capsules_skipped = 0

    for module, current_files in modules.items():
        if not capsules_store.needs_regeneration(repo_path, module, current_files):
            capsules_skipped += 1
            continue

        file_payloads = []
        for cf in current_files:
            abs_path = os.path.join(repo_path, cf["path"])
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    file_payloads.append({"path": cf["path"], "content": fh.read()})
            except OSError:
                continue  # unreadable — skip for capsule purposes, same as the main loop's skip-on-read-error handling

        try:
            summary = generate_module_summary(llm_client, module, file_payloads)
            capsules_store.upsert_capsule(repo_path, module, summary, current_files)
            capsules_generated += 1
            if verbose:
                typer.echo(f"  capsule: {module} ({len(current_files)} files)")
        except Exception as e:
            # Best-effort: a capsule generation failure shouldn't lose the
            # indexing work that already succeeded above.
            if verbose:
                typer.secho(f"  capsule failed for {module}: {e}", fg=typer.colors.YELLOW)

    if verbose:
        typer.secho(
            f"Capsules: {capsules_generated} generated, {capsules_skipped} unchanged.",
            fg=typer.colors.GREEN,
        )

    return {
        "walked": len(files),
        "reindexed": reindexed,
        "skipped": skipped,
        "total_chunks": total_chunks,
        "capsules_generated": capsules_generated,
        "capsules_skipped": capsules_skipped,
        "elapsed": time.time() - start,
        "is_first_index": is_first_index,
    }


@app.command()
def ask(
    query: str = typer.Argument(..., help="Your question about the codebase."),
    path: str | None = typer.Option(
        None, "--path", "-p",
        help=(
            "Repo root to query. Defaults to the current directory; if "
            "that directory hasn't been indexed yet and has no OLORIN.md, "
            "falls back to your last active project instead (V4, context "
            "inheritance — see memory/session_state.py). An explicit "
            "--path always overrides both."
        ),
    ),
    repos: str = typer.Option(
        None, "--repos",
        help=(
            "Comma-separated additional repo paths to search alongside "
            "--path (cross-repo querying, V3) — e.g. "
            "--repos ../other-repo,../third-repo. search_codebase spans "
            "all of them; every other tool stays scoped to --path only."
        ),
    ),
    provider: str = typer.Option(
        "auto", "--provider",
        help="Force a specific backend: groq | cerebras | boromir | faramir | auto.",
    ),
    force_local: bool = typer.Option(
        False, "--force-local", help="Never call the cloud provider (Groq)."
    ),
    profile: bool = typer.Option(
        False, "--profile",
        help="Print a per-stage latency breakdown (V1.5 instrumentation, OLORIN_PROJECT.md Section 11).",
    ),
    skip_index: bool = typer.Option(
        False, "--skip-index",
        help="Skip the automatic index-freshness check (advanced; answers may be stale or ungrounded).",
    ),
    no_server: bool = typer.Option(
        False, "--no-server",
        help="Always use the direct in-process path, even if an Olorin Server is running (V5 Phase 2).",
    ),
):
    """
    Ask Olorin a question about a repo. Thin wrapper around the already-
    proven Agent.run() (core/agent.py) — per Section 7's CLI flags and
    Section 9's ReAct loop.

    Prefers an already-running Olorin Server (server/app.py, V5 Phase 2)
    if one's reachable on localhost, routing the request through it
    instead of building a fresh in-process Agent — avoids the ~6-10s
    cold-start tax on every call when a server happens to already be up.
    Falls back silently to the direct path below if no server is
    running, which is the common case and behaves identically to before
    this existed. --no-server forces the direct path explicitly;
    --profile does the same implicitly, since it measures this process's
    own cost, not a server's.

    Runs the indexing pipeline (_index_repo()) as an automatic precondition
    before answering, unless --skip-index is passed. This replaced an
    earlier design where indexing was purely a separate manual step —
    real product experience shouldn't require a user to think in terms of
    "have I indexed yet" before they can ask a question about a repo they
    just pointed Olorin at (design conversation logged 2026-07-11,
    OLORIN_PROJECT.md Section 8/13). This is NOT a forced full reindex on
    every call: the hash-skip logic already built into _index_repo() means
    a call against an unchanged repo does almost no real work (a fast walk
    + hash comparison, no re-chunking/re-embedding/re-generating anything)
    — the honest cost only shows up on a genuinely first-time or changed
    repo, which is real, unavoidable work either way.
    """
    profiling.reset()

    with profiling.span("agent_import_chain", top_level=True):
        # This cascades into core.llm_client -> providers (openai, ollama
        # libs) -> tools.registry -> tools.search -> indexer.store
        # (chromadb) -> indexer.embedder (torch, sentence-transformers).
        # tools/registry.py imports tools/search.py at module level, so
        # this single import already pays the full heavy-library import
        # cost even if the agent never ends up calling search_codebase —
        # exactly the kind of thing this measurement work exists to find.
        from core.agent import Agent
    from core.llm_client import select_local_specialist

    # Context inheritance / auto-switch on repo change (V4, 2026-07-23,
    # memory/session_state.py). Only kicks in when --path wasn't passed
    # explicitly — an explicit --path always wins outright, same
    # "explicit flag beats an inferred default" precedent as direct-
    # address routing (Section 7) and --force-local's specialist choice
    # (Section 13, 2026-07-21). `ask` already defaulted --path to ".",
    # so cd-into-repo-and-ask already worked before this — the real gap
    # this closes is narrower: what happens when the CURRENT directory
    # ISN'T the repo you mean (you're in ~, or a future no-cwd surface
    # like a system tray icon). In that case, fall back to whatever repo
    # was last active instead of silently trying to index/query wherever
    # you happen to be standing.
    from memory import session_state

    path_was_explicit = path is not None
    repo_path = os.path.abspath(path) if path_was_explicit else os.path.abspath(os.getcwd())

    if not path_was_explicit:
        from indexer.store import collection_exists

        looks_like_known_repo = (
            os.path.isfile(os.path.join(repo_path, "OLORIN.md"))
            or collection_exists(repo_path)
        )
        if not looks_like_known_repo:
            last_repo = session_state.get_last_active_repo()
            if last_repo and os.path.isdir(last_repo):
                typer.secho(
                    f"No project specified, and this directory hasn't been "
                    f"indexed — resuming last active project: {last_repo} "
                    f"(cd there, or pass --path, to use something else).",
                    fg=typer.colors.CYAN,
                )
                repo_path = last_repo
            # else: no prior session AND this directory looks unfamiliar
            # — fall through with cwd anyway, same honest handling
            # index-on-demand already gives any never-before-seen repo.

    if not os.path.isdir(repo_path):
        typer.secho(f"Not a directory: {repo_path}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Honest breadcrumb, not just for the fallback case above — also
    # fires on an ordinary explicit --path to a different repo than last
    # time. Silent when there's no prior session (nothing to compare
    # against) or the repo didn't actually change (including the
    # fallback case itself, where repo_path now equals last_repo by
    # construction, so this deliberately does not double-announce it).
    previous_active = session_state.get_last_active_repo()
    if previous_active and previous_active != repo_path:
        typer.secho(f"Switched context: {previous_active} -> {repo_path}", fg=typer.colors.CYAN)
    session_state.set_last_active_repo(repo_path)

    # Cross-repo querying (V3, "cross-repo querying", Section 11): each
    # extra repo is validated up front, the same way the primary --path
    # already is — fail fast on a typo'd path rather than discovering it
    # mid-indexing. Comma-separated (--repos a,b,c), chosen directly over
    # repeated --path-style flags for this option specifically, since a
    # single string is simpler to parse than typer's multi-value option
    # handling for what's expected to be an occasional, short list.
    extra_repo_paths: list[str] = []
    if repos:
        for r in repos.split(","):
            r = r.strip()
            if not r:
                continue
            abs_r = os.path.abspath(r)
            if not os.path.isdir(abs_r):
                typer.secho(f"Not a directory: {abs_r}", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            extra_repo_paths.append(abs_r)

    # Olorin Server fast path (V5 Phase 2, OLORIN_PROJECT.md Section 11)
    # -- opportunistic, not required. If a server happens to already be
    # running (e.g. launched for a TUI session, or manually for a batch
    # of queries), route through it instead of building a fresh
    # in-process Agent, avoiding the ~6-10s cold-start tax measured back
    # in V1.5. Falls through silently to the existing direct path below
    # if no server is reachable -- the overwhelmingly common case for a
    # one-shot `ask` call, and unchanged from before this existed.
    # --profile forces the direct path regardless: profiling measures
    # THIS process's own cost, and a server-routed call would only
    # measure the HTTP round-trip, not the real work happening in the
    # other process.
    if not no_server and not profile:
        server_result = _try_server_ask({
            "query": query,
            "path": repo_path,
            "repos": repos,
            "provider": provider,
            "force_local": force_local,
            "skip_index": skip_index,
        })
        if server_result is not None:
            if "error" in server_result:
                typer.secho(f"Server error: {server_result['error']}", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            typer.echo(server_result["answer"])
            return

    if not skip_index:
        from indexer.store import get_collection

        # Peeked at before running so a genuinely first-time index gets an
        # honest "this will take a moment" message up front, rather than
        # the user staring at a silent terminal for 30-90s wondering if
        # something's hung.
        was_empty = get_collection(repo_path).count() == 0
        if was_empty:
            typer.secho(
                f"First time indexing {repo_path} — this may take a minute...",
                fg=typer.colors.CYAN,
            )

        with profiling.span("auto_index_check", top_level=True):
            stats = _index_repo(repo_path, verbose=False)

        # Only say something if real work actually happened — the common
        # case (nothing changed) should stay silent, not print freshness-
        # check noise on every single question.
        if not was_empty and (stats["reindexed"] > 0 or stats["capsules_generated"] > 0):
            typer.secho(
                f"Repo changed since last index: {stats['reindexed']} file(s) "
                f"updated, {stats['capsules_generated']} capsule(s) "
                f"regenerated ({stats['elapsed']:.1f}s).",
                fg=typer.colors.CYAN,
            )

        # Each extra repo gets the identical automatic freshness check as
        # the primary repo — consistent with index-on-demand's whole
        # premise (Section 8/13): a cross-repo query shouldn't require the
        # user to have separately remembered to run `index` on every repo
        # first. --skip-index skips this for extras too, same flag,
        # same intent ("I know what I'm doing, don't index anything").
        for extra_path in extra_repo_paths:
            was_empty = get_collection(extra_path).count() == 0
            if was_empty:
                typer.secho(
                    f"First time indexing {extra_path} — this may take a minute...",
                    fg=typer.colors.CYAN,
                )

            with profiling.span("auto_index_check", top_level=True):
                extra_stats = _index_repo(extra_path, verbose=False)

            if not was_empty and (extra_stats["reindexed"] > 0 or extra_stats["capsules_generated"] > 0):
                typer.secho(
                    f"{extra_path} changed since last index: "
                    f"{extra_stats['reindexed']} file(s) updated, "
                    f"{extra_stats['capsules_generated']} capsule(s) "
                    f"regenerated ({extra_stats['elapsed']:.1f}s).",
                    fg=typer.colors.CYAN,
                )

    force_provider = None
    if force_local:
        # Real gap found + fixed (2026-07-21, closing out specialist-
        # routing's loose ends): this used to hardcode force_provider =
        # "boromir" unconditionally, meaning --force-local always meant
        # Boromir regardless of what the query actually was — bypassing
        # the exact specialist heuristic (core/llm_client.py's
        # select_local_specialist()) that the internal auto-mode-
        # falls-through-to-local path already uses. No principled reason
        # --force-local should mean something narrower ("never call
        # cloud, and also always Boromir specifically") than "never call
        # cloud, but otherwise route normally" — so it now gets the same
        # persona choice any other local-bound query would.
        force_provider = select_local_specialist(query)
    elif provider != "auto":
        force_provider = provider

    try:
        with profiling.span("total_request", top_level=True):
            agent = Agent(repo_root=repo_path, extra_repo_roots=extra_repo_paths)
            answer = agent.run(query, force_provider=force_provider)
    except Exception as e:
        # Safety net for whatever LLMClient._route() couldn't already
        # recover from itself (Section 13, 2026-07-12 fallback fix) — a
        # forced local provider failing (e.g. Ollama not running) has no
        # further automatic fallback to try without contradicting what
        # the user explicitly asked for, so the honest move is a clean
        # error, not a raw traceback dump.
        typer.secho(f"Request failed: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.echo(answer)

    if profile:
        external_total_ms = (time.perf_counter() - _MODULE_START) * 1000
        typer.echo("\n" + profiling.report(external_total_ms=external_total_ms))


@app.command()
def history(
    path: str = typer.Option(".", "--path", "-p", help="Repo root to show history for."),
    limit: int = typer.Option(10, "--limit", "-n", help="Max conversations to show."),
    search: str = typer.Option(None, "--search", "-s", help="Keyword-overlap search instead of most-recent."),
):
    """
    Browse episodic memory (Section 10) for a repo — mainly here so the
    write path (Agent.run() -> memory/history.py) can be verified live
    without reaching for a raw sqlite3 shell, and as a quick way to eyeball
    what search_history's tool would see. Not a V2 checklist item on its
    own; a thin, low-cost addition on top of memory/history.py, which does
    the real work.
    """
    from memory import history as history_store

    repo_path = os.path.abspath(path)

    if search:
        records = history_store.search_history(search, repo_path=repo_path, limit=limit)
    else:
        records = history_store.get_recent(repo_path=repo_path, limit=limit)

    if not records:
        typer.echo("No conversations logged for this repo yet.")
        return

    for r in records:
        score = r["complexity_score"] if r["complexity_score"] is not None else "n/a"
        tools = ", ".join(r["tools_used"]) or "none"
        typer.secho(f"[{r['timestamp']}] backend={r['backend_used']} complexity={score}", fg=typer.colors.CYAN)
        typer.echo(f"  Q: {r['user_query']}")
        typer.echo(f"  A: {r['agent_response'][:200]}{'...' if len(r['agent_response']) > 200 else ''}")
        typer.echo(f"  tools: {tools}\n")


@app.command()
def journal(
    path: str = typer.Option(".", "--path", "-p", help="Repo root to generate a journal entry for."),
    date: str = typer.Option(None, "--date", "-d", help="Date to generate, YYYY-MM-DD. Defaults to today."),
    no_llm: bool = typer.Option(
        False, "--no-llm",
        help="Skip the 'Worked on' synthesis (Faramir); use a literal deduplicated query list instead.",
    ),
):
    """
    Generate (or regenerate) an Engineering Journal entry for a repo from
    episodic memory (Section 10) — automates what this project's own
    ENGINEERING_JOURNAL.md has been maintained as by hand all along.
    Writes to ~/.olorin/journal/<repo_hash>/<date>.md; see
    memory/journal.py for why journals live outside the indexed repo
    (same reasoning as ChromaDB's storage location, Section 8).

    Deliberately a separate, explicit command rather than something
    `ask` triggers automatically on every call, unlike index-on-demand —
    see memory/journal.py's module docstring for why (regenerating a
    journal makes a real LLM call every time; a repeat index check does
    not).
    """
    from memory import journal as journal_store
    from core.llm_client import LLMClient

    repo_path = os.path.abspath(path)
    llm_client = None if no_llm else LLMClient()

    result_path = journal_store.write_journal(repo_path, date=date, llm_client=llm_client)
    if result_path is None:
        target_date = date or time.strftime("%Y-%m-%d")
        typer.echo(f"No conversations logged for {repo_path} on {target_date} — nothing to write.")
        return

    typer.secho(f"Journal written: {result_path}", fg=typer.colors.GREEN)


@app.command()
def entities(
    path: str = typer.Option(".", "--path", "-p", help="Repo root to show entities for."),
    limit: int = typer.Option(10, "--limit", "-n", help="Max entities to show."),
    search: str = typer.Option(None, "--search", "-s", help="Keyword-overlap search instead of most-recently-updated."),
):
    """
    Browse entity memory (decisions and concepts extracted from past runs
    — see memory/entities.py's module docstring for the design) for a
    repo. Same purpose as the `history` command: lets the write path
    (Agent._extract_entities() -> memory/entities.py) be verified live
    without a raw sqlite3 shell, and doubles as a quick way to eyeball
    what search_entities' tool would see.
    """
    from memory import entities as entities_store

    repo_path = os.path.abspath(path)

    if search:
        records = entities_store.search_entities(search, repo_path=repo_path, limit=limit)
    else:
        records = entities_store.get_all_entities(repo_path, limit=limit)

    if not records:
        typer.echo("No entities remembered for this repo yet.")
        return

    for e in records:
        typer.secho(f"[{e['last_updated']}] {e['type']}: {e['name']}", fg=typer.colors.CYAN)
        typer.echo(f"  {e['description']}\n")


if __name__ == "__main__":
    app()
