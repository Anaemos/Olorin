# indexer/store.py — ChromaDB persistence layer.
#
# Takes embedded chunk dicts (chunker.py -> embedder.py -> here) and
# handles storage + retrieval. This is the last stage of the indexing
# pipeline described in OLORIN_PROJECT.md Section 8.
#
# DESIGN DECISION (deviates from the casual assumption in an earlier
# .gitignore entry that chroma_db/ lives inside the indexed repo):
# ChromaDB data is stored in ONE persistent client at ~/.olorin/chroma_db,
# never inside the repo being indexed. Indexing someone else's project
# should never write a chroma_db/ folder into their working tree. Each
# indexed repo gets its own Chroma *collection* within that single
# persistent store, named via a hash of the repo's resolved absolute
# path — index the same repo twice, same collection is reused; index two
# different repos, no collisions, no cross-contamination.

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import chromadb

from core import profiling
from indexer.embedder import embed_query

PERSIST_DIR = Path.home() / ".olorin" / "chroma_db"

_client: chromadb.ClientAPI | None = None


def get_client() -> chromadb.ClientAPI:
    """Lazily creates and caches a single PersistentClient for the whole
    process. Chroma's PersistentClient is safe to reuse across calls —
    creating a new one per operation would just reopen the same on-disk
    store repeatedly for no benefit."""
    global _client
    if _client is None:
        with profiling.span("chroma_init"):
            PERSIST_DIR.mkdir(parents=True, exist_ok=True)
            _client = chromadb.PersistentClient(path=str(PERSIST_DIR))
    return _client


def _collection_name(repo_path: str) -> str:
    """Derives a stable, valid Chroma collection name from a repo path.
    Chroma collection names must be 3-63 chars, alphanumeric plus
    underscore/hyphen, start/end alphanumeric — a raw path (colons,
    backslashes, spaces) doesn't qualify, so we hash it instead. Using
    the *resolved* absolute path means the same repo indexed from two
    different working directories (e.g. relative vs. absolute paths at
    the CLI) still maps to the same collection."""
    resolved = str(Path(repo_path).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"repo_{digest}"


def get_collection(repo_path: str) -> chromadb.Collection:
    """Gets or creates the Chroma collection for a given repo. Safe to
    call every time you index or query — get_or_create is idempotent."""
    client = get_client()
    return client.get_or_create_collection(
        name=_collection_name(repo_path),
        metadata={"repo_path": str(Path(repo_path).resolve())},
    )


def collection_exists(repo_path: str) -> bool:
    """
    Read-only existence check — unlike get_collection(), this NEVER
    creates a collection as a side effect. get_or_create_collection()
    means get_collection(x).count() > 0 as an "is this a known repo?"
    probe silently litters the shared Chroma store with a permanent
    empty collection for every directory anyone ever happens to run
    `ask` from, even one that was never meant to be indexed (home
    directory, a random script's cwd). Added for cli.py's context-
    inheritance fallback (V4, 2026-07-23), which needs to answer "is
    this a real, previously-indexed repo" without that answer itself
    being a write.
    """
    client = get_client()
    name = _collection_name(repo_path)
    try:
        client.get_collection(name=name)
        return True
    except Exception:
        return False


def _chunk_id(chunk: dict) -> str:
    """Stable ID for a chunk, derived from its location (file + line
    range), NOT its content. This is what makes re-indexing an
    idempotent upsert rather than an ever-growing pile of duplicates:
    if the function at auth.py:12-28 gets edited, its ID stays the same
    and upsert() overwrites the old embedding/content in place. If the
    function moves to different line numbers, that's correctly treated
    as a new chunk — the old ID's entry becomes stale and orphaned
    until the next full re-index of that file cleans it up (see
    delete_file() below)."""
    raw = f"{chunk['file']}::{chunk['lines']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upsert_chunks(collection: chromadb.Collection, chunks: list[dict]) -> int:
    """
    Writes embedded chunks into the collection. Expects each chunk dict
    to already have an "embedding" key (from indexer/embedder.py's
    embed_chunks()) in addition to the Section 8 contract fields
    (content, type, name, file, language, lines, repo). An optional
    "file_hash" field, if present, is stored as metadata so future
    indexing runs can compare it against the Rust walker's reported
    hash and skip re-chunking/re-embedding unchanged files entirely —
    that comparison itself is the indexing orchestrator's job (cli.py),
    not this module's; store.py only persists what it's given and
    exposes get_indexed_file_hash() to make that comparison possible.

    Returns the number of chunks written.
    """
    if not chunks:
        return 0

    ids = [_chunk_id(c) for c in chunks]

    # Defense-in-depth (2026-07-12): de-duplicate by ID before handing
    # off to Chroma, which raises a hard DuplicateIDError and aborts the
    # WHOLE upsert (including every other legitimate chunk in the batch)
    # if even one collision slips through. The real fix for the one
    # collision cause found so far lives in embedder.py's
    # _split_oversized_chunks() (see its docstring) — this is a backstop
    # for any future collision cause, not a substitute for fixing root
    # causes at the layer that produces them. Keeps the LAST occurrence
    # of a duplicate ID, consistent with upsert semantics (last write
    # wins) rather than silently preferring whichever happened to sort
    # first.
    seen: dict[str, int] = {}
    for i, cid in enumerate(ids):
        seen[cid] = i  # later index overwrites earlier ones for the same id
    if len(seen) < len(ids):
        import sys
        print(
            f"WARNING: {len(ids) - len(seen)} duplicate chunk id(s) "
            f"collapsed before upsert — see upsert_chunks()'s docstring.",
            file=sys.stderr,
        )
        keep_indices = sorted(seen.values())
        chunks = [chunks[i] for i in keep_indices]
        ids = [ids[i] for i in keep_indices]

    embeddings = [c["embedding"] for c in chunks]
    documents = [c["content"] for c in chunks]
    metadatas = [
        {
            "type": c["type"],
            "name": c["name"],
            "file": c["file"],
            "language": c["language"],
            "lines": c["lines"],
            "repo": c["repo"],
            **({"file_hash": c["file_hash"]} if "file_hash" in c else {}),
        }
        for c in chunks
    ]

    # Real bug found live (2026-07-2x): Chroma's SQLite-backed client
    # enforces a hard cap on items per upsert() call -- exceeding it
    # isn't a soft warning, it's a ValueError that aborts the ENTIRE
    # call, none of the batch gets written. First surfaced once this
    # repo's own accumulated docs (ENGINEERING_JOURNAL* archives,
    # OLORIN_PROJECT.md) pushed a single reindex run's total chunk count
    # past the limit for the first time -- no earlier session's indexing
    # run had ever produced enough chunks in one call to hit it. Fixed by
    # asking Chroma's own client for its actual current limit
    # (get_max_batch_size(), a real public method as of chromadb 1.x --
    # NOT hardcoding a guessed constant, since this cap is derived from
    # the underlying SQLite build's own parameter-count limit and isn't
    # guaranteed stable across Chroma/SQLite versions) and slicing the
    # upsert into sequential sub-batches at or under it.
    max_batch = get_client().get_max_batch_size()
    total = len(ids)
    for start in range(0, total, max_batch):
        end = min(start + max_batch, total)
        collection.upsert(
            ids=ids[start:end],
            embeddings=embeddings[start:end],
            documents=documents[start:end],
            metadatas=metadatas[start:end],
        )
    return total


def delete_file(collection: chromadb.Collection, file_path: str) -> None:
    """Removes every chunk belonging to a given file. Needed before a
    full re-chunk of a changed file, so chunks at line ranges that no
    longer exist (because the file was edited) don't linger as stale
    orphaned entries — upsert() alone only overwrites IDs that still
    match; it can't know an old ID's range disappeared."""
    collection.delete(where={"file": file_path})


def get_indexed_file_hash(collection: chromadb.Collection, file_path: str) -> str | None:
    """Looks up the stored file_hash for any chunk belonging to this
    file. Returns None if the file has no indexed chunks yet, or if
    chunks exist but predate the file_hash field being added. The
    indexing orchestrator compares this against the Rust walker's
    freshly-computed hash to decide whether to skip re-indexing a file
    entirely — this is where the walker's hash-based change detection
    (OLORIN_PROJECT.md Section 8) actually pays off downstream."""
    result = collection.get(where={"file": file_path}, limit=1, include=["metadatas"])
    metadatas = result.get("metadatas") or []
    if not metadatas:
        return None
    return metadatas[0].get("file_hash")


def query(collection: chromadb.Collection, query_text: str, n_results: int = 5) -> list[dict]:
    """
    Semantic search over the collection. Embeds query_text via
    embed_query() (applying BGE's query instruction prefix — see
    indexer/embedder.py) and returns the top n_results as a flat list
    of {content, metadata, distance} dicts, closer to the shape
    search_codebase() actually wants to hand back to the agent than
    Chroma's native nested-lists-of-lists query() response shape.
    """
    query_embedding = embed_query(query_text)
    with profiling.span("vector_search"):
        raw = collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )

    results = []
    documents = raw.get("documents") or [[]]
    metadatas = raw.get("metadatas") or [[]]
    distances = raw.get("distances") or [[]]
    for content, metadata, distance in zip(documents[0], metadatas[0], distances[0]):
        results.append({"content": content, "metadata": metadata, "distance": distance})
    return results


def query_multi(repo_paths: list[str], query_text: str, n_results: int = 5) -> list[dict]:
    """
    Cross-repo semantic search (V3, "cross-repo querying", Section 11).

    Queries each repo's own Chroma collection independently, then merges
    and re-ranks results GLOBALLY by raw distance — a real design
    decision, not the only option (a fixed top-N-per-repo quota was
    considered and deliberately rejected): a query heavily relevant to
    one repo and barely relevant to another should surface mostly (or
    entirely) the relevant repo's chunks, rather than artificially
    padding results from an unrelated repo just to guarantee
    representation.

    Comparing distances across genuinely different Chroma collections is
    only valid because every collection in this project shares the same
    embedding model (bge-small-en-v1.5, indexer/embedder.py) and the same
    distance metric (Chroma's default HNSW space — get_collection() never
    overrides it per-collection). This is an explicit assumption worth
    naming, not an incidental fact: if a future version ever let
    different repos use different embedding models, this function's
    cross-collection sort would silently become meaningless, comparing
    numbers from two different spaces as if they meant the same thing.

    The query is embedded ONCE and reused across every collection's raw
    .query() call, not once per repo — avoids N redundant GPU embedding
    calls for what is, from the embedding model's perspective, the exact
    same input every time.

    Repos with an empty (not-yet-indexed) collection are silently
    skipped, not treated as an error — a multi-repo query where only
    some repos happen to be indexed should still return what it can from
    the ones that are, the same "honest partial result over an all-or-
    nothing failure" posture as every other tool in this project.

    Each result dict carries its "repo" field inside "metadata" (already
    stored per-chunk by upsert_chunks() — Section 8's chunk contract),
    so a merged, cross-repo result list is still attributable to the
    repo each chunk actually came from.
    """
    query_embedding = embed_query(query_text)

    all_results: list[dict] = []
    for repo_path in repo_paths:
        collection = get_collection(repo_path)
        if collection.count() == 0:
            continue  # not indexed yet — skip silently, not an error

        with profiling.span("vector_search"):
            raw = collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )

        documents = raw.get("documents") or [[]]
        metadatas = raw.get("metadatas") or [[]]
        distances = raw.get("distances") or [[]]
        for content, metadata, distance in zip(documents[0], metadatas[0], distances[0]):
            all_results.append({"content": content, "metadata": metadata, "distance": distance})

    all_results.sort(key=lambda r: r["distance"])
    return all_results[:n_results]


if __name__ == "__main__":
    # Smoke test: python -m indexer.store
    # Chunks + embeds core/agent.py for real (using the actual chunker
    # and embedder, not fake data), stores it under a "test" repo
    # collection, then runs a real semantic query against it end to end.
    from indexer.chunker import chunk_file

    test_repo = "."
    test_file = "core/agent.py"

    with open(test_file, "r", encoding="utf-8") as f:
        content = f.read()

    chunks = chunk_file(test_file, content, "python", repo=test_repo)
    print(f"Chunked {test_file} into {len(chunks)} chunks")

    from indexer.embedder import embed_chunks
    chunks = embed_chunks(chunks)
    print("Embedded chunks")

    collection = get_collection(test_repo)
    count = upsert_chunks(collection, chunks)
    print(f"Upserted {count} chunks into collection '{collection.name}'")

    test_query = "how does the agent loop handle tool calls"
    results = query(collection, test_query, n_results=3)
    print(f"\nQuery: {test_query!r}\n")
    for r in results:
        m = r["metadata"]
        print(f"  [{m['type']}] {m['name']} ({m['file']}:{m['lines']})  distance={r['distance']:.4f}")
