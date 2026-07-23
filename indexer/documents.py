"""
indexer/documents.py — PDF text extraction and page-level chunking
(V3, "document ingestion", OLORIN_PROJECT.md Section 11).

Parallels indexer/chunker.py's role for code, but PDFs have no AST to
walk — page boundaries are the natural chunkable unit instead of
function/class boundaries. Deliberately bypasses chunker.py entirely
rather than forcing PDFs through the same code path: chunker.py's
contract starts from already-decoded TEXT content (Section 8: the walker
reports metadata only, the caller reads+decodes file content, THEN
passes it to chunk_file()) — a PDF is binary, needs its own
library-based extraction, and produces chunks page-by-page rather than
node-by-node. cli.py's index() command dispatches to extract_pdf_chunks()
directly for .pdf files (detected by extension, not by the Rust walker's
"language" field — see cli.py's own comment on why), never routing them
through chunk_file() at all.

Uses pdfplumber (MIT licensed), chosen directly over pypdf for
extraction quality on real-world PDF layouts (columns, tables) — a real
trade-off (heavier dependency chain via pdfminer.six) worth naming
rather than defaulting to the lighter option without discussion.
"""

from __future__ import annotations

import sys

import pdfplumber

# Same reasoning as list_files' max_files cap (bug #9, OLORIN_PROJECT.md
# Section 13): an unbounded document-ingestion path is exactly the kind
# of thing that works fine until it doesn't — a very large PDF (a full
# textbook, a huge spec) could otherwise produce thousands of chunks and
# tie up indexing for a single file. Added proactively this time, having
# already learned that lesson the hard way with list_files rather than
# waiting to hit it again with a different file type.
MAX_PDF_PAGES = 500


def extract_pdf_chunks(abs_path: str, rel_path: str, repo: str) -> list[dict]:
    """
    Extracts a PDF's text, one chunk per page, matching the same chunk
    contract chunker.py's chunk_file() produces (content, type, name,
    file, language, lines, repo) so the rest of the pipeline
    (embedder.py's embed_chunks(), store.py's upsert_chunks()) needs no
    changes to accept these alongside code chunks in the SAME ChromaDB
    collection.

    "lines" is deliberately set to a page-number pseudo-range ("N-N")
    rather than a real source line range — reusing the existing
    contract's id-stability mechanism (store.py's _chunk_id() hashes
    file+lines) instead of inventing a parallel "page" identity scheme.
    This also means embedder.py's _split_oversized_chunks() — which
    parses "lines" as int(lines.split("-")[0]) to re-derive real
    positions for its own sub-chunks — works completely UNMODIFIED for
    oversized PDF pages. Verified directly (not assumed) against a real
    multi-page PDF with one deliberately oversized page before this was
    considered done: the split produced genuinely unique, non-colliding
    chunk IDs, the same guarantee bugs #11/#12 established for code.

    Blank/image-only pages (no extractable text layer) are silently
    skipped, not stored as empty chunks — an empty embedding would just
    be dead weight in the index with nothing for search to usefully
    match against.

    Returns an empty list (not an exception) if the PDF can't be opened
    at all (corrupted, password-protected, or any other pdfplumber
    failure) — the caller (cli.py) treats this the same as any other
    per-file read failure: log a warning, skip the file, keep indexing
    the rest of the repo. Verified directly against a real corrupted
    file and a nonexistent path, not just reasoned about.
    """
    try:
        pdf = pdfplumber.open(abs_path)
    except Exception as e:
        print(f"  PDF open failed for {rel_path}: {e}", file=sys.stderr)
        return []

    chunks: list[dict] = []
    try:
        total_pages = len(pdf.pages)
        truncated = total_pages > MAX_PDF_PAGES
        pages_to_process = pdf.pages[:MAX_PDF_PAGES]

        if truncated:
            print(
                f"  WARNING: {rel_path} has {total_pages} pages, "
                f"only indexing the first {MAX_PDF_PAGES} "
                "(MAX_PDF_PAGES cap, indexer/documents.py).",
                file=sys.stderr,
            )

        for page_num, page in enumerate(pages_to_process, start=1):
            try:
                text = page.extract_text()
            except Exception as e:
                # One malformed page shouldn't lose every other page in
                # the same document — same "best effort per unit, don't
                # let one failure crash the whole file" posture as
                # cli.py's per-file try/except around import-graph
                # extraction (Section 11).
                print(f"  page {page_num} extraction failed in {rel_path}: {e}", file=sys.stderr)
                continue

            if not text or not text.strip():
                continue  # blank/image-only page, nothing to index

            chunks.append({
                "content": text,
                "type": "pdf_page",
                "name": f"Page {page_num}",
                "file": rel_path,
                "language": "pdf",
                "lines": f"{page_num}-{page_num}",
                "repo": repo,
            })
    finally:
        pdf.close()

    return chunks


if __name__ == "__main__":
    # Manual smoke test: python -m indexer.documents <path_to_pdf>
    if len(sys.argv) < 2:
        print("usage: python -m indexer.documents <path_to_pdf>")
        sys.exit(1)

    from pathlib import Path

    test_path = sys.argv[1]
    result = extract_pdf_chunks(test_path, Path(test_path).name, repo=".")
    print(f"{len(result)} chunk(s):\n")
    for c in result:
        print(f"  [{c['type']:10s}] {c['name']:20s} lines {c['lines']:>10s}  ({len(c['content'])} chars)")
