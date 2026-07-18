"""
indexer/capsules.py — Knowledge Capsule generation (module-level summaries).

Only knows how to SYNTHESIZE a summary from file content, via one tool-free
LLM completion per module. Storage lives in memory/capsules.py. Deliberately
takes an LLM client as a plain argument rather than importing
core.llm_client directly — indexer/ has had no dependency on core/ up to
this point, and there's no reason to introduce one here. cli.py, which
already orchestrates across indexer/, memory/, and core/, wires the two
together.

Why Faramir generates capsules, not Boromir: Section 2's lore assigns
Faramir to "general reasoning, comparison, knowledge questions" and
Boromir to fast, task-focused tool execution — synthesizing a holistic
"what does this module do" summary is squarely the former, not the
latter. The known risk logged elsewhere (deepseek-r1's <think> block
confusing tool-call parsing) doesn't apply here: capsule generation makes
no tool calls, it's a single plain completion.

Both caps below exist because capsules get injected into the agent's
system prompt (core/agent.py's build_system_prompt()) at the start of
every run — their size is a standing tax on every conversation's local
context budget (Boromir/Faramir run at 16K ctx), not a one-time
generation-time cost. See OLORIN_PROJECT.md Section 10 for the full
context-budget reasoning (step-1-only injection, length cap).
"""

# Per-file truncation so one unusually large file can't dominate a
# module's generation prompt or blow the local model's context budget
# during generation itself.
MAX_CHARS_PER_FILE = 3000

# Overall cap on how much source text feeds into one generation call —
# roughly 6K tokens, leaving headroom in Faramir's 16K ctx for the prompt
# scaffolding and the response itself. Files beyond this budget are
# simply left out of that module's summary — a pragmatic sampling
# decision, not an attempt to summarize every line of a large module.
MAX_TOTAL_CHARS = 24000


def _build_module_context(files: list[dict]) -> str:
    """files: [{"path": str, "content": str}, ...]"""
    parts = []
    total = 0
    for f in files:
        content = f["content"][:MAX_CHARS_PER_FILE]
        chunk = f"--- {f['path']} ---\n{content}\n"
        if total + len(chunk) > MAX_TOTAL_CHARS:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts)


def generate_module_summary(llm_client, module_name: str, files: list[dict]) -> str:
    """
    Args:
        llm_client: anything with a .chat(messages, force_provider=...)
                    method matching core.llm_client.LLMClient's signature
                    (duck-typed deliberately — see module docstring).
        module_name: top-level directory name, e.g. "core", "providers".
        files: [{"path": str, "content": str}, ...] — every code file
               currently in this module.

    Returns:
        A short prose summary (a few sentences), stripped of leading/
        trailing whitespace. Length is enforced by the prompt, not
        post-hoc truncation — asking the model to stay short produces a
        more coherent result than cutting off mid-sentence would.
    """
    context = _build_module_context(files)

    prompt = (
        f"Below is a sample of the source files in the '{module_name}' "
        "module of a codebase.\n\n"
        f"{context}\n\n"
        "Write a 3-4 sentence summary (under 100 words) covering: what "
        "this module is responsible for, its key files or components, "
        "and how it fits into the rest of the system. Prose only — no "
        "code snippets, no bullet lists, no headers."
    )

    response = llm_client.chat(
        messages=[{"role": "user", "content": prompt}],
        force_provider="faramir",
    )
    return response.content.strip()
