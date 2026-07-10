# indexer/chunker.py — AST-aware code chunking via tree-sitter.
#
# Takes raw file content + a language label and produces semantically
# meaningful chunks (functions, classes, methods, structs, ...) instead of
# naive fixed-size line splits. A chunk is "one function," not "500
# characters that happen to end mid-function" — this is the whole reason
# to use tree-sitter instead of a text splitter.
#
# Consumes the {path, language, ...} metadata the Rust file walker
# (indexer_core/) produces. Does NOT do any file walking or reading itself
# — the walker reports metadata only (no file content, to keep its JSON
# output small and keep all text/encoding handling in Python), so content
# is read by whatever orchestrates walker -> chunker -> embedder and
# passed in here directly.

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from tree_sitter_language_pack import get_parser

# Maps our internal language labels (set by indexer_core/src/main.rs's
# detect_language()) to tree-sitter-language-pack's grammar names. Most
# match directly; a couple of ours don't correspond to a real grammar
# (e.g. "cpp_header" — headers use the same grammar as .cpp files).
LANGUAGE_MAP: dict[str, str] = {
    "python": "python",
    "rust": "rust",
    "javascript": "javascript",
    "typescript": "typescript",
    "go": "go",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "cpp_header": "cpp",
}

# Per-language: which AST node types count as a chunkable unit, and what
# label to give that chunk. Deliberately conservative for V1 — functions,
# methods, classes/structs/impls/interfaces. Not every language construct
# needs its own chunk (a single `import` line doesn't), just the units a
# developer would actually think of as "a piece of the code."
#
# Note on Python specifically: there's no separate "method" node type in
# tree-sitter-python — a method is just a function_definition whose parent
# happens to be a class body. We don't special-case that; it just shows up
# as type "function" regardless of nesting, which is fine for retrieval.
CHUNKABLE_NODES: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
    },
    "rust": {
        "function_item": "function",
        "impl_item": "impl",
        "struct_item": "struct",
        "trait_item": "trait",
        "enum_item": "enum",
    },
    "javascript": {
        "function_declaration": "function",
        "method_definition": "method",
        "class_declaration": "class",
    },
    "typescript": {
        "function_declaration": "function",
        "method_definition": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
    },
    "java": {
        "method_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
    },
    "c": {
        "function_definition": "function",
        "struct_specifier": "struct",
    },
    "cpp": {
        "function_definition": "function",
        "struct_specifier": "struct",
        "class_specifier": "class",
    },
}

# Below this many lines, splitting a file into pieces isn't worth doing —
# treat the whole file as one chunk. Avoids pointless fragmentation of
# small config files, __init__.py stubs, short scripts, etc.
MIN_LINES_TO_CHUNK = 15


def _node_name(node, source: bytes) -> str:
    """Pull a human-readable name out of a node via its `name` field, if
    the grammar exposes one. Falls back to 'anonymous' — some constructs
    (e.g. a JS function expression assigned to a variable) don't carry a
    clean name field in every grammar."""
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
    return "anonymous"


def _whole_file_chunk(content: str, file_path: str, language: str, repo: str) -> dict:
    line_count = content.count("\n") + 1
    return {
        "content": content,
        "type": "module",
        "name": Path(file_path).name,
        "file": file_path,
        "language": language,
        "lines": f"1-{line_count}",
        "repo": repo,
    }


def chunk_file(file_path: str, content: str, language: str, repo: str) -> list[dict]:
    """
    Chunk a single file's content into semantically meaningful pieces.

    file_path: path as reported by the Rust walker (relative to repo root)
    content:   full file text, already read and decoded by the caller
    language:  language label from the walker's extension mapping
    repo:      repo root path, carried through chunk metadata for future
               multi-repo indexing/cross-repo querying (V3)

    Returns a list of chunk dicts matching the Section 8 contract:
    {content, type, name, file, language, lines, repo}. Every file
    produces at least one chunk — unsupported languages, tiny files, and
    files with no matched constructs all fall back to a single
    whole-file chunk rather than being silently dropped from the index.
    """
    grammar_name = LANGUAGE_MAP.get(language)

    line_count = content.count("\n") + 1
    if grammar_name is None or line_count < MIN_LINES_TO_CHUNK:
        return [_whole_file_chunk(content, file_path, language, repo)]

    try:
        parser = get_parser(grammar_name)
    except LookupError:
        # tree-sitter-language-pack doesn't ship this grammar — fail soft
        # to the whole-file fallback rather than crashing the whole
        # indexing run over one file.
        return [_whole_file_chunk(content, file_path, language, repo)]

    source_bytes = content.encode("utf-8")
    tree = parser.parse(source_bytes)
    node_types = CHUNKABLE_NODES.get(grammar_name, {})

    chunks: list[dict] = []

    def walk(node) -> None:
        chunk_type = node_types.get(node.type)
        if chunk_type is not None:
            chunk_content = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
            chunks.append({
                "content": chunk_content,
                "type": chunk_type,
                "name": _node_name(node, source_bytes),
                "file": file_path,
                "language": language,
                "lines": f"{node.start_point[0] + 1}-{node.end_point[0] + 1}",
                "repo": repo,
            })
        # Always recurse, even into a matched node — a class chunk's
        # content includes its methods' source, and each method also
        # becomes its own chunk. This deliberate overlap is a standard
        # RAG chunking tradeoff: a query about one specific method
        # retrieves the small, precise method chunk; a query about what
        # the class does as a whole retrieves the class-level chunk. The
        # duplication costs some embedding storage, not retrieval
        # quality.
        for child in node.children:
            walk(child)

    walk(tree.root_node)

    if not chunks:
        # Grammar parsed fine but nothing matched CHUNKABLE_NODES — e.g. a
        # Python file that's all top-level script code, no functions or
        # classes. Don't silently drop the file's content from the index.
        return [_whole_file_chunk(content, file_path, language, repo)]

    return chunks


# KNOWN LIMITATION (deferred to indexer/embedder.py, not solved here):
# a single chunk (e.g. a very large function) can exceed bge-small's ~512
# token context window. Whether to split oversized chunks further, or
# truncate, or embed only a summary, is an embedding-layer concern —
# chunker.py's job is correct AST-based boundaries, not enforcing a size
# ceiling that's specific to one embedding model's limits.


if __name__ == "__main__":
    # Smoke test: python -m indexer.chunker <file_path> <language>
    # Prints chunk boundaries/names/sizes so you can eyeball correctness
    # against a real file before wiring this into the embedding pipeline.
    if len(sys.argv) < 3:
        print("usage: python -m indexer.chunker <file_path> <language>")
        sys.exit(1)

    test_path = sys.argv[1]
    test_language = sys.argv[2]
    with open(test_path, "r", encoding="utf-8") as f:
        test_content = f.read()

    result = chunk_file(test_path, test_content, test_language, repo=".")
    print(f"{len(result)} chunk(s):\n")
    for c in result:
        print(f"  [{c['type']:10s}] {c['name']:30s} lines {c['lines']:>10s}  ({len(c['content'])} chars)")
