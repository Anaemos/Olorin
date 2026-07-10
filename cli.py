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

# Where the Rust walker binary lands after `cargo build --release`.
# Matches the standard Cargo output layout; not user-configurable in V1
# since indexer_core/ is a fixed part of this repo's own layout (as
# opposed to the *indexed* repo, which is arbitrary).
_INDEXER_BINARY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "indexer_core", "target", "release",
    "indexer_core.exe" if os.name == "nt" else "indexer_core",
)


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
    """
    # Deferred imports: these pull in torch/sentence-transformers/chromadb,
    # which are slow to import and unnecessary for `ask` or `--help`.
    from indexer.chunker import chunk_file
    from indexer.embedder import embed_chunks
    from indexer.store import get_collection, get_indexed_file_hash, delete_file, upsert_chunks

    repo_path = os.path.abspath(path)
    if not os.path.isdir(repo_path):
        typer.secho(f"Not a directory: {repo_path}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    start = time.time()
    typer.echo(f"Walking {repo_path} ...")
    files = _run_walker(repo_path)
    typer.echo(f"Walker found {len(files)} files ({time.time() - start:.2f}s)")

    collection = get_collection(repo_path)

    skipped = 0
    reindexed = 0
    total_chunks = 0

    for f in files:
        rel_path = f["path"]
        file_hash = f["hash"]
        language = f["language"]

        existing_hash = get_indexed_file_hash(collection, rel_path)
        if existing_hash == file_hash:
            skipped += 1
            continue

        abs_path = os.path.join(repo_path, rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as e:
            typer.secho(f"  skip (read error) {rel_path}: {e}", fg=typer.colors.YELLOW)
            continue

        # Full re-chunk on any change: clear this file's old chunks first
        # so stale line-range entries from a shrunk/reshaped file don't
        # linger (see store.py's delete_file() docstring).
        if existing_hash is not None:
            delete_file(collection, rel_path)

        chunks = chunk_file(rel_path, content, language, repo=repo_path)
        for c in chunks:
            c["file_hash"] = file_hash
        chunks = embed_chunks(chunks)
        total_chunks += upsert_chunks(collection, chunks)
        reindexed += 1
        typer.echo(f"  indexed {rel_path} ({len(chunks)} chunks)")

    elapsed = time.time() - start
    typer.secho(
        f"\nDone in {elapsed:.2f}s — {reindexed} file(s) (re)indexed, "
        f"{skipped} unchanged file(s) skipped, {total_chunks} chunks written.",
        fg=typer.colors.GREEN,
    )


@app.command()
def ask(
    query: str = typer.Argument(..., help="Your question about the codebase."),
    path: str = typer.Option(".", "--path", "-p", help="Repo root to query."),
    provider: str = typer.Option(
        "auto", "--provider",
        help="Force a specific backend: groq | boromir | faramir | auto.",
    ),
    force_local: bool = typer.Option(
        False, "--force-local", help="Never call the cloud provider (Groq)."
    ),
    profile: bool = typer.Option(
        False, "--profile",
        help="Print a per-stage latency breakdown (V1.5 instrumentation, OLORIN_PROJECT.md Section 11).",
    ),
):
    """
    Ask Olorin a question about a repo. Thin wrapper around the already-
    proven Agent.run() (core/agent.py) — per Section 7's CLI flags and
    Section 9's ReAct loop.
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

    repo_path = os.path.abspath(path)
    if not os.path.isdir(repo_path):
        typer.secho(f"Not a directory: {repo_path}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    force_provider = None
    if force_local:
        force_provider = "boromir"
    elif provider != "auto":
        force_provider = provider

    with profiling.span("total_request", top_level=True):
        agent = Agent(repo_root=repo_path)
        answer = agent.run(query, force_provider=force_provider)

    typer.echo(answer)

    if profile:
        external_total_ms = (time.perf_counter() - _MODULE_START) * 1000
        typer.echo("\n" + profiling.report(external_total_ms=external_total_ms))


if __name__ == "__main__":
    app()
