# indexer/imports.py — import-statement extraction + repo-relative
# resolution, for the import graph ("why is this file important?", V3,
# OLORIN_PROJECT.md Section 11).
#
# Two-stage pipeline, mirroring chunker.py's own shape (parse -> walk AST
# -> pull out the pieces that matter) but for import statements instead
# of function/class boundaries:
#
#   1. extract_raw_imports() — parse a file, return its import specifiers
#      EXACTLY as written ("core.llm_client", "./foo", "crate::config",
#      "std::io::Read") — no resolution yet, just "what did this file
#      say it imports".
#   2. resolve_imports() — turn those raw specifiers into repo-relative
#      file paths, keeping only the ones that resolve to a real file in
#      THIS repo and silently dropping everything else (external
#      packages, stdlib, unresolvable specifiers). Dropping most specs
#      is the expected, common case, not a failure — most imports in any
#      real file are external dependencies, not other files in the repo.
#
# Verified against tree-sitter-language-pack directly (node types/field
# names below are not assumed from memory — dumped and confirmed via a
# real parse before writing the extractors that depend on them, same
# "live-verify before trusting" discipline as everywhere else in this
# project).
#
# SCOPE (V1, deliberately conservative — same posture as chunker.py's
# CHUNKABLE_NODES comment): Python, Rust, JavaScript, TypeScript, C,
# C++. Go and Java are NOT included, not as an oversight — Go's import
# paths are rooted at a module name declared in go.mod, and Java's at a
# source root (src/main/java/, etc.) that this project doesn't parse;
# resolving either correctly needs information this project doesn't
# have access to yet. Rather than wire up extraction with no working
# resolution (dead code that always drops everything), both are left out
# entirely until that's worth doing — a real follow-up, not silently
# forgotten scope.
#
# KNOWN LIMITATIONS (V1, accepted — revisit only if a real repo's
# results are measurably wrong because of one of these):
#   - Rust: grouped imports ("use std::{io, fs};") aren't parsed — only
#     the single scoped_identifier shape is handled. A grouped use
#     statement's specs are silently dropped, same as an unresolvable
#     external import.
#   - Rust: "crate::" is resolved as rooted at the repo root, correct
#     for a single-crate layout (this project's own indexer_core/) but
#     not for a multi-crate Cargo workspace with nested src/ directories.
#   - Python: star imports ("from x import *") resolve x itself
#     correctly (the module_name field doesn't care what's being
#     imported), so this isn't actually a gap.
#   - JS/TS: only relative ("./", "../") specifiers are ever resolved —
#     bare specifiers are always treated as npm packages and dropped,
#     even in the rare case of a configured path alias (e.g. "@/utils")
#     resolving to a real repo file via tsconfig paths. Not handled —
#     would need to parse tsconfig.json, out of scope for V1.

from __future__ import annotations

import os

from tree_sitter_language_pack import get_parser

from indexer.chunker import LANGUAGE_MAP


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


# --- Extraction: raw specifier strings, per language -----------------------

def _extract_python(node, source: bytes) -> list[str]:
    """
    import_statement ("import os", "import a.b.c as x") and
    import_from_statement ("from a.b import c") both expose the module
    path as text, but differently:
      - import_statement: one or more dotted_name children (plain form),
        or an aliased_import wrapping a dotted_name (the "as x" form).
      - import_from_statement: a module_name field — confirmed via a
        real parse to correctly return a dotted_name for the plain form
        ("core.llm_client") and a relative_import node (dots included in
        its own text, e.g. ".", ".sibling", "..pkg.sub") for the
        relative form. Both cases are handled identically here since
        resolve_imports() is what interprets the leading dots.
    """
    specs = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "dotted_name":
                specs.append(_text(child, source))
            elif child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    specs.append(_text(name_node, source))
    elif node.type == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        if module_node is None:
            return specs
        module_text = _text(module_node, source)
        specs.append(module_text)

        # Also capture each imported NAME as a combined "module.name"
        # candidate — real gap found live (2026-07-18): "from . import
        # registry" was only ever contributing "." (the bare module_name
        # field text) with no way to know "registry" was the actual
        # thing being asked for, so a real submodule import like this
        # never resolved to the file it should (e.g. "core/registry.py").
        # Confirmed via a real parse that every imported name — first
        # and subsequent, comma-separated — is a dotted_name (or
        # aliased_import) child positioned after the module_name node,
        # but tree-sitter-python only tags the FIRST one with a "name"
        # field; relying on child_by_field_name("name") alone silently
        # drops every name past the first in a multi-import statement
        # ("from . import registry, reader"). Walking node.children
        # instead needed a byte-offset comparison, not `is` — confirmed
        # live that tree-sitter node objects don't preserve Python
        # object identity across separate attribute accesses, so
        # `child is module_node` silently never matched even for the
        # identical underlying node, and the whole combined-name branch
        # never fired despite looking correct. This combined form is
        # additive, not a replacement: resolve_imports() still also gets
        # the bare module_text candidate, and for a plain symbol import
        # ("from a.b import c" where c isn't a file) the combined
        # candidate just won't match anything in repo_files and drops
        # silently, same as any other unresolvable specifier.
        past_module = False
        for child in node.children:
            if child.start_byte == module_node.start_byte and child.end_byte == module_node.end_byte:
                past_module = True
                continue
            if not past_module:
                continue
            imported_name = None
            if child.type == "dotted_name":
                imported_name = _text(child, source)
            elif child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    imported_name = _text(name_node, source)
            if imported_name is not None:
                separator = "" if module_text.endswith(".") else "."
                specs.append(f"{module_text}{separator}{imported_name}")
    return specs


def _extract_rust(node, source: bytes) -> list[str]:
    """
    use_declaration's child is a scoped_identifier carrying the full
    path text ("crate::config", "std::io::Read", "super::foo",
    "self::bar") — confirmed via a real parse. Grouped imports
    ("use std::{io, fs};") produce a use_list child instead of a
    scoped_identifier and aren't handled (see module docstring's Known
    Limitations) — silently produces no specs for that statement,
    same as any other unresolvable case.
    """
    for child in node.children:
        if child.type == "scoped_identifier":
            return [_text(child, source)]
    return []


def _extract_js_ts(node, source: bytes) -> list[str]:
    """
    import_statement's source field is a string node wrapping the
    module specifier (confirmed via a real parse) — strip the
    surrounding quote characters so resolve_imports() gets a bare path.
    """
    source_node = node.child_by_field_name("source")
    if source_node is None:
        return []
    raw = _text(source_node, source)
    return [raw[1:-1]] if len(raw) >= 2 else []


def _extract_c_cpp(node, source: bytes) -> list[str]:
    """
    preproc_include has exactly one of two child shapes (confirmed via a
    real parse): string_literal for the quoted form ('#include "auth.h"')
    or system_lib_string for the angle-bracket form ('#include <stdio.h>').
    Only the quoted form is ever project-relative by C/C++ convention —
    the angle-bracket form is dropped right here at extraction time,
    before resolution even runs, since there's no repo file a <...>
    include could ever resolve to.
    """
    for child in node.children:
        if child.type == "string_literal":
            text = _text(child, source)
            return [text[1:-1]] if len(text) >= 2 else []
    return []


# grammar name (tree-sitter-language-pack's key space, same as
# chunker.py's LANGUAGE_MAP values) -> {node type: extractor function}
_IMPORT_NODE_TYPES: dict[str, dict[str, callable]] = {
    "python": {
        "import_statement": _extract_python,
        "import_from_statement": _extract_python,
    },
    "rust": {"use_declaration": _extract_rust},
    "javascript": {"import_statement": _extract_js_ts},
    "typescript": {"import_statement": _extract_js_ts},
    "c": {"preproc_include": _extract_c_cpp},
    "cpp": {"preproc_include": _extract_c_cpp},
}


def extract_raw_imports(content: str, language: str) -> list[str]:
    """
    Parses one file's content and returns its raw, UNRESOLVED import
    specifiers — exactly as written in the source. Mirrors chunker.py's
    grammar-lookup-with-soft-fallback pattern exactly: an unsupported
    language (including Go/Java — see module docstring) or a missing
    grammar returns an empty list rather than raising, since a file this
    can't extract imports from should never break the rest of indexing.
    """
    grammar_name = LANGUAGE_MAP.get(language)
    if grammar_name is None or grammar_name not in _IMPORT_NODE_TYPES:
        return []

    try:
        parser = get_parser(grammar_name)
    except LookupError:
        return []

    source_bytes = content.encode("utf-8")
    tree = parser.parse(source_bytes)
    node_extractors = _IMPORT_NODE_TYPES[grammar_name]

    specs: list[str] = []

    def walk(node) -> None:
        extractor = node_extractors.get(node.type)
        if extractor is not None:
            specs.extend(extractor(node, source_bytes))
            return  # import statements don't nest further imports
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return specs


# --- Resolution: raw specifier -> repo-relative file path -----------------

def _candidate_paths_python(spec: str, importer_path: str) -> list[str]:
    leading_dots = len(spec) - len(spec.lstrip("."))
    remainder = spec[leading_dots:]
    parts = remainder.split(".") if remainder else []

    if leading_dots > 0:
        # One leading dot means "this package" (the importer's own
        # directory); each additional dot climbs one directory further,
        # matching Python's actual relative-import semantics.
        base_dir = os.path.dirname(importer_path)
        for _ in range(leading_dots - 1):
            base_dir = os.path.dirname(base_dir)
        rel = "/".join(parts)
        base = f"{base_dir}/{rel}" if rel else base_dir
    else:
        base = "/".join(parts)

    base = base.strip("/")
    if not base:
        return []
    return [f"{base}.py", f"{base}/__init__.py"]


def _candidate_paths_rust(spec: str, importer_path: str) -> list[str]:
    base_dir = os.path.dirname(importer_path)
    if spec.startswith("crate::"):
        # Simplification: rooted at the repo root. Correct for a
        # single-crate layout (this project's own indexer_core/), not a
        # multi-crate workspace — see module docstring's Known
        # Limitations.
        base = spec.removeprefix("crate::").replace("::", "/")
    elif spec.startswith("super::"):
        base = f"{os.path.dirname(base_dir)}/{spec.removeprefix('super::').replace('::', '/')}".strip("/")
    elif spec.startswith("self::"):
        base = f"{base_dir}/{spec.removeprefix('self::').replace('::', '/')}".strip("/")
    else:
        return []  # external crate (std::, serde::, ...) — not in this repo

    if not base:
        return []
    return [f"{base}.rs", f"{base}/mod.rs"]


def _candidate_paths_js_ts(spec: str, importer_path: str) -> list[str]:
    if not spec.startswith("."):
        return []  # bare specifier — npm package, not a repo file (see
                    # module docstring's Known Limitations re: path aliases)

    base_dir = os.path.dirname(importer_path)
    resolved = os.path.normpath(os.path.join(base_dir, spec)).replace(os.sep, "/")

    candidates = []
    if os.path.splitext(resolved)[1]:
        candidates.append(resolved)  # already has an extension
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        candidates.append(f"{resolved}{ext}")
        candidates.append(f"{resolved}/index{ext}")
    return candidates


def _candidate_paths_c_cpp(spec: str, importer_path: str) -> list[str]:
    # Quoted includes are already project-relative by convention (angle-
    # bracket includes never reach here — dropped at extraction time).
    base_dir = os.path.dirname(importer_path)
    resolved = os.path.normpath(os.path.join(base_dir, spec)).replace(os.sep, "/")
    return [resolved]


_CANDIDATE_RESOLVERS: dict[str, callable] = {
    "python": _candidate_paths_python,
    "rust": _candidate_paths_rust,
    "javascript": _candidate_paths_js_ts,
    "typescript": _candidate_paths_js_ts,
    "c": _candidate_paths_c_cpp,
    "cpp": _candidate_paths_c_cpp,
}


def resolve_imports(
    importer_path: str,
    raw_specs: list[str],
    language: str,
    repo_files: set[str],
) -> list[str]:
    """
    Turns raw, language-specific import specifiers into resolved,
    repo-relative file paths — keeping only the ones that match a real
    file in THIS repo and silently dropping everything else (external
    packages, stdlib, unresolvable specifiers). Dropping is the expected,
    common case: most of any file's imports are external dependencies,
    not other files in the repo.

    Args:
        importer_path: repo-relative path of the file whose imports
                       these are — needed for every relative-import form
                       (Python's leading dots, Rust's self::/super::,
                       JS's './').
        raw_specs: output of extract_raw_imports() for this file.
        language: walker-reported language label (LANGUAGE_MAP's key
                  space, same as chunker.py).
        repo_files: every repo-relative file path this repo's walker
                    reported. Resolution never touches the filesystem
                    directly — the walker's own output is already the
                    single source of truth for "what files exist" that
                    the rest of the indexing pipeline relies on, so this
                    stays consistent with that rather than doing a
                    second, potentially-inconsistent os.path.isfile()
                    check of its own.

    Returns:
        Sorted, deduplicated list of repo-relative paths confirmed to
        exist in repo_files.
    """
    grammar_name = LANGUAGE_MAP.get(language)
    resolver = _CANDIDATE_RESOLVERS.get(grammar_name)
    if resolver is None:
        return []

    resolved: set[str] = set()
    for spec in raw_specs:
        for candidate in resolver(spec, importer_path):
            normalized = candidate.strip("/")
            if normalized in repo_files:
                resolved.add(normalized)

    return sorted(resolved)


if __name__ == "__main__":
    # Manual smoke test: python -m indexer.imports
    # Deliberately uses a hand-built repo_files set rather than a real
    # walk, so this test is a pure, fast unit check of extraction +
    # resolution logic in isolation, independent of the Rust walker or
    # any real repo on disk.
    #
    # Two importer positions are tested against the SAME source text on
    # purpose (2026-07-18, after a live run against this real repo
    # caught the original version of this test asserting the wrong
    # thing): "from . import registry" / "from .reader import
    # read_file" are relative imports, so where they resolve TO
    # genuinely depends on where the importing file itself lives.
    # tools/agent.py's relative imports correctly land in tools/;
    # core/agent.py's identical-looking relative imports correctly do
    # NOT reach into tools/ at all — asserting a fixed resolved-path
    # list independent of importer_path was the actual bug, not the
    # resolution logic itself (see ENGINEERING_JOURNAL.md's 2026-07-18
    # entry for the full story, including the separate byte-offset-vs-
    # `is` bug this same live run caught in the combined-name walk).
    python_src = (
        "import os\n"
        "import config\n"
        "from core.llm_client import LLMClient\n"
        "from . import registry\n"
        "from .reader import read_file\n"
    )
    repo_files = {
        "config.py", "core/llm_client.py", "core/agent.py",
        "tools/registry.py", "tools/reader.py",
    }
    raw = extract_raw_imports(python_src, "python")
    print(f"Python raw specs: {raw}")

    resolved_same_pkg = resolve_imports("tools/agent.py", raw, "python", repo_files)
    print(f"Python resolved (importer=tools/agent.py, same package as the relative imports): {resolved_same_pkg}")
    assert resolved_same_pkg == ["config.py", "core/llm_client.py", "tools/reader.py", "tools/registry.py"], resolved_same_pkg

    resolved_diff_pkg = resolve_imports("core/agent.py", raw, "python", repo_files)
    print(f"Python resolved (importer=core/agent.py, relative imports correctly stay in core/ and find nothing there): {resolved_diff_pkg}")
    assert resolved_diff_pkg == ["config.py", "core/llm_client.py"], resolved_diff_pkg

    rust_src = (
        "use crate::config;\n"
        "use std::io::Read;\n"
        "use super::embedder;\n"
    )
    repo_files_rs = {"config.rs", "indexer/embedder.rs", "indexer/chunker.rs"}
    raw_rs = extract_raw_imports(rust_src, "rust")
    print(f"\nRust raw specs: {raw_rs}")
    resolved_rs = resolve_imports("indexer/chunker.rs", raw_rs, "rust", repo_files_rs)
    print(f"Rust resolved (importer=indexer/chunker.rs): {resolved_rs}")
    assert resolved_rs == ["config.rs", "indexer/embedder.rs"], resolved_rs

    ts_src = "import { foo } from './bar';\nimport x from 'react';\n"
    repo_files_ts = {"src/bar.ts", "src/main.ts"}
    raw_ts = extract_raw_imports(ts_src, "typescript")
    print(f"\nTS raw specs: {raw_ts}")
    resolved_ts = resolve_imports("src/main.ts", raw_ts, "typescript", repo_files_ts)
    print(f"TS resolved (importer=src/main.ts): {resolved_ts}")
    assert resolved_ts == ["src/bar.ts"], resolved_ts

    print("\nAll assertions passed.")
