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

# Together AI / Cerebras are V2 stubs — read now so config.py doesn't need
# to change again when those providers go live, but they're allowed to be
# absent in V1.
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY")  # None until V2
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")  # None until V2


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
    print(f"  Ollama base URL: {OLLAMA_BASE_URL}")
    print(f"  Boromir model: {BOROMIR_MODEL}")
    print(f"  Faramir model: {FARAMIR_MODEL}")
    print(f"  Complexity threshold: {COMPLEXITY_THRESHOLD}")
    print(f"  Max agent steps: {MAX_AGENT_STEPS}")
