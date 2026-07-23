"""
memory/entities.py — Entity memory (SQLite), designed 2026-07-15 (see
ENGINEERING_JOURNAL.md's 2026-07-15 (continued) entry for the full
design conversation this implements).

SCOPE — resolved directly with Aryavart before any code was written, in
response to a genuine open question ("object relations in a repo, or
the agent's relation to things?"): this tracks DECISIONS and CONCEPTS
discussed across sessions — an agent's persistent, updateable knowledge
of named things ("what is X, what's its current status?") — NOT source
code structure. Code-level relationships (imports, call graphs, "why is
this file important") are a separate, already-scoped V3 item
(OLORIN_PROJECT.md Section 11) — building that here would just be that
item wearing a different name.

The purpose test that settled the scope (what question does this answer
that nothing else in the system does?):
  - Cache            -> "have I solved this exact query before?"
  - Capsules         -> "what does this module do?"
  - search_history   -> "which past conversation mentioned X?"
  - Entity memory    -> "what is X, what's its current status?" (a
                         resolved fact, not a conversation to re-read)

SCHEMA DEVIATION from the original stub (OLORIN_PROJECT.md Section 10):
the original stub had no repo_path column, and a type set of "repo" |
"file" | "person" | "concept". Both changed here: repo_path added for
consistency with every other memory table (capsules, conversations) —
a decision about Vorticity's architecture and a decision about Olorin's
are different things, and mixing them in one global list would be
confusing the moment this tool gets used against more than one repo.
Type scope narrowed to "decision" | "concept" only — "repo"/"file"
overlap with Capsules/OLORIN.md and aren't populated by anything in
this design; "person" was considered and deliberately left out (solo
project, no clear population source yet — can be added later without a
migration, since type is just a TEXT column, not an enum).

TWO-PART SPLIT, same shape as memory/capsules.py + indexer/capsules.py
and memory/journal.py: this module owns storage, the dedup match, the
commit gate, AND extraction (the LLM call) — unlike Capsules (generation
lives in indexer/capsules.py) or Journal (kept in one file already),
extraction here is small enough and specific enough to entities that
splitting it into a separate module would be over-structuring for what
it does. llm_client is duck-typed (anything exposing
.chat(messages, force_provider=...)), matching journal.py's
synthesize_worked_on() and indexer/capsules.py's generate_module_summary()
exactly — no hard dependency on core/ for this optional feature.

POPULATION MECHANISM (the design conversation's central question):
extraction runs after every completed Agent.run() that ends in a clean
finish_reason=="stop" answer (see core/agent.py's Agent._extract_entities()
for the call site), using the SAME backend that just answered — no
separate model choice, no new complexity-based gate. This reuses the
routing decision the complexity scorer (Section 7) already made instead
of inventing a second one, and avoids any Ollama model-swap cost since
that backend is already warm.

Faramir's exclusion REMOVED 2026-07-21 (was: _UNRELIABLE_STRUCTURED_
OUTPUT_BACKENDS = {"faramir"}, added 2026-07-15). Worth being precise
about why the original exclusion didn't actually hold up under
inspection, not just "the underlying model changed": extract_candidates()
below has never called llm_client.chat() with a tools= argument at all —
this is, and always was, a plain-text completion asking the model to
write a JSON array as its response, parsed by _parse_json_array() below.
test_faramir_tools.py (which motivated the original exclusion) tested a
genuinely different capability: whether DeepSeek-R1 could trigger the
API's structured tool_calls mechanism. "Can't reliably use tool_calls"
and "can't reliably write JSON as plain text" are not the same claim,
and the exclusion here was conflating them even before the 2026-07-20
Boromir/Faramir model collapse made the point moot anyway (both
personas now share qwen3:8b, which reliably does BOTH). One residual
question checked before removing this, not assumed: Ollama's native API
already separates a thinking-mode response's reasoning trace
(message.thinking) from its actual answer (message.content) — see
providers/ollama_provider.py's chat() — so Faramir's think=True content
field shouldn't have stray <think> text bleeding into what
_parse_json_array() tries to parse. Not yet re-verified against a real
live Faramir-backed extraction call as of this change; worth confirming
with a real `cli.py ask "Faramir, ..."` on a decision-shaped query and
checking `cli.py entities` afterward.

Most exchanges produce zero candidates — that's the expected, normal
result, not a failure. The extraction prompt says so explicitly.

COMMIT GATE: only candidates the model itself rates "medium" or "high"
confidence get written. Below that, discarded — no staging table for
"maybe" candidates, matching this project's YAGNI discipline elsewhere
(see memory/capsules.py's module docstring for the same instinct applied
to a different feature).

DEDUP: keyword-overlap match against existing entities' name+description
(same mechanism memory/history.py's search_history() already uses),
deliberately conservative — a HIGH match-ratio threshold, because a
false-positive merge (silently overwriting one real thing's description
with a different real thing's) is a worse failure than a false-negative
(two near-duplicate rows, which is just noise a person can still find
via search_entities). Favoring "miss and duplicate" over "wrongly merge"
mirrors this project's established preference for the safer failure mode
elsewhere (see OLORIN_PROJECT.md Section 13's bug #13 entry: an honest
"I don't know" beats a confident wrong answer).
"""

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Optional

import config

# Plain stdlib logging, not core.logging_config's get_logger() — memory/
# has deliberately had no dependency on core/ up to this point (see
# memory/journal.py's module docstring: llm_client is duck-typed for the
# identical reason). Importing core.logging_config here just for a
# logger would introduce exactly the coupling that design has avoided
# everywhere else; this achieves the same "olorin.entities"-prefixed
# logger without it.
logger = logging.getLogger("olorin.entities")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    repo_path TEXT,
    type TEXT,             -- "decision" | "concept"
    name TEXT,
    description TEXT,      -- latest known state, overwritten on update
    metadata TEXT,         -- JSON, reserved for future use
    last_updated TEXT
);
"""

# Words shorter than this are too common to be useful signal — same
# threshold memory/history.py's search_history() uses, for the same
# reason (filters out "a", "is", "the", ...).
_MIN_KEYWORD_LEN = 3

_VALID_TYPES = {"decision", "concept"}
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# Candidates below this confidence are discarded at commit time, not
# staged — see module docstring's COMMIT GATE section.
_COMMIT_THRESHOLD = "medium"

# Dedup match ratios required before two candidates are treated as the
# same entity. Two separate signals, checked independently (see
# _find_matching_entity()'s docstring for why they're split rather than
# blended into one blanket ratio) — both deliberately conservative, per
# the module docstring's DEDUP section, but NAME_MATCH_THRESHOLD is
# lower because a name is a short, stable identifier ("Daemon
# deferral") while a description is free text that two independent
# extractions of the SAME decision can phrase quite differently ("was
# measured as justified but deliberately deferred" vs "confirmed still
# deferred; revisit once V3 settles") — confirmed live 2026-07-16: two
# real extractions of the daemon-deferral decision shared both words of
# a two-word name (ratio 1.0) but only 30% of their combined name+
# description keywords, missing the original single blended-ratio
# design entirely. Two thresholds fixes it without just loosening the
# blanket bar, which would have reopened the false-merge risk the
# module docstring's DEDUP section explains.
_NAME_MATCH_THRESHOLD = 0.5
_DEDUP_MATCH_THRESHOLD = 0.6


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def _row_to_record(row: sqlite3.Row) -> dict:
    return dict(row)


def get_all_entities(repo_path: str, limit: Optional[int] = None) -> list[dict]:
    """All entities for a repo, most-recently-updated first."""
    conn = _connect()
    try:
        query = "SELECT * FROM entities WHERE repo_path = ? ORDER BY last_updated DESC"
        params: tuple = (repo_path,)
        if limit is not None:
            query += " LIMIT ?"
            params = (repo_path, limit)
        rows = conn.execute(query, params).fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def search_entities(query: str, repo_path: str, limit: int = 5) -> list[dict]:
    """
    Keyword-overlap search over stored entities — same mechanism and same
    scope decision as memory/history.py's search_history() (see that
    module's docstring for the semantic-search-would-be-nicer tradeoff;
    identical reasoning applies here, not repeated).

    Scoring: each entity gets a score equal to the number of distinct
    query keywords found in its name + description. Rows with score 0
    are dropped. Ties broken by recency (last_updated DESC ordering is
    the fetch order; Python's sort is stable).
    """
    keywords = {
        w.lower() for w in re.findall(r"[a-zA-Z0-9_]+", query)
        if len(w) >= _MIN_KEYWORD_LEN
    }
    if not keywords:
        return []

    rows = get_all_entities(repo_path)

    scored = []
    for record in rows:
        haystack = f"{record['name']} {record['description']}".lower()
        overlap = sum(1 for kw in keywords if kw in haystack)
        if overlap > 0:
            scored.append((overlap, record))

    scored.sort(key=lambda pair: -pair[0])
    return [record for _, record in scored[:limit]]


def _keyword_set(text: str) -> set[str]:
    return {
        w.lower() for w in re.findall(r"[a-zA-Z0-9_]+", text)
        if len(w) >= _MIN_KEYWORD_LEN
    }


def _overlap_ratio(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _find_matching_entity(
    entity_type: str, name: str, description: str, existing: list[dict],
) -> Optional[dict]:
    """
    Best match among existing entities of the same type, checked against
    two independent signals (fixed 2026-07-16 after a real dedup miss —
    see _NAME_MATCH_THRESHOLD's comment above for the concrete numbers
    that motivated the split):

      1. Name-only overlap, against the lower _NAME_MATCH_THRESHOLD —
         names are short, stable labels; two extractions of the same
         real-world decision tend to name it similarly even when their
         descriptions diverge.
      2. Combined name+description overlap, against the higher, original
         _DEDUP_MATCH_THRESHOLD — the fallback for when names differ but
         the full text clearly describes the same thing.

    A candidate matches if EITHER signal clears its own bar; the row
    with the higher of the two ratios wins if more than one clears.
    Still returns None (insert a new row) far more often than it
    matches — see module docstring's DEDUP section for why that
    remains the deliberately safer default.
    """
    candidate_name_kw = _keyword_set(name)
    candidate_kw = _keyword_set(f"{name} {description}")
    if not candidate_kw:
        return None

    best_row = None
    best_score = 0.0
    for row in existing:
        if row["type"] != entity_type:
            continue

        existing_name_kw = _keyword_set(row["name"])
        existing_kw = _keyword_set(f"{row['name']} {row['description']}")
        if not existing_kw:
            continue

        name_ratio = _overlap_ratio(candidate_name_kw, existing_name_kw)
        combined_ratio = _overlap_ratio(candidate_kw, existing_kw)

        score = 0.0
        if name_ratio >= _NAME_MATCH_THRESHOLD:
            score = max(score, name_ratio)
        if combined_ratio >= _DEDUP_MATCH_THRESHOLD:
            score = max(score, combined_ratio)

        if score > best_score:
            best_score = score
            best_row = row

    return best_row if best_score > 0 else None


def commit_candidates(repo_path: str, candidates: list[dict]) -> list[int]:
    """
    Applies the commit gate (confidence >= _COMMIT_THRESHOLD) and dedup
    match to a batch of raw candidates, writing survivors to storage.

    Args:
        repo_path: Absolute path to the repo these candidates came from.
        candidates: Raw dicts from extract_candidates() — untrusted
                    model output. Anything missing a required field, an
                    unrecognized type, or an unrecognized confidence
                    level is silently discarded (defensive, not an
                    error) — same "malformed model output shouldn't
                    crash anything" posture as tools/registry.py's
                    unknown-tool-name handling.

    Returns:
        List of entity ids written or updated, in candidate order.
    """
    committed_ids: list[int] = []
    existing = get_all_entities(repo_path)  # fetched once; updated in-memory
    # below so within-batch duplicates also dedupe against each other,
    # not just against what was already in storage before this call.

    conn = _connect()
    try:
        for c in candidates:
            entity_type = c.get("type")
            name = (c.get("name") or "").strip()
            description = (c.get("description") or "").strip()
            confidence = c.get("confidence")

            if entity_type not in _VALID_TYPES or not name or not description:
                continue
            if confidence not in _CONFIDENCE_RANK:
                continue
            if _CONFIDENCE_RANK[confidence] < _CONFIDENCE_RANK[_COMMIT_THRESHOLD]:
                continue  # below the commit gate — discarded, not staged

            now = datetime.now().isoformat()
            match = _find_matching_entity(entity_type, name, description, existing)

            if match:
                conn.execute(
                    "UPDATE entities SET description = ?, last_updated = ? WHERE id = ?",
                    (description, now, match["id"]),
                )
                match["description"] = description  # keep in-memory copy
                match["last_updated"] = now          # current for later candidates in this batch
                committed_ids.append(match["id"])
                logger.info(f"entity memory: updated existing entity #{match['id']} ({entity_type}: {name})")
            else:
                cur = conn.execute(
                    """
                    INSERT INTO entities (repo_path, type, name, description, metadata, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (repo_path, entity_type, name, description, "{}", now),
                )
                new_row = {
                    "id": cur.lastrowid, "repo_path": repo_path, "type": entity_type,
                    "name": name, "description": description, "last_updated": now,
                }
                existing.append(new_row)
                committed_ids.append(cur.lastrowid)
                logger.info(f"entity memory: new entity #{cur.lastrowid} ({entity_type}: {name})")

        conn.commit()
    finally:
        conn.close()

    return committed_ids


def _parse_json_array(raw: str) -> list:
    """
    Best-effort JSON-array extraction from a model's raw text response.
    Deliberately simple compared to providers/ollama_provider.py's
    fallback tool-call parser: this isn't parsing a tool call out of a
    provider-specific transport quirk, it's parsing a plain-text
    completion the extraction prompt explicitly asked to be JSON-only,
    against backends already proven reliable at structured output
    (Faramir — the one exception — never reaches this function; see
    extract_candidates()). Two attempts, then give up quietly: strip a
    markdown fence if present and try straight json.loads(), then fall
    back to slicing between the first '[' and last ']' in case the model
    added any stray preamble/postamble despite being told not to. Never
    raises — malformed output just means zero candidates this run, the
    same graceful-degradation posture as every other optional LLM call
    in this project (memory/journal.py's synthesize_worked_on(), etc.).
    """
    if not raw:
        return []

    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        pass

    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            pass

    return []


_EXTRACTION_PROMPT_TEMPLATE = """\
A developer asked their coding assistant this question, and got this answer:

Question: {query}

Answer: {answer}

Identify any DECISIONS or CONCEPTS worth remembering long-term from this \
exchange.

A DECISION is something explicitly concluded or decided ("we chose X over \
Y", "this was deferred because...", "switched from A to B") — not \
something merely mentioned or explained in passing.

A CONCEPT is a specific idea, tradeoff, or piece of reusable knowledge \
substantively discussed — not just a term that happens to appear.

CRITICAL: most exchanges contain NEITHER. If this answer is just an \
explanation, a code walkthrough, or a simple factual lookup, return an \
empty JSON array — that is the normal, expected result for most \
questions. Do not force something into "decision" or "concept" just \
because you were asked to look.

Respond with ONLY a JSON array, no preamble, no markdown fences. Each \
item must have exactly these fields:
  "type": "decision" or "concept"
  "name": a short label (a few words)
  "description": one sentence describing its current state
  "confidence": "low", "medium", or "high" — "high" only if the text \
explicitly and unambiguously states this; "low" if you are inferring or \
extrapolating at all.

If nothing qualifies, respond with exactly: []\
"""


def extract_candidates(llm_client, backend_used: str, query: str, answer: str) -> list[dict]:
    """
    One completion asking the backend that just answered `query` to
    identify any decisions/concepts worth remembering from that exchange.
    Raw, uncommitted output — see commit_candidates() for the gate that
    actually decides what gets written.

    llm_client is duck-typed (see module docstring); backend_used selects
    force_provider so the SAME model that answered does the extraction —
    no separate model choice, no new gate, reuses the routing decision
    the complexity scorer already made. Returns [] (never raises) if
    llm_client is None, or if the call itself or the response parsing
    fails — extraction is best-effort by design, never allowed to affect
    the answer already returned to the user. Faramir's old exclusion
    here was removed 2026-07-21 (see module docstring's POPULATION
    MECHANISM section) — this function has never actually depended on
    tool-calling reliability at all, only on writing plain-text JSON.
    """
    if llm_client is None:
        return []

    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(query=query, answer=answer)

    try:
        response = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            force_provider=backend_used,
        )
    except Exception as e:
        logger.warning(f"Entity extraction call failed ({backend_used}): {e}")
        return []

    parsed = _parse_json_array(response.content)

    # Cheap, permanent observability (added 2026-07-16 after a real run
    # made an extraction call — visible in the log as a request — but
    # committed nothing, with no way to tell from the logs alone whether
    # that was a parsing bug or the model genuinely returning []. Traced
    # to GroqProvider.chat() never setting `temperature`, so Groq's
    # non-zero server default applies and the identical prompt can
    # legitimately return different candidates across separate calls
    # (confirmed: an isolated re-run of the exact same query/answer
    # returned 3 high-confidence candidates where the live run returned
    # none). Logged only when non-zero, since an empty result is the
    # expected common case (see the prompt's own "most exchanges contain
    # NEITHER" instruction) and shouldn't spam every ordinary run.
    if parsed:
        logger.info(f"entity extraction: {len(parsed)} raw candidate(s) from {backend_used}")

    return parsed


def extract_and_commit(
    llm_client, backend_used: str, repo_path: str, query: str, answer: str,
) -> list[int]:
    """
    Convenience wrapper: extract_candidates() -> commit_candidates() in
    one call, matching memory/journal.py's write_journal()-wraps-
    synthesize_worked_on() shape. See core/agent.py's Agent._extract_
    entities() for the actual call site.
    """
    candidates = extract_candidates(llm_client, backend_used, query, answer)
    if not candidates:
        return []
    return commit_candidates(repo_path, candidates)


if __name__ == "__main__":
    # Manual smoke test: python -m memory.entities
    # Exercises storage + dedup + commit gate WITHOUT a live LLM call
    # (candidates are hand-constructed, mirroring what extract_candidates()
    # would return) — same no-external-dependency philosophy as memory/
    # journal.py's smoke test. Live extraction is exercised via a real
    # `python cli.py ask "..."` against the real olorin repo instead.
    test_repo = "/fake/repo/for/entities/smoke/test"

    print("--- First batch: two decisions, one below the commit gate ---")
    candidates_v1 = [
        {
            "type": "decision", "name": "Daemon deferral",
            "description": "A persistent daemon was measured as justified but deliberately deferred until V2/V3 settle.",
            "confidence": "high",
        },
        {
            "type": "decision", "name": "Together AI dropped",
            "description": "Together AI's free tier was retired industry-side; the project stays free-tier-only.",
            "confidence": "medium",
        },
        {
            "type": "concept", "name": "Vague guess",
            "description": "Something that might be worth remembering, maybe.",
            "confidence": "low",  # below the commit gate — should be discarded
        },
    ]
    ids_v1 = commit_candidates(test_repo, candidates_v1)
    print(f"Committed ids: {ids_v1} (expect 2, not 3 — low-confidence discarded)")

    print("\n--- get_all_entities ---")
    for e in get_all_entities(test_repo):
        print(f"  [{e['type']}] {e['name']}: {e['description']}")

    print("\n--- Second batch: a near-duplicate of 'Daemon deferral' (should UPDATE, not insert) ---")
    candidates_v2 = [
        {
            "type": "decision", "name": "Daemon deferral decision",
            "description": "Daemon deferral confirmed still deferred; revisit once V3 settles further.",
            "confidence": "high",
        },
    ]
    ids_v2 = commit_candidates(test_repo, candidates_v2)
    all_entities = get_all_entities(test_repo)
    print(f"Committed ids: {ids_v2}")
    print(f"Total entity count after second batch: {len(all_entities)} (expect 2, not 3 — dedup matched)")

    print("\n--- search_entities('daemon') ---")
    for e in search_entities("daemon", test_repo):
        print(f"  MATCH: [{e['type']}] {e['name']}: {e['description']}")

    print("\n--- search_entities('nonexistent gibberish') (expect empty) ---")
    print(search_entities("nonexistent gibberish", test_repo))
