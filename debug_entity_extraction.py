"""
debug_entity_extraction.py — standalone diagnostic for a live extraction
call, isolated from the rest of the agent loop (same pattern as
test_faramir_tools.py / test_websearch_backends.py).

Written 2026-07-16: a real `ask "why was the daemon deliberately
deferred instead of built?"` made an extraction call (visible in the
log as an extra groq request right after the final answer) but
`cli.py entities` showed nothing committed afterward. This uses the
EXACT query/answer from that real run and prints the RAW model
response before any parsing, so we can tell whether this is a parsing
bug, a commit-gate miss, or the model genuinely judging this doesn't
qualify — before changing anything based on a guess.

Run from the olorin root: python debug_entity_extraction.py
"""

from core.llm_client import LLMClient
from memory import entities as entities_store

QUERY = "why was the daemon deliberately deferred instead of built?"
ANSWER = (
    "The daemon was deliberately deferred instead of built because the "
    "profiling confirmed that the import overhead is real, but it's no "
    "longer the dominant cost once inference time is counted. The "
    "decision was made to not build the daemon now, as it would add "
    "ongoing complexity, and the feature set is about to change "
    "significantly in V2. The daemon would eliminate import overhead "
    "but cannot touch inference time, which is the actual largest cost "
    "on realistic queries. The decision was made to revisit the daemon "
    "after V2/V3, and the ~6-10s tax is real but bounded, and nothing "
    "in V1 is unusable because of it."
)

llm = LLMClient()

prompt = entities_store._EXTRACTION_PROMPT_TEMPLATE.format(query=QUERY, answer=ANSWER)
print("=== PROMPT SENT ===")
print(prompt)
print()

response = llm.chat(
    messages=[{"role": "user", "content": prompt}],
    force_provider="groq",
)

print("=== RAW RESPONSE (repr, so whitespace/fences are visible) ===")
print(repr(response.content))
print()

parsed = entities_store._parse_json_array(response.content)
print("=== PARSED CANDIDATES ===")
print(parsed)
print()

if parsed:
    committed = entities_store.commit_candidates("/tmp/debug-entity-repo", parsed)
    print(f"=== Commit gate result: would commit ids {committed} ===")
    for c in parsed:
        print(f"  candidate: type={c.get('type')!r} name={c.get('name')!r} confidence={c.get('confidence')!r}")
else:
    print(
        "=== Nothing parsed. Check RAW RESPONSE above: either the model "
        "returned an empty array (a real judgment call, not a bug), or "
        "the response wasn't valid/extractable JSON (a real parsing bug "
        "in _parse_json_array). ==="
    )
