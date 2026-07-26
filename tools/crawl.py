"""
tools/crawl.py — crawl_page agent tool (V4/searxNcrawl integration,
OLORIN_PROJECT.md Section 11/13's 2026-07-24 plan, revised 2026-07-26
after actually reading the real cloned repo instead of trusting
2026-07-14's research notes).

SCOPE CORRECTION FROM THE ORIGINAL PLAN (2026-07-26): the 2026-07-14/24
plan assumed a thin library requiring a hand-built SearXNG deployment
plus a custom wrapper around a loosely-specified crawler.crawl_page
call. The real cloned repo (C:\\Users\\Aryavart\\searxNcrawl, pip package
"searxNcrawl" v0.30.0) turned out to already ship a clean, stable Python
API: `from crawler import crawl_page` returns a `CrawledDocument`
dataclass (final_url, status, markdown, metadata, error_message) with
its own built-in 30s timeout — no custom async plumbing needed. It also
ships a `search` function requiring SearXNG, but Olorin doesn't use
that — Olorin already has its own four-backend web_search cascade
(tools/websearch.py) — so SearXNG is correctly NOT deployed for this
integration; only crawl4ai/Playwright are required, and crawl_page has
zero SearXNG dependency.

What this module adds on top of the raw library call, matching the
safety plan already sketched in OLORIN_PROJECT.md Section 13 before any
code was written:

1. URL SCHEME ALLOWLIST — http/https only. Rejects file://, data://,
   and anything else outright before it ever reaches crawl4ai.
2. SSRF-STYLE GUARD — the url argument is model-chosen, and could in
   principle be steered by adversarial content encountered elsewhere in
   a run (a prompt-injection-shaped link inside a web_search result, for
   instance). Resolves the hostname and rejects it if ANY resolved IP is
   private, loopback, link-local, or otherwise reserved (covers
   127.0.0.0/8, 169.254.0.0/16 — cloud metadata endpoints included —
   10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, and IPv6 equivalents via
   Python's own ipaddress classification rather than a hand-rolled CIDR
   list). This is a personal local tool with no external users, but the
   guard costs little and closes a real category of risk rather than
   assuming good faith from every URL a model happens to pick.
3. CONTENT CAP — a direct lesson from bug #17 (PDF-hang story,
   OLORIN_PROJECT.md Section 13): any tool that ingests external content
   needs a cap from day one. crawl_page() already has its own 30s
   request timeout built into the library (verified by reading
   crawler/__init__.py's DEFAULT_PAGE_TIMEOUT before relying on it, not
   assumed) so no timeout wrapping is needed here — only the content-size
   cap, mirroring read_file's max_chars/truncated pattern.

Deliberately NOT wired into web_search's cascade or its result shape —
this is a genuinely different operation (fetch one specific page in
full vs. return snippets across many results), the same reasoning
git_diff got its own tool instead of folding into search_codebase.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from crawler import RunConfigOverrides, build_markdown_run_config
from crawler import crawl_page as _lib_crawl_page

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_CONTENT_CHARS = 15000

# searxNcrawl's default run config (crawler/config.py's
# build_markdown_run_config()) is tuned specifically for documentation/
# article pages: it waits for `document.querySelector('main')` to exist
# with >50 chars of text before extracting anything. That's a real, good
# default for the doc-site use case this library was built around, but
# it's a hard gate, not a hint — a page with no semantic <main> element
# (confirmed live 2026-07-26 against https://example.com, which has no
# <main> tag at all) can never satisfy it, so crawl4ai just waits out
# the full page_timeout (30s) and reports a timeout, not a fast, honest
# "no main content found" result. Since crawl_page here needs to handle
# arbitrary pages the agent might be pointed at (a README render, a blog
# post without semantic markup, a plain text file), not just docs sites,
# the wait condition is overridden to a general "has the page actually
# rendered real content" check (body text > 50 chars) — still waits out
# genuinely JS-heavy pages, just doesn't require a specific tag to exist.
# Everything else from the default config is kept as-is (nav/footer/ad
# exclusion selectors, the markdown generator, target_elements as an
# extraction preference rather than a wait gate) — this is a narrow,
# single-field override, not a rebuild.
_OLORIN_RUN_CONFIG = build_markdown_run_config(
    RunConfigOverrides(
        wait_for=(
            "js:() => document.body && "
            "document.body.innerText.trim().length > 50"
        ),
    )
)


class CrawlValidationError(Exception):
    """Raised for a URL that fails validation before ever reaching
    crawl4ai — caught at the tool boundary and turned into an honest
    {"error": ...} result, same pattern as git_diff.py's GitError."""


def _validate_url(url: str) -> None:
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise CrawlValidationError(
            f"Invalid URL scheme '{parsed.scheme or '(none)'}': only "
            "http and https are allowed."
        )

    hostname = parsed.hostname
    if not hostname:
        raise CrawlValidationError(f"Could not parse a hostname from '{url}'.")

    # Resolve and check every returned address, not just the first —
    # a hostname can resolve to multiple IPs (round-robin DNS), and
    # only one of them being private is enough to be a real SSRF vector.
    try:
        resolved = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise CrawlValidationError(f"Could not resolve hostname '{hostname}': {e}")

    for family, _, _, _, sockaddr in resolved:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise CrawlValidationError(
                f"'{hostname}' resolves to a private/internal address "
                f"({ip_str}) — refusing to crawl it."
            )


def crawl_page(repo_root: str, url: str, max_chars: int = _MAX_CONTENT_CHARS) -> dict:
    """
    Fetches a real web page and returns its extracted content as
    markdown — the thing web_search's snippets can't do: reading a full
    linked page rather than a summary of it.

    Args:
        repo_root: Injected by execute_tool() for dispatch-signature
                   uniformity, same as web_search — this tool has no
                   notion of a repo scope, deliberately unused.
        url: The page to crawl. Validated (scheme + SSRF guard) before
             any request is made — see module docstring.
        max_chars: Cap on returned markdown content. Mirrors read_file's
                   max_chars/truncated pattern.

    Returns:
        dict — {"url", "title", "content", "truncated"} on success, or
        {"error": ...} for a validation failure, a timeout, a crawl
        failure (bad status code, unreachable site, etc.), or any other
        exception — matching web_search's honest-error pattern so the
        model can fall back to its own knowledge/reasoning rather than
        treating a missing tool result as a hard stop.
    """
    try:
        _validate_url(url)
    except CrawlValidationError as e:
        return {"error": str(e)}

    try:
        doc = _lib_crawl_page(url, config=_OLORIN_RUN_CONFIG, dedup_mode="exact")
    except TimeoutError:
        return {"error": f"Timed out crawling '{url}' (30s limit)."}
    except Exception as e:
        # crawl4ai/Playwright can raise a wide variety of exception
        # types (browser launch failures, navigation errors, etc.) —
        # caught broadly here, same posture as websearch.py's per-
        # backend exception handling, since a page-fetch failure should
        # degrade to an honest error, not crash the agent loop.
        return {"error": f"Failed to crawl '{url}': {e}"}

    if doc.status == "failed":
        return {"error": doc.error_message or f"Crawl failed for '{url}' (no further detail)."}

    title = doc.metadata.get("title_clean") or doc.metadata.get("title") or ""
    content = doc.markdown or ""
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]

    return {
        "url": doc.final_url,
        "title": title,
        "content": content,
        "truncated": truncated,
    }


if __name__ == "__main__":
    # Manual smoke test: python -m tools.crawl
    # NOT YET RUN — searxNcrawl (crawl4ai + Playwright/Chromium) hasn't
    # been installed into this venv yet. Run the install steps first
    # (see ENGINEERING_JOURNAL.md's 2026-07-26 entry), then run this.
    print("--- crawl_page(valid public URL) ---")
    result = crawl_page("", "https://example.com")
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"url={result['url']}")
        print(f"title={result['title']!r}")
        print(f"content length={len(result['content'])} truncated={result['truncated']}")
        print(f"content preview:\n{result['content'][:300]}")

    print("\n--- crawl_page(invalid scheme) ---")
    result = crawl_page("", "file:///etc/passwd")
    print(result)
    assert "error" in result

    print("\n--- crawl_page(SSRF-shaped: localhost) ---")
    result = crawl_page("", "http://localhost:8080/admin")
    print(result)
    assert "error" in result

    print("\n--- crawl_page(SSRF-shaped: private IP) ---")
    result = crawl_page("", "http://192.168.1.1/")
    print(result)
    assert "error" in result

    print("\n--- crawl_page(SSRF-shaped: cloud metadata endpoint) ---")
    result = crawl_page("", "http://169.254.169.254/latest/meta-data/")
    print(result)
    assert "error" in result

    print("\n--- crawl_page(nonexistent domain) ---")
    result = crawl_page("", "https://this-domain-genuinely-does-not-exist-xyz123.com")
    print(result)
    assert "error" in result
