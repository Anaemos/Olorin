"""
config.py — Central configuration for Project Olorin.

Loads all API keys, model names, and tunable thresholds from .env.
Nothing else in the codebase should read os.environ directly —
everything goes through this module so there's one source of truth.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # reads .env in the current working directory


# --- Provider credentials -----------------------------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError(
        "GROQ_API_KEY is missing. Create a .env file with GROQ_API_KEY=your_key "
        "(get one free at console.groq.com)."
    )

# Together AI is a deliberately unbuilt stub (decision 2026-07-13): its
# free signup credit was retired industry-side in July 2025 and it now
# requires a $5 minimum purchase, so this project stays free-tier-only
# and doesn't implement a provider class for it. See
# OLORIN_PROJECT.md Section 13 for the full reasoning. Read here anyway
# so config.py doesn't need another change if that decision is revisited.
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY")  # unused — see comment above

# Cerebras — LIVE second-tier cloud fallback (2026-07-13, providers/
# cerebras_provider.py). Genuinely free (no card, ~1M tokens/day) but
# optional: None here just means the auto-routing cascade skips it and
# falls straight to local, same as if it never existed. Get a free key
# at https://cloud.cerebras.ai.
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")

# Free-tier model catalog churns (see cerebras_provider.py's module
# docstring) — env-overridable for exactly that reason, same pattern as
# GROQ_MODEL below. Check https://inference-docs.cerebras.ai/models/overview
# if this default ever starts failing.
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")

# web_search tool (Section 9) — all four optional, opt-in via .env.
# Cascade order (corrected 2026-07-13 after verifying real free-tier
# terms, not the terms these services advertise on marketing pages):
#   1. Tavily  — 1,000 queries/month, genuinely RECURRING, no card.
#      The only backend here with a real sustained-forever-free story.
#   2. Jina (s.jina.ai) — no card at all, ~1M free tokens per new key
#      (one-time per key, but large), LLM-ready extracted text like
#      Tavily (not raw SERP data). Free tier is non-commercial use
#      (CC-BY-NC) — fine for this portfolio project, but a real license
#      term, not just a quota.
#   3. Serper — 2,500 credits, ONE-TIME on signup, NOT monthly (corrected
#      2026-07-13 — the original "2,500/month" claim in this project's
#      own docs was wrong). Raw SERP snippets, no content extraction.
#      Kept as a reserve, not a routine fallback, since spending it
#      doesn't come back.
#   4. Exa — opt-in, last resort. $10 one-time credit with no card
#      (~1,400 searches at $7/1k); the oft-advertised "1,000/month
#      recurring" figure only applies if a payment method is added,
#      which this project deliberately declines (same reasoning as
#      dropping Together AI, Section 13). Strongest semantic/neural
#      search quality of the four, which is why it's kept as an opt-in
#      extra hop rather than dropped entirely, despite the shallow free
#      tier. See tools/websearch.py for the full cascade implementation.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
JINA_API_KEY = os.getenv("JINA_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
EXA_API_KEY = os.getenv("EXA_API_KEY")


# --- Local (Ollama) settings ---------------------------------------------

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

# Single collapsed local model (2026-07-20 — see OLORIN_PROJECT.md
# Section 13's V4 specialist-routing entry). Boromir and Faramir now run
# on the SAME loaded qwen3:8b weights ("local:latest" — FROM qwen3:8b,
# num_ctx 16384, nothing persona-specific baked in); persona is purely
# an application-layer concern now (system prompt text + the `think`
# flag per request, both applied in core/llm_client.py/core/agent.py),
# not a Modelfile concept. Verified live: switching `think` on an
# already-loaded model costs ~0.2s (noise-level) vs ~8s for a genuine
# cold load — zero swap cost between personas now, they're one model.
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "local:latest")

# Ollama doesn't require a real API key, but the openai SDK client demands
# a non-empty string in the constructor. This is a placeholder, not a secret.
OLLAMA_API_KEY = "ollama-local-no-key-required"


# --- Cloud model name -----------------------------------------------------

# Switched 2026-07-25 from llama-3.3-70b-versatile to openai/gpt-oss-120b
# (see OLORIN_PROJECT.md Section 13 for the full story). Two independent
# reasons converged the same day: (1) Groq officially deprecated
# llama-3.3-70b-versatile on 2026-06-17, shutdown date 2026-08-16 — we
# were inside that transition window, and Groq's own migration guidance
# explicitly recommends openai/gpt-oss-120b or qwen/qwen3.6-27b for
# "superior tool use capabilities... improved reliability." (2) A real,
# in-project data point independent of Groq's own claim: every malformed-
# tool-call failure this project has EVER logged (bugs #10, #14, #18, and
# a whole session's worth on 2026-07-25) was llama-3.3-70b-versatile;
# Cerebras — which already runs gpt-oss-120b, one of Groq's own
# recommended replacements — has never once thrown this error in this
# project's history. Picked gpt-oss-120b over qwen/qwen3.6-27b
# specifically because this project already has live, working evidence
# of gpt-oss-120b's tool-calling reliability via Cerebras, rather than a
# second unknown model. Still fully env-overridable — same escape hatch
# as CEREBRAS_MODEL if this default ever needs to change again.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


# --- Routing / complexity scorer -------------------------------------------

# See OLORIN_PROJECT.md Section 7 for the scoring formula this threshold
# gates. Tunable without touching code — raise it to send fewer queries
# to Groq, lower it to escalate more aggressively.
COMPLEXITY_THRESHOLD = int(os.getenv("COMPLEXITY_THRESHOLD", "15"))


# --- Circuit breaker --------------------------------------------------------

# Minutes to disable a provider after a genuinely transient rate limit
# (per-minute/per-request) before retrying it.
CIRCUIT_BREAKER_COOLDOWN_MINUTES = int(
    os.getenv("CIRCUIT_BREAKER_COOLDOWN_MINUTES", "5")
)

# Real gap found live (2026-07-19): a DAILY token-budget exhaustion (Groq's
# TPD limit, Cerebras's own documented daily-quota case) doesn't recover
# in 5 minutes the way a per-minute rate limit does — the same daily cap
# is still exhausted on every retry until it naturally resets. Using the
# short cooldown for this case meant every subsequent query re-attempted
# a call guaranteed to fail again, for the rest of the day. A separate,
# much longer cooldown for specifically-daily-quota errors (detected via
# providers/base.py's classify_quota_error()) avoids that pointless
# repeated latency. 240 minutes (4 hours) is a deliberately simple,
# fixed duration rather than calculating an exact UTC reset time (which
# neither Groq's nor Cerebras's docs commit to precisely) — long enough
# to stop the wasteful retry pattern for the rest of a normal working
# session, short enough not to disable a provider for an entire day if
# the quota actually resets sooner than expected.
DAILY_QUOTA_COOLDOWN_MINUTES = int(
    os.getenv("DAILY_QUOTA_COOLDOWN_MINUTES", "240")
)


# --- Agent loop --------------------------------------------------------

# Hard cap on ReAct loop iterations to prevent infinite tool-call loops.
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "10"))


# --- Paths --------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CHROMA_DB_PATH = os.getenv("CHROMA_DB_PATH", os.path.join(PROJECT_ROOT, "chroma_db"))
MEMORY_DB_PATH = os.getenv("MEMORY_DB_PATH", os.path.join(PROJECT_ROOT, "memory.db"))


if __name__ == "__main__":
    # Quick sanity check: run `python config.py` to confirm .env loaded correctly
    # without ever printing the actual key.
    print("Config loaded successfully.")
    print(f"  Groq API key present: {bool(GROQ_API_KEY)}")
    print(f"  Groq model: {GROQ_MODEL}")
    print(f"  Cerebras API key present: {bool(CEREBRAS_API_KEY)}")
    print(f"  Cerebras model: {CEREBRAS_MODEL}")
    print(f"  Tavily API key present: {bool(TAVILY_API_KEY)}")
    print(f"  Jina API key present: {bool(JINA_API_KEY)}")
    print(f"  Serper API key present: {bool(SERPER_API_KEY)}")
    print(f"  Exa API key present: {bool(EXA_API_KEY)}")
    print(f"  Ollama base URL: {OLLAMA_BASE_URL}")
    print(f"  Local model (Boromir+Faramir, collapsed 2026-07-20): {LOCAL_MODEL}")
    print(f"  Complexity threshold: {COMPLEXITY_THRESHOLD}")
    print(f"  Max agent steps: {MAX_AGENT_STEPS}")
