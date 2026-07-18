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

# Named models (see OLORIN_PROJECT.md Section 4 — these are Modelfile-wrapped
# versions of the base weights, with 16K context baked in).
BOROMIR_MODEL = os.getenv("BOROMIR_MODEL", "boromir")
FARAMIR_MODEL = os.getenv("FARAMIR_MODEL", "faramir")

# Ollama doesn't require a real API key, but the openai SDK client demands
# a non-empty string in the constructor. This is a placeholder, not a secret.
OLLAMA_API_KEY = "ollama-local-no-key-required"


# --- Cloud model name -----------------------------------------------------

GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


# --- Routing / complexity scorer -------------------------------------------

# See OLORIN_PROJECT.md Section 7 for the scoring formula this threshold
# gates. Tunable without touching code — raise it to send fewer queries
# to Groq, lower it to escalate more aggressively.
COMPLEXITY_THRESHOLD = int(os.getenv("COMPLEXITY_THRESHOLD", "15"))


# --- Circuit breaker --------------------------------------------------------

# Minutes to disable a provider after a 429 before retrying it.
CIRCUIT_BREAKER_COOLDOWN_MINUTES = int(
    os.getenv("CIRCUIT_BREAKER_COOLDOWN_MINUTES", "5")
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
    print(f"  Boromir model: {BOROMIR_MODEL}")
    print(f"  Faramir model: {FARAMIR_MODEL}")
    print(f"  Complexity threshold: {COMPLEXITY_THRESHOLD}")
    print(f"  Max agent steps: {MAX_AGENT_STEPS}")
