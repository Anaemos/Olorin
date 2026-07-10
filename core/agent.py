"""
core/agent.py — Raw ReAct agent loop (Section 9 of OLORIN_PROJECT.md).

No LangChain. No LangGraph. Every line here is meant to be explainable in
an interview — this is deliberately hand-written so the mechanics of
"model reasons, calls a tool, reasons over the result, repeats" are fully
visible rather than hidden inside a framework.

Flow per step:
  1. Send messages + tool schemas to LLMClient.
  2. If the model returns a final answer (finish_reason == "stop"), done.
  3. If the model wants to call tools (finish_reason == "tool_calls"),
     execute each one via tools/registry.py, append results as "tool"
     role messages, and loop back to step 1 so the model can reason over
     what it just learned.
  4. Hard-capped at MAX_AGENT_STEPS to prevent an infinite loop if a model
     keeps calling tools without ever producing a final answer.
"""

import json

import config
from core.llm_client import LLMClient
from core.logging_config import get_logger
from tools.registry import TOOL_SCHEMAS, execute_tool, parse_tool_call_arguments

logger = get_logger("agent")


def build_system_prompt(repo_root: str) -> str:
    """
    V1 system prompt. OLORIN.md per-repo config injection (Section 8) is
    a V2 feature — this is deliberately the plain version for now.
    """
    return (
        "You are Olorin, a local-first engineering assistant with access "
        "to tools for exploring a codebase.\n\n"
        f"The repository root is: {repo_root}\n\n"
        "You have three tools available:\n"
        "- search_codebase: semantic search over the indexed codebase — "
        "try this first for conceptual/\"how does X work\" questions. If "
        "the repo hasn't been indexed yet, it'll tell you so.\n"
        "- list_files: list files matching a glob pattern\n"
        "- read_file: read the full contents of a specific file\n\n"
        "When asked about the codebase, ground your answer in the actual "
        "code rather than guessing — use search_codebase to find relevant "
        "chunks, falling back to list_files/read_file if search isn't "
        "available or doesn't find what you need. Give a direct, grounded "
        "answer once you have enough information — don't call tools more "
        "than necessary."
    )


class Agent:
    """
    Owns one LLMClient and runs the ReAct loop against a single repo_root.
    """

    def __init__(self, repo_root: str, llm_client: LLMClient | None = None):
        self.repo_root = repo_root
        self.llm = llm_client or LLMClient()

    def run(self, user_query: str, force_provider: str | None = None) -> str:
        messages = [
            {"role": "system", "content": build_system_prompt(self.repo_root)},
            {"role": "user", "content": user_query},
        ]

        for step in range(config.MAX_AGENT_STEPS):
            logger.info(f"step={step + 1} | sending {len(messages)} messages")

            response = self.llm.chat(
                messages=messages,
                tools=TOOL_SCHEMAS,
                query=user_query,
                force_provider=force_provider,
            )

            if response.finish_reason == "stop":
                logger.info(f"step={step + 1} | final answer from {response.provider}")
                return response.content

            if response.finish_reason == "tool_calls":
                # Record the assistant's tool-call request in the transcript
                # before executing anything — the message history must
                # match what actually happened, in order, for the next
                # model call to make sense of the tool results that follow.
                messages.append({
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in response.tool_calls
                    ],
                })

                for tool_call in response.tool_calls:
                    arguments = parse_tool_call_arguments(tool_call["arguments"])
                    logger.info(
                        f"step={step + 1} | executing {tool_call['name']}"
                        f"({arguments})"
                    )

                    result = execute_tool(
                        tool_name=tool_call["name"],
                        arguments=arguments,
                        repo_root=self.repo_root,
                    )

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(result),
                    })
                # Loop continues — model reasons over the tool results
                # on the next iteration.
                continue

            # Defensive: shouldn't happen given only "stop"/"tool_calls"
            # are produced by our providers, but fail loudly rather than
            # silently looping forever on an unrecognized finish_reason.
            logger.warning(f"Unexpected finish_reason: {response.finish_reason}")
            return response.content or "No answer produced."

        logger.warning(f"Max steps ({config.MAX_AGENT_STEPS}) reached without final answer.")
        return "Max reasoning steps reached without a final answer."


if __name__ == "__main__":
    # Manual smoke test: python -m core.agent
    import os

    test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    agent = Agent(repo_root=test_root)

    print("--- Query: 'What does config.py do?' ---")
    answer = agent.run("What does config.py do? Read the file to find out.")
    print(f"\nFinal answer:\n{answer}")
