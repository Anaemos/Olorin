"""
server/app.py -- Olorin Server (V5 Phase 2, OLORIN_PROJECT.md Section 11).

A session-scoped, synchronous HTTP wrapper around the existing Agent
(core/agent.py) and _index_repo (cli.py). Exists to remove the
per-invocation Python cold-start tax (~6-10s, measured in V1.5) for a
TUI session sending many messages in one sitting -- a fresh
`python cli.py ask` process pays that tax on every turn; this server
pays it once at startup and then just answers requests.

Deliberately NOT a background daemon: it starts when a frontend launches
it and exits on POST /shutdown or process kill. No auto-start, no
persistent background indexing, no state surviving between unrelated
sessions -- the risk profile that got the V1.5 daemon deferred.

Agent.run() itself is completely unmodified -- POST /ask stays a plain
synchronous request/response. POST /ask/stream (V5 Phase 4) adds real
live visibility into a request via Server-Sent Events: Agent.run() runs
on a background thread while a temporary logging handler forwards its
existing log output (core/logging_config.py's "olorin" logger -- no new
instrumentation needed, this taps what Agent.run() already logs) to the
client as it happens, ending with one final event carrying the real
answer. /ask itself needed zero changes for this to exist alongside it.

Uses Python's stdlib http.server -- no new dependency. Bound to ONE
primary repo, passed at startup (--path), matching how a TUI naturally
already knows which project it's working in. Narrower than cli.py's ask
command on purpose for this first pass: no context-inheritance fallback,
no session_state pointer. Migrating cli.py's ask to call this server
instead of Agent directly is a separate, later step -- not done here, to
keep this addition purely additive against a heavily-verified CLI path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from cli import _index_repo
from core.agent import Agent
from core.llm_client import select_local_specialist


class _State:
    """The one thing this server remembers across requests: which repo(s)
    it's bound to. Set once at startup, read by every request -- not a
    request-handler instance attribute, since http.server instantiates a
    fresh handler per request.
    """
    repo_root: str = ""
    extra_repo_roots: list[str] = []


def _validate_request(payload: dict) -> tuple[str, list[str], str]:
    """Fast, synchronous-only validation: resolves repo_root/extra_repo_
    roots and confirms a query was given and the path exists. Raises
    ValueError on bad input. Deliberately does NOT do indexing or Agent
    construction -- those can be slow, and for /ask/stream specifically,
    they need to happen AFTER response headers are already sent so their
    own latency shows up in the stream instead of silently delaying the
    very first byte (found live: a 60s gap before any event arrived,
    traced to index-on-demand running entirely before headers were sent).
    """
    query = payload.get("query")
    if not query:
        raise ValueError("missing 'query'")

    repo_root = os.path.abspath(payload["path"]) if payload.get("path") else _State.repo_root
    if not os.path.isdir(repo_root):
        raise ValueError(f"not a directory: {repo_root}")

    extra_repo_roots = list(_State.extra_repo_roots)
    if payload.get("repos"):
        for r in payload["repos"].split(","):
            r = r.strip()
            if r:
                extra_repo_roots.append(os.path.abspath(r))

    return repo_root, extra_repo_roots, query


def _resolve_force_provider(payload: dict, query: str) -> str | None:
    if payload.get("force_local"):
        return select_local_specialist(query)
    if payload.get("provider") and payload["provider"] != "auto":
        return payload["provider"]
    return None


def _prepare_agent(payload: dict) -> tuple[Agent, str, str | None]:
    """Used by /ask, where indexing latency is just part of the normal
    synchronous wait -- there's no stream for it to silently precede.
    """
    repo_root, extra_repo_roots, query = _validate_request(payload)

    if not payload.get("skip_index"):
        _index_repo(repo_root, verbose=False)
        for extra in extra_repo_roots:
            _index_repo(extra, verbose=False)

    force_provider = _resolve_force_provider(payload, query)
    agent = Agent(repo_root=repo_root, extra_repo_roots=extra_repo_roots)
    return agent, query, force_provider


def _handle_ask(payload: dict) -> dict:
    try:
        agent, query, force_provider = _prepare_agent(payload)
    except ValueError as e:
        return {"error": str(e)}
    answer = agent.run(query, force_provider=force_provider)
    # Metadata added alongside the answer -- additive, existing consumers
    # (cli.py's _try_server_ask, which only reads "answer"/"error") stay
    # unaffected by unknown extra fields. Real data, read straight off
    # the Agent instance's last_* attributes (core/agent.py) rather than
    # anything invented here.
    return {
        "answer": answer,
        "backend": agent.last_backend_used,
        "provider": agent.last_provider,
        "model": agent.last_model,
        "complexity_score": agent.last_complexity_score,
        "input_tokens": agent.last_input_tokens,
        "output_tokens": agent.last_output_tokens,
    }


# Sentinel object (identity-compared, never equal to any real queued
# item) marking "the background agent thread is done" on the streaming
# queue -- a plain None wouldn't work here since a real log message
# could theoretically be an empty/falsy value.
_DONE = object()


class _QueueLogHandler(logging.Handler):
    """Captures formatted log records onto a queue instead of (or here,
    ALONGSIDE -- core/logging_config.py's existing file/console handlers
    on the "olorin" logger are untouched) printing them. Attached
    temporarily, per streaming request, so /ask/stream can forward each
    real Agent.run() log event to a connected client as it actually
    happens -- Agent.run() itself needs zero changes, this just taps its
    existing logging as a side channel.
    """

    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self._queue = q

    def emit(self, record: logging.LogRecord) -> None:
        self._queue.put(
            {
                "type": "log",
                "level": record.levelname,
                "logger": record.name.removeprefix("olorin."),
                "message": record.getMessage(),
            }
        )


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silenced -- Agent.run()'s own logger (core/logging_config.py)
        # already covers real request logging.
        pass

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # The client disconnected before we could write the response
            # back -- e.g. olorin_tui's user pressed Esc mid-request
            # (V5 Phase 3/4, real responsiveness via tokio::select! means
            # a client CAN now walk away mid-flight, where it couldn't
            # before). Benign: nothing on this side failed, there's just
            # nobody listening anymore. Logged as a clean one-liner
            # instead of letting the default per-request error handler
            # print a full traceback for something this routine.
            print(f"Client disconnected before response could be sent: {e}")

    def _handle_ask_stream(self, payload: dict) -> None:
        try:
            repo_root, extra_repo_roots, query = _validate_request(payload)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        skip_index = bool(payload.get("skip_index"))

        event_queue: "queue.Queue" = queue.Queue()
        log_handler = _QueueLogHandler(event_queue)

        # Attached BEFORE any of the slow work below starts (indexing,
        # capsule regeneration, the agent loop itself) -- attaching to
        # the shared "olorin" root logger means every submodule's
        # existing logger (olorin.agent, olorin.llm_client, ...) is
        # captured automatically via normal logging propagation,
        # including capsule regeneration's own LLM calls, which already
        # log through olorin.llm_client regardless of context. The two
        # handlers already attached in core/logging_config.py (file +
        # console) are untouched, so the server's own terminal keeps
        # showing the same output it always has.
        olorin_logger = logging.getLogger("olorin")
        olorin_logger.addHandler(log_handler)

        result: dict = {}

        def run_agent():
            try:
                # Indexing (and, inside it, capsule regeneration) runs
                # HERE, on the background thread, after headers are
                # already sent -- not before, in _validate_request.
                # Otherwise a slow first-time/changed-repo index is
                # invisible latency before the client ever receives a
                # single byte, exactly the silent-hang problem this
                # whole feature exists to fix.
                if not skip_index:
                    _index_repo(repo_root, verbose=False)
                    for extra in extra_repo_roots:
                        _index_repo(extra, verbose=False)
                force_provider = _resolve_force_provider(payload, query)
                agent = Agent(repo_root=repo_root, extra_repo_roots=extra_repo_roots)
                result["answer"] = agent.run(query, force_provider=force_provider)
                # Same real metadata as /ask's response now carries --
                # read straight off the Agent instance after run()
                # returns (core/agent.py's last_* attributes).
                result["backend"] = agent.last_backend_used
                result["provider"] = agent.last_provider
                result["model"] = agent.last_model
                result["complexity_score"] = agent.last_complexity_score
                result["input_tokens"] = agent.last_input_tokens
                result["output_tokens"] = agent.last_output_tokens
            except Exception as e:
                result["error"] = str(e)
            finally:
                event_queue.put(_DONE)

        # Runs on its own thread so THIS thread stays free to write
        # queued events out to the client as they arrive, rather than
        # only being able to send everything at once after the whole
        # run finishes (which would defeat the entire point of
        # streaming).
        thread = threading.Thread(target=run_agent, daemon=True)
        thread.start()

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            while True:
                item = event_queue.get()
                if item is _DONE:
                    if "answer" in result:
                        final = {
                            "type": "done",
                            "answer": result["answer"],
                            "backend": result.get("backend"),
                            "provider": result.get("provider"),
                            "model": result.get("model"),
                            "complexity_score": result.get("complexity_score"),
                            "input_tokens": result.get("input_tokens"),
                            "output_tokens": result.get("output_tokens"),
                        }
                    else:
                        final = {"type": "done", "error": result.get("error", "unknown error")}
                    self.wfile.write(f"data: {json.dumps(final)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break
                self.wfile.write(f"data: {json.dumps(item)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # Same benign case _send_json already handles for /ask (e.g.
            # olorin_tui's user pressed Esc mid-request) -- the
            # background agent thread just keeps running to completion
            # on its own; its result gets discarded since nobody's
            # listening, the same as an abandoned /ask request today.
            print(f"Client disconnected before stream could finish: {e}")
        finally:
            olorin_logger.removeHandler(log_handler)
            # Best-effort, short timeout -- if the client left early the
            # agent thread could still be running for minutes; this
            # isn't trying to wait that out, just avoiding an obviously
            # already-finished thread lingering as a zombie reference.
            thread.join(timeout=5)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "repo_root": _State.repo_root})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/shutdown":
            self._send_json(200, {"status": "shutting down"})
            threading.Thread(target=self.server.shutdown).start()
            return

        if self.path not in ("/ask", "/ask/stream"):
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        if self.path == "/ask/stream":
            self._handle_ask_stream(payload)
            return

        try:
            result = _handle_ask(payload)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return

        status = 400 if "error" in result else 200
        self._send_json(status, result)


def main():
    parser = argparse.ArgumentParser(
        description="Olorin Server -- session-scoped HTTP wrapper around Agent.run()."
    )
    parser.add_argument("--path", default=".", help="Primary repo root this session is bound to.")
    parser.add_argument("--repos", default=None, help="Comma-separated additional repo roots.")
    parser.add_argument("--port", type=int, default=8756, help="Port to listen on (default: 8756).")
    args = parser.parse_args()

    _State.repo_root = os.path.abspath(args.path)
    if not os.path.isdir(_State.repo_root):
        raise SystemExit(f"Not a directory: {_State.repo_root}")

    _State.extra_repo_roots = [
        os.path.abspath(r.strip()) for r in (args.repos or "").split(",") if r.strip()
    ]

    print(f"Olorin Server -- bound to {_State.repo_root}")
    if _State.extra_repo_roots:
        print(f"Also searching: {', '.join(_State.extra_repo_roots)}")
    print(f"Listening on http://127.0.0.1:{args.port}")
    print('POST /ask {"query": "..."} | POST /ask/stream (SSE) | GET /health | POST /shutdown')

    httpd = HTTPServer(("127.0.0.1", args.port), _Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
