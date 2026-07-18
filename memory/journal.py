"""
memory/journal.py — Engineering Journal automation (OLORIN_PROJECT.md
Section 10; the last open V2 checklist item as of 2026-07-12).

Auto-generates journal/YYYY-MM-DD.md-style session summaries from
episodic memory (memory/history.py's `conversations` table) instead of
requiring a human to hand-write them — which is exactly how this
project's own ENGINEERING_JOURNAL.md has been maintained all along.
Nothing new is logged here; this module only reads what Agent.run()
already writes on every completed run (Section 10) and reshapes it.

STORAGE LOCATION mirrors indexer/store.py's ChromaDB pattern exactly,
for the identical reason: writing generated output into the INDEXED
repo's own working tree would mean journaling someone else's project
leaves files in their tree they never asked for. Journals live at
~/.olorin/journal/<repo_hash>/YYYY-MM-DD.md, one subfolder per repo,
keyed by the same sha256-of-resolved-path scheme store.py uses for
Chroma collection names — globally, outside every indexed repo.

TWO-PART SPLIT, same shape as memory/capsules.py + indexer/capsules.py:
  - The "Queries" / "Tools used" / "Backend" sections are pure,
    deterministic aggregation over what's already in episodic memory —
    no LLM involved, so they always run and never fail.
  - The "Worked on" section is a short LLM synthesis over the day's
    queries (see synthesize_worked_on()'s docstring for which backend
    and why) — optional, duck-typed llm_client, gracefully degrades to
    a literal deduplicated query list if omitted or if generation fails.
    Kept in this one file rather than split into an indexer/journal.py
    generator module the way Capsules are: Capsules generate from
    *source code* (an indexer/ concern), Journals generate from
    *episodic memory* (already a memory/ concern) — there's no
    indexer-vs-memory boundary to preserve here the way there was there.

DELIBERATELY NOT auto-triggered on every `ask`, unlike indexing
(Section 8's index-on-demand). Auto-indexing was justified because the
hash-skip logic makes a repeat check nearly free when nothing changed.
Regenerating a day's journal calls an LLM for the "Worked on" synthesis
every time it runs — real, non-trivial latency — so doing that on every
single query would tax the interactive path for a document nobody's
reading mid-session. Journal generation is instead a deliberate,
explicit, end-of-session action: `python cli.py journal`.
"""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from memory import history

JOURNAL_ROOT = Path.home() / ".olorin" / "journal"


def _repo_journal_dir(repo_path: str) -> Path:
    """
    Same hash-of-resolved-path scheme as indexer/store.py's
    _collection_name() (Section 8), reimplemented locally rather than
    imported. memory/ has had no dependency on indexer/ up to this
    point — indexer/capsules.py already made the same call in reverse
    (accepting a duck-typed llm_client instead of importing core/) to
    avoid introducing a cross-package coupling for one small piece of
    logic. This is a three-line function, not worth a cross-package
    import for.
    """
    resolved = str(Path(repo_path).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    path = JOURNAL_ROOT / f"repo_{digest}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _format_tool_counts(conversations: list[dict]) -> str:
    """
    "search_codebase (4x), read_file (2x)" — matching Section 10's spec
    example exactly. Counts every tool call across every conversation
    logged that day, in first-seen order (dict insertion order, Python
    3.7+) so the output reflects the session's natural flow instead of
    being alphabetically shuffled.
    """
    counts: dict[str, int] = {}
    for conv in conversations:
        for tool in conv["tools_used"]:
            counts[tool] = counts.get(tool, 0) + 1
    if not counts:
        return "(no tool calls)"
    return ", ".join(f"{name} ({n}x)" for name, n in counts.items())


def _format_backend_counts(conversations: list[dict]) -> str:
    """
    "Groq (2 queries), Boromir (6 queries)" — per Section 10's spec
    example. backend_used is stored lowercase ("groq"/"boromir"/
    "faramir" — see core/agent.py's _log_conversation()); capitalized
    here purely for display, to match how the lore names are normally
    written.
    """
    counts: dict[str, int] = {}
    for conv in conversations:
        backend = conv["backend_used"] or "unknown"
        counts[backend] = counts.get(backend, 0) + 1
    if not counts:
        return "(no queries)"
    return ", ".join(
        f"{name.capitalize()} ({n} quer{'y' if n == 1 else 'ies'})"
        for name, n in counts.items()
    )


def _format_queries_section(conversations: list[dict]) -> str:
    """
    One line per query: `"query" -> first ~100 chars of the answer`,
    matching the spec's arrow-summary style ("what does the auth module
    do?" -> found auth.py middleware chain). The full answer text
    already lives in episodic memory (`python cli.py history` or the
    `search_history` tool) for anyone who wants the whole thing — this
    section is a session skim, not a transcript dump.
    """
    if not conversations:
        return "(no queries logged)"
    lines = []
    for conv in conversations:
        answer = conv["agent_response"].strip().replace("\n", " ")
        summary = answer[:100] + ("..." if len(answer) > 100 else "")
        lines.append(f'- "{conv["user_query"]}" -> {summary}')
    return "\n".join(lines)


def synthesize_worked_on(llm_client, conversations: list[dict]) -> list[str]:
    """
    Short LLM synthesis of 2-4 "worked on" bullets from a day's queries —
    the one part of a journal entry that genuinely benefits from real
    synthesis rather than raw aggregation. Section 10's own spec example
    ("- Olorin agent loop") is a theme, not a literal question someone
    typed, so this can't be produced by reformatting stored rows alone.

    Faramir generates this, not Boromir, for the identical reason
    Knowledge Capsules use Faramir (indexer/capsules.py, Section 10):
    this is general prose synthesis, not tool orchestration — Faramir's
    role per Section 2's lore — and the known Faramir <think>-block
    tool-calling risk (Section 9) doesn't apply, since this call makes
    no tool calls either.

    llm_client is duck-typed (anything exposing
    .chat(messages, force_provider=...)), matching
    indexer/capsules.py's generate_module_summary() exactly — keeps
    memory/ from needing a hard dependency on core/ just for this one
    optional feature. Passing None (or a client whose call fails) is a
    supported, expected path, not an error case: write_journal() falls
    back to a literal query list when this returns empty, so a Groq/
    Ollama hiccup degrades the journal instead of blocking it.

    Returns a plain list of bullet strings (leading "- " already
    stripped) — never raises.

    PROMPT DESIGN NOTE (bug #13, found 2026-07-12 during first live
    verification): the initial prompt asked only for "themes and tasks"
    with no constraint on what counts as a theme, and on a sparse real
    session (two queries, both *asking the assistant to explain/
    summarize existing code and docs*) Faramir confidently synthesized
    bullets describing implementation work that never happened —
    "Implement daemon deferral mechanism", "Handle background task
    execution delays" — extrapolated from the phrase "daemon deferral"
    in a question that was actually just asking for a documentation
    summary. Same failure mode Section 9 already documents for bare
    parametric recall (confident, fluent, wrong) showing up here in a
    grounded-but-underspecified synthesis task instead. Fixed at the
    prompt layer: explicit instruction to describe only what a question
    literally shows (explain/read/summarize -> "reviewed"/
    "investigated", never "implemented"/"fixed" unless the question is
    actually about a change), rather than adding a workaround like
    suppressing synthesis below some conversation-count threshold — the
    same root-cause-over-workaround preference applied everywhere else
    in this project.
    """
    if llm_client is None or not conversations:
        return []

    queries = "\n".join(f"- {c['user_query']}" for c in conversations)
    prompt = (
        "Here are the questions a developer asked their coding assistant "
        f"during one session:\n\n{queries}\n\n"
        "Summarize what they were actually working on as 2-4 short "
        "bullet points (a few words each, like git commit subject "
        "lines) — themes and tasks, not a restatement of each question.\n\n"
        "CRITICAL: describe only what the questions themselves show — do "
        "not infer that anything was built, implemented, fixed, or "
        "changed unless a question says so explicitly. A question that "
        "asks the assistant to explain, read, or summarize existing "
        "code or documentation means the developer was investigating or "
        "reviewing that area, not building it — phrase those bullets as "
        "'reviewed X' / 'investigated Y', never as 'implemented Z' or "
        "'fixed W' unless the question itself is about a fix or change. "
        "When in doubt, stay closer to the literal question than to a "
        "guessed narrative.\n\n"
        "Respond with ONLY the bullet points, one per line, each "
        "starting with '- '. No preamble, no headers."
    )
    try:
        response = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            force_provider="faramir",
        )
    except Exception:
        return []

    bullets = []
    for line in response.content.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        bullets.append(line[2:].strip() if line.startswith("- ") else line)
    return bullets


def generate_journal_markdown(
    date: str,
    conversations: list[dict],
    llm_client=None,
) -> str:
    """
    Pure formatting: takes already-fetched conversations (so callers —
    and tests — control what data goes in) and produces the full
    Markdown document in Section 10's exact spec shape.
    """
    worked_on = synthesize_worked_on(llm_client, conversations)
    if not worked_on:
        # Honest fallback: list the literal, deduplicated queries instead
        # of a synthesized theme, rather than printing a misleading or
        # empty section.
        seen = set()
        worked_on = []
        for c in conversations:
            q = c["user_query"]
            if q not in seen:
                seen.add(q)
                worked_on.append(q)

    worked_on_section = "\n".join(f"- {b}" for b in worked_on)

    return (
        f"# {date}\n\n"
        f"## Worked on\n{worked_on_section}\n\n"
        f"## Queries\n{_format_queries_section(conversations)}\n\n"
        f"## Tools used\n- {_format_tool_counts(conversations)}\n\n"
        f"## Backend\n- {_format_backend_counts(conversations)}\n"
    )


def write_journal(
    repo_path: str,
    date: Optional[str] = None,
    llm_client=None,
) -> Optional[Path]:
    """
    Fetches the day's conversations for repo_path from episodic memory,
    generates the Markdown, and writes it to
    ~/.olorin/journal/<repo_hash>/<date>.md — overwriting any existing
    file for that date. Regeneration is idempotent and cheap outside the
    optional LLM call, so "just regenerate the whole file" is simpler
    and safer than trying to append/merge entries.

    Returns the written Path, or None if there's nothing logged for that
    repo on that date (no file is written — an empty journal is noise,
    not signal, and shouldn't silently overwrite a real prior entry for
    the same date if called with a bad date by mistake).
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    conversations = history.get_conversations_for_date(repo_path, date)
    if not conversations:
        return None

    markdown = generate_journal_markdown(date, conversations, llm_client)
    out_path = _repo_journal_dir(repo_path) / f"{date}.md"
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    # Manual smoke test: python -m memory.journal
    # Logs a couple of fake conversations under today's date for a
    # throwaway repo path, then generates + writes the journal WITHOUT an
    # LLM client (worked_on falls back to the literal-query-list path),
    # so this smoke test has no external dependency on Groq/Ollama being
    # up. Live LLM synthesis is exercised via `python cli.py journal`
    # against the real olorin repo instead.
    test_repo = "/fake/repo/for/journal/smoke/test"
    today = datetime.now().strftime("%Y-%m-%d")

    history.log_conversation(
        repo_path=test_repo,
        user_query="how does the agent loop handle tool calls?",
        agent_response="It checks finish_reason, executes tools via the registry, and loops.",
        tools_used=["search_codebase", "read_file"],
        backend_used="boromir",
        complexity_score=12,
    )
    history.log_conversation(
        repo_path=test_repo,
        user_query="what does config.py do?",
        agent_response="Centralizes env vars, model names, and tunable thresholds.",
        tools_used=["read_file"],
        backend_used="boromir",
        complexity_score=3,
    )

    written = write_journal(test_repo, date=today)
    print(f"Journal written to: {written}")
    print("\n--- contents ---\n")
    print(written.read_text(encoding="utf-8"))
