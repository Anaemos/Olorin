"""
tools/websearch.py — the web_search tool (OLORIN_PROJECT.md Section 9,
implemented 2026-07-13, cascade corrected same day after verifying real
free-tier terms).

Motivation, from Section 9's original design (2026-07-06): local models
hallucinate confidently on open-knowledge questions outside their
training data or the indexed repo. Groq/Cerebras have the identical
blind spot; this isn't a cost-avoidance substitute for cloud calls, it's
a grounding tool. As of 2026-07-13, available to Groq, Cerebras, and
Boromir — NOT Faramir, who is deliberately gated away from every tool
(see core/agent.py's Agent.run(), test_faramir_tools.py, and Section
9/13's account of why).

FOUR FREE-TIER BACKENDS, CASCADED — the same dependency-inversion +
graceful-fallback pattern already proven for LLM providers
(providers/base.py, Section 7), applied to search instead of chat. Order
reflects each backend's REAL free-tier shape, verified via web search
2026-07-13 rather than trusted from marketing pages — an earlier version
of this module claimed "3,500 free searches/month combined" for
Tavily+Serper, which was wrong (Serper's credits are one-time, not
monthly). See OLORIN_PROJECT.md Section 13 for the full research trail.

  1. Tavily (primary) — free 1,000 queries/MONTH, genuinely recurring,
     no card. The only backend here with a real sustained-forever-free
     story. LLM-ready extracted text, not raw snippets — matters more
     here than for a frontier model, since Boromir only has 16K context
     and already-fragile tool-call parsing (Section 13's bug history).

  2. Jina (s.jina.ai) — no card at all, ~1,000,000 free tokens per new
     API key (one-time per key, but a large single allotment), and works
     even without a key at a lower rate limit as an ultimate no-signup
     fallback. Also returns LLM-ready extracted content (Jina Reader),
     not raw SERP metadata — a real upgrade over what Serper offered in
     that slot. One real term worth being explicit about: Jina's free
     tier is licensed non-commercial (CC-BY-NC) — fine for this
     portfolio project, which isn't being sold, but a genuine license
     condition, not just a quota number.

  3. Serper (reserve) — free 2,500 credits, but a ONE-TIME signup grant,
     not monthly (corrected 2026-07-13 — the original design assumed
     monthly and was wrong; multiple independent sources confirm the
     one-time shape). Raw Google SERP metadata only (title/link/snippet,
     no extracted page text). Kept in the cascade as a reserve for when
     both recurring-ish backends above are down or exhausted, not as a
     routine fallback, since spending it doesn't come back.

  4. Exa (opt-in last resort) — $10 one-time credit with NO card
     required (~1,400 searches at $7/1k for standard search). The
     "1,000/month recurring" figure quoted around the web only applies
     once a payment method is on file, which this project declines for
     the same reason Together AI was dropped entirely (Section 13) — the
     difference here is Exa's card-free one-time credit is real and
     worth using, just not renewable, so it's deliberately last in the
     cascade. Exa's real differentiator is quality, not free volume:
     neural/semantic search that scores meaningfully higher on
     multi-hop retrieval benchmarks than keyword-based search — kept as
     an opt-in extra hop specifically for that reason, not dropped
     despite the shallow free tier.

A self-hosted alternative (SearXNG) was considered and deliberately NOT
built (2026-07-13, Section 13): real ongoing operational complexity
(Docker, uptime, upstream-engine blocking) with no measured need for it
yet — the same daemon-deferral reasoning applied here.
"""

import requests

import config
from core.logging_config import get_logger

logger = get_logger("websearch")


class SearchProviderError(Exception):
    """Raised when a search backend fails in a way the cascade should
    react to (quota exhausted, network error, bad key, non-200)."""


class _TavilyBackend:
    """Primary backend — the only genuinely recurring free tier of the
    four. See module docstring."""

    BASE_URL = "https://api.tavily.com/search"

    def is_available(self) -> bool:
        return bool(config.TAVILY_API_KEY)

    def search(self, query: str, max_results: int) -> list[dict]:
        try:
            resp = requests.post(
                self.BASE_URL,
                json={
                    "api_key": config.TAVILY_API_KEY,
                    "query": query,
                    "max_results": max_results,
                    "include_answer": False,
                },
                timeout=15,
            )
        except requests.RequestException as e:
            raise SearchProviderError(f"Tavily unreachable: {e}")

        if resp.status_code == 429:
            raise SearchProviderError(f"Tavily quota/rate limit hit: {resp.text[:200]}")
        if resp.status_code != 200:
            raise SearchProviderError(f"Tavily request failed ({resp.status_code}): {resp.text[:200]}")

        data = resp.json()
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
            for r in data.get("results", [])
        ]


class _JinaBackend:
    """
    Secondary backend (promoted here 2026-07-13, replacing Serper's old
    slot) — no card required at all, a large one-time token grant per
    key, and LLM-ready extracted text like Tavily rather than raw SERP
    data. Non-commercial license (CC-BY-NC) on the free tier — see
    module docstring.
    """

    BASE_URL = "https://s.jina.ai/"

    def is_available(self) -> bool:
        return bool(config.JINA_API_KEY)

    def search(self, query: str, max_results: int) -> list[dict]:
        try:
            resp = requests.get(
                self.BASE_URL,
                params={"q": query},
                headers={
                    "Authorization": f"Bearer {config.JINA_API_KEY}",
                    "Accept": "application/json",
                    "X-Respond-With": "no-content",  # snippets only, conserves the token grant
                },
                timeout=20,
            )
        except requests.RequestException as e:
            raise SearchProviderError(f"Jina unreachable: {e}")

        if resp.status_code == 429:
            raise SearchProviderError(f"Jina quota/rate limit hit: {resp.text[:200]}")
        if resp.status_code != 200:
            raise SearchProviderError(f"Jina request failed ({resp.status_code}): {resp.text[:200]}")

        data = resp.json()
        # s.jina.ai's JSON response nests results under "data" — each
        # entry has title/url/content(/description). Defensive .get()s
        # throughout since this is a less heavily-documented shape than
        # Tavily's.
        results = data.get("data", []) if isinstance(data, dict) else []
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("description") or r.get("content", "")[:500],
            }
            for r in results[:max_results]
        ]


class _SerperBackend:
    """
    Reserve backend — raw Google SERP metadata only, no content
    extraction, and its 2,500 free credits are a ONE-TIME signup grant,
    not monthly (corrected 2026-07-13, see module docstring). Tried only
    after Tavily and Jina both fail/are unconfigured.
    """

    BASE_URL = "https://google.serper.dev/search"

    def is_available(self) -> bool:
        return bool(config.SERPER_API_KEY)

    def search(self, query: str, max_results: int) -> list[dict]:
        try:
            resp = requests.post(
                self.BASE_URL,
                headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": max_results},
                timeout=15,
            )
        except requests.RequestException as e:
            raise SearchProviderError(f"Serper unreachable: {e}")

        if resp.status_code == 429:
            raise SearchProviderError(f"Serper quota/rate limit hit: {resp.text[:200]}")
        if resp.status_code != 200:
            raise SearchProviderError(f"Serper request failed ({resp.status_code}): {resp.text[:200]}")

        data = resp.json()
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "snippet": r.get("snippet", ""),
            }
            for r in data.get("organic", [])
        ]


class _ExaBackend:
    """
    Opt-in, last-resort backend — see module docstring for why it's kept
    despite the shallow (one-time, $10) card-free free tier: strongest
    semantic/neural search quality of the four. Requests `highlights`
    rather than full `text` per Exa's own docs recommendation for agent
    workflows (10x fewer tokens for the most relevant excerpts) — both to
    fit Boromir's context budget and to conserve the one-time credit,
    since highlights-only calls are cheaper than full-content extraction.
    """

    BASE_URL = "https://api.exa.ai/search"

    def is_available(self) -> bool:
        return bool(config.EXA_API_KEY)

    def search(self, query: str, max_results: int) -> list[dict]:
        try:
            resp = requests.post(
                self.BASE_URL,
                headers={"x-api-key": config.EXA_API_KEY, "Content-Type": "application/json"},
                json={
                    "query": query,
                    "numResults": max_results,
                    "contents": {"highlights": True},
                },
                timeout=20,
            )
        except requests.RequestException as e:
            raise SearchProviderError(f"Exa unreachable: {e}")

        if resp.status_code == 429:
            raise SearchProviderError(f"Exa quota/rate limit hit: {resp.text[:200]}")
        if resp.status_code != 200:
            raise SearchProviderError(f"Exa request failed ({resp.status_code}): {resp.text[:200]}")

        data = resp.json()
        results = []
        for r in data.get("results", [])[:max_results]:
            highlights = r.get("highlights") or []
            snippet = " ... ".join(highlights) if highlights else r.get("text", "")[:500]
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": snippet,
            })
        return results


_TAVILY = _TavilyBackend()
_JINA = _JinaBackend()
_SERPER = _SerperBackend()
_EXA = _ExaBackend()

# Order matters — see module docstring for the reasoning behind each
# position.
_CASCADE = [
    ("tavily", _TAVILY),
    ("jina", _JINA),
    ("serper", _SERPER),
    ("exa", _EXA),
]


def web_search(query: str, max_results: int = 5) -> dict:
    """
    Tool entry point — dispatched from tools/registry.py's execute_tool().

    Cascades through _CASCADE in order, mirroring core/llm_client.py's
    provider-cascade pattern: try the next configured backend, fall back
    on failure, never let a search-backend hiccup crash the agent loop.
    No backend configured is a normal, expected state (all four are
    opt-in via .env) — not an error, just a clear message steering the
    model back to its trained knowledge instead of silently returning
    nothing and letting it guess.
    """
    any_configured = any(backend.is_available() for _, backend in _CASCADE)
    if not any_configured:
        return {
            "error": (
                "web_search is not configured (no TAVILY_API_KEY, "
                "JINA_API_KEY, SERPER_API_KEY, or EXA_API_KEY in .env) — "
                "answer from your trained knowledge instead, and say so "
                "plainly if you're not confident or the information could "
                "be outdated."
            ),
            "results": [],
        }

    for name, backend in _CASCADE:
        if not backend.is_available():
            continue
        try:
            results = backend.search(query, max_results)
            return {"provider": name, "results": results}
        except SearchProviderError as e:
            logger.warning(f"{name} failed, trying next backend: {e}")

    return {
        "error": "All configured web search backends failed right now — answer from trained knowledge, and say so.",
        "results": [],
    }


if __name__ == "__main__":
    # Manual smoke test: python -m tools.websearch
    for name, backend in _CASCADE:
        print(f"{name} configured: {backend.is_available()}")

    if not any(backend.is_available() for _, backend in _CASCADE):
        print("No backend configured — add at least one of TAVILY_API_KEY / JINA_API_KEY / SERPER_API_KEY / EXA_API_KEY to .env.")
    else:
        result = web_search("what is the current stable version of Python")
        print(f"\nProvider used: {result.get('provider', 'none')}")
        for r in result.get("results", [])[:3]:
            print(f"- {r['title']}: {r['url']}")
            print(f"  {r['snippet'][:150]}")
