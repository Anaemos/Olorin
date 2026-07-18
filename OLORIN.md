# OLORIN.md

This is Olorin's own project config — Olorin indexing itself, and a live
test fixture for the OLORIN.md loading feature (Section 8 / V2).

Project conventions:
- This is a Python 3.11 project. Local LLMs run via Ollama (native API,
  not the OpenAI-compat shim — see OLORIN_PROJECT.md Section 13 for why).
- Never suggest LangChain or LangGraph for the agent loop in core/agent.py
  — it's deliberately hand-written ReAct, see Section 9.
- The two local personas are Boromir (qwen3:8b, think:false, fast
  task-focused tool execution) and Faramir (deepseek-r1:8b, think:true,
  reasoning/synthesis). Don't conflate their roles.
- SQLite storage (memory.db) is the shared home for the query cache,
  episodic memory, and Knowledge Capsules — don't propose a separate
  database for new memory features without a real reason.
- Provider quirks (Ollama transport, message-schema translation) belong
  entirely inside providers/ollama_provider.py — never patch around them
  in core/agent.py or core/llm_client.py.
