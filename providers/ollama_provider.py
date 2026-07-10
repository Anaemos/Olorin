"""
providers/ollama_provider.py — Local backend implementation (Boromir + Faramir).

ROOT-CAUSE FIX (see ENGINEERING_JOURNAL.md and OLORIN_PROJECT.md Section 13
for the full story): this originally used the `openai` SDK pointed at
Ollama's OpenAI-compatibility endpoint (/v1/chat/completions). That endpoint
is explicitly documented by Ollama as experimental, and is independently
confirmed (multiple integration guides, GitHub issues across several agent
frameworks) to be unreliable specifically for tool calling — models often
emit a syntactically valid tool-call JSON payload into the plain `content`
field instead of the structured `tool_calls` field.

Ollama's NATIVE API (/api/chat, via the official `ollama` Python library)
is documented to handle tool calling correctly and is the recommended path
for anything that depends on it. This file now uses that native client
instead of `openai`.

Note this breaks symmetry with groq_provider.py (which still uses the
`openai` SDK, correctly — Groq's OpenAI compatibility is stable, not
experimental). That asymmetry is fine: BaseProvider's abstraction means
nothing outside this file needs to know or care which underlying client
library is used, only that chat() returns a normalized ProviderResponse.

The JSON-in-content fallback parser is KEPT as defense-in-depth even after
this fix — it costs nothing when unused (only triggers when tool_calls
comes back empty) and protects against any future model/template
combination that still misbehaves, on either endpoint.
"""

import json
import time
import uuid

import ollama as ollama_lib

import config
from core import profiling
from core.logging_config import get_logger
from providers.base import BaseProvider, ProviderError, ProviderResponse

logger = get_logger("ollama_provider")


def _native_host(base_url: str) -> str:
    """
    config.OLLAMA_BASE_URL was historically set to the OpenAI-compat form
    (e.g. "http://localhost:11434/v1"). The native ollama.Client wants just
    the host, no /v1 suffix. Stripping it here means existing .env files
    don't need to change — this fix doesn't require any config edits.
    """
    host = base_url.rstrip("/")
    if host.endswith("/v1"):
        host = host[: -len("/v1")]
    return host


def _try_parse_fallback_tool_call(content: str, available_tool_names: set) -> list | None:
    """
    Defense-in-depth, kept even after switching to the native API. See
    module docstring above and OLORIN_PROJECT.md Section 13 for the full
    story of why this exists. Only ever fires when the real tool_calls
    field comes back empty AND content happens to parse as a tool-call-
    shaped JSON object matching a registered tool name — it is inert
    (never invoked) for any well-behaved response.

    Handles two observed shapes of misbehaving output: raw JSON with no
    wrapper, and JSON wrapped in a markdown code fence (```json ... ```
    or plain ``` ... ```) — the latter confirmed live on Boromir
    (qwen2.5-coder), which sometimes formats its stray tool-call text as
    a fenced code block rather than bare JSON. Stripping the fence is
    the correct fix here (in the provider that owns this quirk), not in
    core/agent.py or tools/registry.py, per the same dependency-inversion
    principle documented in Section 13 for the original transport fix.
    """
    stripped = content.strip()

    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # Drop the opening fence line (``` or ```json) and a trailing
        # fence line, if present, without disturbing the JSON body
        # in between.
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    name = parsed.get("name")
    arguments = parsed.get("arguments")
    if name not in available_tool_names or not isinstance(arguments, dict):
        return None

    return [{
        "id": f"fallback_call_{uuid.uuid4().hex[:8]}",
        "name": name,
        "arguments": json.dumps(arguments),
    }]


def _translate_messages_for_native(messages: list) -> list:
    """
    core/agent.py builds message history in OpenAI-canonical shape (since
    that's what GroqProvider needs verbatim). Ollama's native API expects
    a different shape for the two message types that involve tool calls:

      - assistant messages: tool_calls[].function.arguments must be a
        dict, not a JSON string.
      - tool-result messages: expects a "tool_name" field, not OpenAI's
        "tool_call_id".

    This translates one direction (OpenAI-shape in -> native-shape out)
    to complement the output normalization already done in chat() below
    (native-shape out -> OpenAI-shape in ProviderResponse). Keeping both
    translations here means agent.py never needs to know Ollama's native
    API disagrees with OpenAI's conventions.
    """
    # tool_call_id -> tool name, so tool-result messages (which only carry
    # the id in OpenAI shape) can be translated to native's tool_name.
    id_to_name = {}
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                if tc.get("id"):
                    id_to_name[tc["id"]] = func.get("name")

    translated = []
    for msg in messages:
        role = msg.get("role")

        if role == "assistant" and msg.get("tool_calls"):
            native_tool_calls = []
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                args = func.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                native_tool_calls.append({
                    "function": {"name": func.get("name"), "arguments": args or {}},
                })
            translated.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": native_tool_calls,
            })

        elif role == "tool":
            tool_name = id_to_name.get(msg.get("tool_call_id"), "unknown_tool")
            translated.append({
                "role": "tool",
                "tool_name": tool_name,
                "content": msg.get("content", ""),
            })

        else:
            translated.append(msg)

    return translated


class OllamaProvider(BaseProvider):
    """
    Local provider — wraps a single named Ollama model (boromir or faramir),
    using Ollama's native /api/chat endpoint via the official `ollama`
    Python library (not the OpenAI-compat shim — see module docstring).
    """

    def __init__(self, model_name: str, think: bool | None = None):
        """
        think: explicit thinking-mode control (Ollama's "think" request
        field). None leaves the model's own default behavior in place.

        Added 2026-07-09 when Boromir's base moved from qwen2.5-coder
        (no thinking capability at all) to qwen3:8b, which supports a
        genuine hybrid thinking/non-thinking toggle. Without this,
        Boromir would silently inherit whatever Ollama/Qwen3's default
        is for a model whose entire role (OLORIN_PROJECT.md Section 2:
        "fast responses", task-focused execution) assumes no deliberation
        overhead before a tool call. core/llm_client.py sets this
        explicitly per persona rather than relying on a shared default.
        """
        self.client = ollama_lib.Client(host=_native_host(config.OLLAMA_BASE_URL))
        self.model = model_name
        self.think = think

    def chat(self, messages: list, tools: list | None = None, think_override: bool | None = None) -> ProviderResponse:
        # think_override lets a future caller flip this persona into
        # thinking mode for one specific heavy task without changing its
        # standing default (self.think, set at construction — see
        # __init__ docstring and core/llm_client.py). Falls back to the
        # instance default when not given.
        effective_think = think_override if think_override is not None else self.think

        start = time.monotonic()
        try:
            kwargs = {"model": self.model, "messages": _translate_messages_for_native(messages)}
            if tools:
                kwargs["tools"] = tools
            if effective_think is not None:
                kwargs["think"] = effective_think

            response = self.client.chat(**kwargs)

        except ollama_lib.ResponseError as e:
            raise ProviderError(
                f"Ollama request failed for model '{self.model}': {e}",
                is_rate_limit=False,
            )
        except Exception as e:
            # Covers connection failures (Ollama not running), timeouts,
            # and anything else the native client's HTTP layer might raise.
            raise ProviderError(
                f"Ollama unreachable (is 'ollama serve' running?): {e}",
                is_rate_limit=False,
            )

        latency_ms = int((time.monotonic() - start) * 1000)

        # V1.5 instrumentation (OLORIN_PROJECT.md Section 11): Ollama's
        # native API reports its own load_duration/total_duration in
        # nanoseconds on every response. Using these instead of guessing
        # from wall-clock deltas is what actually lets us split "model
        # cold-loading into VRAM" from "real inference" — the two costs
        # a persistent-daemon decision needs to tell apart (see the
        # 2026-07-06 journal entry: ~41-45s cold vs 2.9-8.4s warm, but
        # that was inferred from call order, not measured directly).
        #
        # Recorded as a true partition (model_load + inference +
        # client_overhead == latency_ms exactly), NOT as four overlapping
        # views of the same call — an earlier version of this recorded
        # total/model_load/inference/wall_clock as four separate entries,
        # which quadruple-counted this call's time in profiling.report()'s
        # sum and produced a nonsensical negative "unaccounted" number on
        # the first live --profile run. client_overhead captures whatever
        # Ollama's own total_duration doesn't (HTTP round-trip, JSON
        # (de)serialization, native-message translation above) — real
        # cost, just not attributable to the model itself.
        def _ns_to_ms(ns):
            return (ns or 0) / 1e6

        _get = (lambda k, d=None: response.get(k, d)) if isinstance(response, dict) else (lambda k, d=None: getattr(response, k, d))
        total_duration_ns = _get("total_duration")
        load_duration_ns = _get("load_duration")
        if total_duration_ns is not None and load_duration_ns is not None:
            total_ms = _ns_to_ms(total_duration_ns)
            load_ms = _ns_to_ms(load_duration_ns)
            inference_ms = total_ms - load_ms
            client_overhead_ms = latency_ms - total_ms
            profiling.record(f"ollama_model_load[{self.model}]", load_ms)
            profiling.record(f"ollama_inference[{self.model}]", inference_ms)
            profiling.record(f"ollama_client_overhead[{self.model}]", client_overhead_ms)
        else:
            # Ollama didn't report durations (older server version?) —
            # fall back to the one number we do have rather than silently
            # recording nothing.
            profiling.record(f"ollama_call_wall_clock[{self.model}]", latency_ms)

        message = response["message"]

        # V1.5 diagnostic (added 2026-07-10): confirm directly whether
        # `think` was actually honored by Ollama/the model, rather than
        # inferring it indirectly from latency. Ollama puts real reasoning
        # text in message.thinking when the model actually thought;
        # empty/absent means it didn't, regardless of what was requested.
        thinking_content = message.get("thinking") if isinstance(message, dict) else getattr(message, "thinking", None)
        logger.info(
            f"model={self.model} think_requested={effective_think} "
            f"thinking_produced={'yes' if thinking_content else 'no'} "
            f"thinking_chars={len(thinking_content) if thinking_content else 0}"
        )

        tool_calls = []
        raw_tool_calls = message.get("tool_calls") if isinstance(message, dict) else message.tool_calls
        if raw_tool_calls:
            for tc in raw_tool_calls:
                func = tc["function"] if isinstance(tc, dict) else tc.function
                name = func["name"] if isinstance(func, dict) else func.name
                arguments = func["arguments"] if isinstance(func, dict) else func.arguments

                # Native API returns arguments as a dict already (unlike
                # OpenAI's spec, which uses a JSON string). Normalize to a
                # string here so parse_tool_call_arguments() in
                # tools/registry.py can json.loads() it uniformly
                # regardless of which provider answered.
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments)

                call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if not call_id:
                    call_id = f"call_{uuid.uuid4().hex[:8]}"

                tool_calls.append({"id": call_id, "name": name, "arguments": arguments})

        content = message.get("content") if isinstance(message, dict) else message.content
        content = content or ""

        if not tool_calls and tools and content:
            available_names = {t["function"]["name"] for t in tools}
            fallback = _try_parse_fallback_tool_call(content, available_names)
            if fallback:
                tool_calls = fallback

        input_tokens = response.get("prompt_eval_count", 0) if isinstance(response, dict) else getattr(response, "prompt_eval_count", 0)
        output_tokens = response.get("eval_count", 0) if isinstance(response, dict) else getattr(response, "eval_count", 0)

        return ProviderResponse(
            content=content,
            finish_reason="tool_calls" if tool_calls else "stop",
            provider="ollama",
            model=self.model,
            latency_ms=latency_ms,
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            tool_calls=tool_calls,
        )

    def is_available(self) -> bool:
        """
        Cheap health check — lists models via the native client rather
        than sending a chat completion. Same reasoning as before: avoids
        risking an unwanted VRAM model-swap just to check availability.
        """
        try:
            self.client.list()
            return True
        except Exception:
            return False


if __name__ == "__main__":
    # Manual smoke test: python -m providers.ollama_provider
    for model_name in (config.BOROMIR_MODEL, config.FARAMIR_MODEL):
        print(f"\n--- Testing {model_name} ---")
        provider = OllamaProvider(model_name)
        print(f"Available: {provider.is_available()}")

        result = provider.chat(
            [
                {
                    "role": "user",
                    "content": f"Say 'hello from {model_name}' and nothing else.",
                }
            ]
        )
        print(f"Response: {result.content}")
        print(
            f"Provider: {result.provider} | Model: {result.model} | Latency: {result.latency_ms}ms"
        )
        print(f"Tokens: {result.input_tokens} in / {result.output_tokens} out")
