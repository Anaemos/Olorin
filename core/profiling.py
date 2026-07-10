"""
core/profiling.py — lightweight stage-timing utility for the V1.5
cold-start/reload measurement work (OLORIN_PROJECT.md Section 11).

Deliberately NOT a general-purpose profiler or APM integration — just a
context manager that records (label, elapsed_ms) pairs into a shared,
per-process list, plus a formatter to print them. Single source of truth
for "how do we time a stage" so every module that needs to record a span
(cli.py, indexer/embedder.py, indexer/store.py, providers/ollama_provider.py)
does it the same way, and one `ask --profile` run produces one coherent
report instead of five modules printing their own ad-hoc timings.

Spans are always recorded (the overhead of a perf_counter() call is
negligible) — `cli.py`'s `--profile` flag only gates whether the report
gets printed, so instrumentation never has to be "turned on" separately
from the feature that reads it.

TOP-LEVEL vs NESTED (fixed 2026-07-09 after a live run produced a
nonsensical negative "unaccounted" number): most spans are NESTED inside
another span — e.g. embedder_init, chroma_init, query_embedding, and every
Ollama call all happen *inside* cli.py's total_request span, and an
Ollama call's model_load/inference/wall_clock numbers are three
overlapping views of that same one call, not three separate costs. Naively
summing every recorded span therefore double-, triple-, or quadruple-counts
huge chunks of time. Only `agent_import_chain` and `total_request` are
actually sequential/non-overlapping in cli.py's `ask()` — those are the
only two spans marked `top_level=True`, and only top-level spans are
summed for the "accounted for" total. Every other span is still printed
(for the detailed nested breakdown), just excluded from that sum.

HONESTY NOTE on what this can and can't measure: everything here is timed
from *inside* the Python process. True OS-level interpreter startup (the
cost of the OS loading python.exe, dynamic libraries, and running site.py
— all of which happens before the very first line of cli.py executes) is
NOT visible to any in-process timer, by construction. `report()` reports
the gap between an external wall-clock total (passed in by the caller,
measured from the first executable line of cli.py) and the sum of
top-level spans as "unaccounted" — that gap is process/interpreter
overhead plus anything not explicitly instrumented, not a lie dressed up
as a measurement. Getting the *true* interpreter-startup number requires
timing the process from outside (e.g. PowerShell's `Measure-Command`
wrapping the whole `python cli.py ...` invocation) and comparing against
this report's external wall-clock line.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

# Each entry: (label, elapsed_ms, is_top_level)
_spans: list[tuple[str, float, bool]] = []


@contextmanager
def span(label: str, top_level: bool = False):
    """
    Times a block of code and records it under `label`.

    Usage:
        with span("embedder_init"):
            model = SentenceTransformer(...)

    top_level=True marks a span as sequential/non-overlapping with other
    top-level spans — i.e. safe to sum for a wall-clock accounting total.
    Leave False (default) for anything that happens *inside* another
    span; see module docstring. Multiple spans can share a label (e.g.
    several Ollama calls in one multi-step agent run) — stored as a list
    of entries, not a dict, so nothing gets silently overwritten.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        _spans.append((label, elapsed_ms, top_level))


def record(label: str, elapsed_ms: float, top_level: bool = False) -> None:
    """
    For durations measured some other way (e.g. Ollama's own
    server-reported load_duration/eval_duration, which is more accurate
    than wrapping the call in a `span()` since it separates VRAM load
    time from actual inference time at the source, not by guessing from
    wall-clock deltas). See top_level note on span() above — defaults to
    False since every current caller of record() is reporting a nested
    breakdown of a call that's already inside another span.
    """
    _spans.append((label, elapsed_ms, top_level))


def report(external_total_ms: float | None = None) -> str:
    """
    Formats every recorded span, in recording order, marking which are
    top-level, plus a sum of ONLY the top-level ones (see module
    docstring for why nested spans are excluded from that sum). If
    external_total_ms is given (wall clock from the caller's own start
    point, e.g. cli.py's first executable line), also reports the gap
    between that and the top-level sum as "unaccounted".
    """
    if not _spans:
        return "(no spans recorded)"

    lines = ["Stage timing breakdown (nested spans indented, excluded from the summed total):"]
    top_level_total = 0.0
    for label, elapsed_ms, is_top in _spans:
        prefix = "  " if is_top else "    "
        marker = " [top-level]" if is_top else ""
        lines.append(f"{prefix}{label:36s} {elapsed_ms:9.1f} ms{marker}")
        if is_top:
            top_level_total += elapsed_ms
    lines.append(f"  {'-' * 36} {'-' * 9}")
    lines.append(f"  {'sum of TOP-LEVEL spans':36s} {top_level_total:9.1f} ms")

    if external_total_ms is not None:
        unaccounted = external_total_ms - top_level_total
        lines.append(
            f"  {'unaccounted (proc/interp overhead)':36s} {unaccounted:9.1f} ms"
        )
        lines.append(
            f"  {'wall clock (from cli.py first line)':36s} {external_total_ms:9.1f} ms"
        )
        lines.append(
            "\n  Note: true OS-level interpreter startup (before cli.py's "
            "first line even runs) isn't visible from inside the process. "
            "For that number, compare against an external timer, e.g. "
            "PowerShell: Measure-Command { python cli.py ask \"...\" --profile }"
        )

    return "\n".join(lines)


def reset() -> None:
    """Clears all recorded spans. cli.py calls this at the start of a
    `--profile` run so spans from a previous import of this module in the
    same process (shouldn't normally happen — cli.py is one-shot per
    invocation — but matters if this is ever used from a long-running
    process, e.g. the daemon this measurement work might justify)."""
    _spans.clear()
