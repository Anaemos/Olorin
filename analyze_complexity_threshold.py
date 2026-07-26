"""
analyze_complexity_threshold.py — standalone diagnostic for re-tuning
COMPLEXITY_THRESHOLD (OLORIN_PROJECT.md Section 7/13), 2026-07-24.

Same "standalone diagnostic, no LLM calls, run directly" pattern as
test_faramir_tools.py / test_websearch_backends.py / debug_entity_
extraction.py / benchmark_embeddings.py.

The threshold has correctly sat at its original guessed value (15)
since 2026-07-14, deliberately gated on "enough real traffic to tune
against" rather than being re-guessed blind. This reads every row in
the conversations table across ALL repos (COMPLEXITY_THRESHOLD is one
global config value, not per-repo — see config.py) where complexity_
score is non-null, i.e. rows where the post-retrieval scorer actually
ran. Forced-provider calls (--provider X, --force-local) and cache
hits are excluded on purpose (memory/history.py's log_conversation()
docstring): they never had a real routing decision to analyze, and
including them would silently pollute the distribution with scores
that were computed but never acted on.

Deliberately does NOT auto-recommend a new number. This project's
whole documented style around this exact threshold has been "not
actionable yet, needs real data first" (2026-07-14, then again
2026-07-15) — the honest next step once there IS real data is to look
at it together and decide, not have a script silently pick a value.
"""
from __future__ import annotations

import config
from memory.history import get_recent


def main():
    # get_recent()'s own limit param defaults to 10. No get_all()-style
    # function exists in memory/history.py, and adding one purely for
    # this one-off diagnostic isn't worth a permanent module change —
    # passing an enormous limit gets the same effect.
    rows = get_recent(repo_path=None, limit=1_000_000)
    scored = [r for r in rows if r["complexity_score"] is not None]

    print(f"Total conversations logged (all repos): {len(rows)}")
    print(f"Rows with a real complexity score (auto-mode only): {len(scored)}")
    if not scored:
        print("\nNothing to analyze yet — no auto-mode queries have been logged.")
        return

    scores = sorted(r["complexity_score"] for r in scored)
    n = len(scores)
    mean = sum(scores) / n
    median = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2

    print(f"\nScore stats: min={scores[0]}  max={scores[-1]}  mean={mean:.1f}  median={median}")
    print(f"Current COMPLEXITY_THRESHOLD = {config.COMPLEXITY_THRESHOLD}")

    # Histogram, bucket width 10 — coarse enough to read at a glance,
    # fine enough to see whether there's a natural gap anywhere near the
    # current threshold worth drawing the line at.
    bucket_width = 10
    max_bucket = (scores[-1] // bucket_width + 1) * bucket_width
    print("\nDistribution:")
    b = 0
    while b < max_bucket:
        count = sum(1 for s in scores if b <= s < b + bucket_width)
        marker = "  <-- current threshold in this bucket" if b <= config.COMPLEXITY_THRESHOLD < b + bucket_width else ""
        print(f"  {b:4d}-{b + bucket_width - 1:<4d} | {'#' * count} ({count}){marker}")
        b += bucket_width

    # Backend split, for context on how much of THIS traffic already
    # went cloud vs. local under the current threshold.
    backend_counts: dict[str, int] = {}
    for r in scored:
        backend_counts[r["backend_used"]] = backend_counts.get(r["backend_used"], 0) + 1
    print(f"\nBackend split (of the {len(scored)} auto-mode rows): {backend_counts}")

    # Rows that scored ABOVE the current threshold but were still logged
    # as answered by a local backend — usually means a cloud escalation
    # was attempted and failed over (Section 13's cascade-fallback
    # entries: bug #10, #18, the malformed-tool-call retry, daily-quota
    # exhaustion), NOT evidence the threshold itself picked wrong.
    # Surfaced as context, not folded into the threshold recommendation.
    mismatches = [
        r for r in scored
        if r["complexity_score"] > config.COMPLEXITY_THRESHOLD
        and r["backend_used"] in ("boromir", "faramir")
    ]
    if mismatches:
        print(
            f"\n{len(mismatches)} row(s) scored ABOVE the current threshold but "
            f"were answered by a local backend anyway — likely cloud-escalation "
            f"attempts that failed over rather than evidence of misrouting. "
            f"Worth a manual look only if this is large relative to total traffic."
        )

    # Sensitivity band: how many real scores sit within +/-5 of the
    # current threshold. These are the only queries whose ROUTING would
    # actually flip if the threshold moved a little — a small band means
    # the threshold isn't very sensitive right now (most traffic is
    # clearly-local or clearly-cloud); a large one means small changes
    # would visibly shift a lot of real traffic.
    band = [s for s in scores if abs(s - config.COMPLEXITY_THRESHOLD) <= 5]
    print(
        f"\n{len(band)} of {n} scored queries ({100 * len(band) / n:.0f}%) fall "
        f"within +/-5 of the current threshold ({config.COMPLEXITY_THRESHOLD}) — "
        f"these are the ones actually sensitive to where the line gets drawn."
    )


if __name__ == "__main__":
    main()
