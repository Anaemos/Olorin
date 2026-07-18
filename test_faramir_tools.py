"""
test_faramir_tools.py — isolated tool-calling reliability test for
Faramir (deepseek-r1:8b), run once before trusting her real tool calls
in the live agent loop — web_search included.

STANDING ACTION ITEM since 2026-07-06 (OLORIN_PROJECT.md Section 9):
Faramir emits <think>...</think> before real output, which can break
tool-call parsing that expects clean structured output — the model may
narrate ABOUT calling a tool inside her reasoning instead of emitting an
actual tool-call token. Boromir (Qwen-based, fine-tuned for function
calling) doesn't share this failure mode. Section 13 later found Faramir
and Boromir share a Qwen3 base architecture (2026-07-09) — that's
ancestry, not a tool-calling reliability guarantee, and this test is
exactly the thing that was never actually run to confirm one way or the
other. Do not assume parity with Boromir just because both now trace
back to the same base weights.

Deliberately isolated from the real agent loop and the real tool
registry — a single trivial forced-tool-call prompt against a throwaway
dummy tool that has nothing to do with search_codebase/read_file/
web_search. A failure here is unambiguously about Faramir's tool-calling
mechanics in general, not about anything specific to a real tool's
semantics or the ReAct loop's message handling.

Run: python test_faramir_tools.py
(Requires Ollama running locally with the faramir model built — same
prerequisite as any other `python cli.py ask "Faramir, ..."` call.)
"""

import config
from providers.ollama_provider import OllamaProvider

DUMMY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def main():
    provider = OllamaProvider(config.FARAMIR_MODEL, think=True)
    print(f"Faramir available: {provider.is_available()}")
    if not provider.is_available():
        print("Ollama isn't reachable — start it and make sure the "
              "faramir model exists (`ollama list`) before running this.")
        return

    prompt = "What's the weather like in Tokyo right now? Use the get_weather tool to find out."
    response = provider.chat(
        messages=[{"role": "user", "content": prompt}],
        tools=DUMMY_TOOL,
    )

    print(f"\nfinish_reason: {response.finish_reason}")
    print(f"tool_calls: {response.tool_calls}")
    print(f"content (should be empty/minimal if tool_calls fired cleanly): {response.content!r}")

    if response.finish_reason == "tool_calls" and response.tool_calls:
        tc = response.tool_calls[0]
        if tc["name"] == "get_weather" and "Tokyo" in tc["arguments"]:
            print(
                "\nPASS — Faramir called the tool correctly, clean "
                "structured output. Safe to trust her real tool calls "
                "(web_search included) in the live agent loop."
            )
        else:
            print(
                "\nPARTIAL — tool_calls fired but arguments look off, "
                "inspect above before trusting this in the live loop."
            )
    else:
        print(
            "\nFAIL — Faramir did not produce a clean tool_calls response. "
            "This confirms the Section 9 risk empirically: her real tool "
            "calls (search_codebase/read_file/web_search, all of them, "
            "not just web_search) may be silently unreliable in the live "
            "agent loop. Check whether providers/ollama_provider.py's "
            "fallback JSON parser caught a malformed shape (look for a "
            "warning-level log line above) before deciding this needs a "
            "real fix rather than just being aware of the risk."
        )


if __name__ == "__main__":
    main()
