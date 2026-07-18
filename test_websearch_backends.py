"""
test_websearch_backends.py — isolated per-backend smoke test for the
web_search cascade (tools/websearch.py).

Why this exists: in normal cascade order, Tavily is configured and
succeeds first, so Jina/Serper/Exa never actually get exercised live —
only Tavily has real verification so far (2026-07-14 session).
This script calls each backend's .search() directly, bypassing cascade
order, so each one gets an independent real pass/fail signal.

Run from the olorin root: python test_websearch_backends.py
"""

from tools.websearch import _TAVILY, _JINA, _SERPER, _EXA

QUERY = "what is the current stable version of Python"

BACKENDS = [
    ("tavily", _TAVILY),
    ("jina", _JINA),
    ("serper", _SERPER),
    ("exa", _EXA),
]

for name, backend in BACKENDS:
    print(f"\n=== {name} ===")
    if not backend.is_available():
        print("  SKIPPED — no API key configured")
        continue
    try:
        results = backend.search(QUERY, max_results=3)
        print(f"  OK — {len(results)} result(s)")
        for r in results:
            print(f"  - {r['title']}: {r['url']}")
            print(f"    {r['snippet'][:150]}")
    except Exception as e:
        print(f"  FAILED — {type(e).__name__}: {e}")
