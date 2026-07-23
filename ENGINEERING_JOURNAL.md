# Olorin — Engineering Journal

A running build log. Each entry: what got built, why, and the interview-ready
explanation for it. Full architectural rationale lives in `OLORIN_PROJECT.md`;
this file is the day-by-day record of actually building it.

**Continuing from `ENGINEERING_JOURNAL_ARCHIVE_02.md`** (2026-07-12
continued → 2026-07-21 — V2's close-out, all of V3, and V4's
specialist-routing work), split off once that file passed ~2000 lines —
nothing was lost or changed, this is purely a readability split.
**`ENGINEERING_JOURNAL_ARCHIVE_01.md`** covers the earlier 2026-07-06 →
2026-07-12 range. This file picks up from 2026-07-22 onward.

---

## 2026-07-22/23 — Closing specialist routing's real loose ends: persona-known-before-prompt-build, `git_diff`'s honesty note

### Picking up where the last session stopped
2026-07-20/21 left three explicitly-named open items on specialist
routing (`OLORIN_PROJECT.md` Section 11/13): (1) persona voice and
step-budget only applied when the persona was known *before* the first
system prompt got built — plain unaddressed auto-mode traffic never
benefited; (2) `git_diff`'s ref-choice fix (a tightened tool-prompt
description) had already shown mixed compliance live; (3) the Boromir
step-budget ceiling (6) had never actually been stress-tested. Decided
directly with Aryavart to close loose ends before starting any new V4
item (LangGraph, system tray, etc.) rather than let both categories of
unfinished work pile up at once.

### Item 1 — the actual fix, not just a workaround
The real gap: `select_local_specialist(query)` was cheap and pure but
only ever got called *inside* `LLMClient._route()`, at the moment a
local fallback actually happened — after `Agent.run()` had already
built and sent the first system prompt under generic "Olorin" framing.
Fix: `Agent.run()` now calls `select_local_specialist(user_query)` once,
up front, before `messages[0]` is built. The same predicted persona
threads two places — `build_system_prompt()`'s `persona` param (real
`_PERSONA_VOICE`/`_PERSONA_MAX_STEPS` for unaddressed auto-mode too, not
just direct-address/`--provider`) and a new `local_persona_hint` param
threaded through `LLMClient.chat()` → `_route()`, so any later local
fallback in the same run (a forced-cloud failure, or the plain
auto-mode local resolution) uses the *same* persona the prompt already
committed to, instead of `_route()` recomputing
`select_local_specialist()` independently and risking divergence.
`local_persona_hint=None` (the default) preserves exact prior behavior
for any caller that doesn't pass it.

Explicit design note carried into the docstrings: this doesn't reopen
Section 13's rejection of keyword-based *difficulty* classification —
persona selection and complexity scoring are different axes.
Complexity genuinely needs retrieved content (post-retrieval, by
design); persona selection is "if this lands local, which flavor,"
a question the query text alone has always been the legitimate signal
for (this is exactly what `select_local_specialist()` already did when
called late — nothing about the *content* of the decision changed,
only *when* it runs).

### Item 2 — `git_diff`'s ref-choice fragility, a structural second line of defense
The existing fix (tool-prompt description nudging toward the no-ref
default) is soft instruction a model can override, and had already
been shown doing exactly that live in the prior session. Rather than
word the prompt harder for diminishing returns, added
`_uncommitted_changes_note()`: when a supplied `ref` is a two-dot-or-
more range (the shape that structurally excludes the working tree — a
plain single ref like `HEAD~1` still diffs against the current tree
too, per real git semantics, so it needs no caveat), `git_diff()` runs
a cheap `git status --porcelain` check and, if real uncommitted changes
exist, adds a `note` field to its own result naming exactly how many
files aren't reflected in the diff shown. Same "honest partial result
over a silently complete-looking one" posture as `search_codebase`'s
not-indexed message or `web_search`'s unconfigured message — just
triggered here by the model's own tool-call choice rather than by
missing data. Best-effort: a `git status` failure only skips the note,
never breaks the diff result itself.

### Live verification — both items, and a real honest finding along the way

**Item 1, confirmed live.** A real unaddressed `ask "how does the
complexity scorer decide between local and cloud?"` logged
`specialist routing: boromir (code_score=0 reasoning_score=0)` **before**
`step=1 | sending 2 messages` — direct proof the prediction now runs
ahead of the first system-prompt build, not after. (Two earlier
`faramir` log lines in the same run were capsule regeneration from a
routine reindex, unrelated — confirmed by matching their combined
~48s against the logged "2 capsule(s) regenerated (54.8s)" line.) The
query itself then escalated to Groq on complexity (score cleared
`COMPLEXITY_THRESHOLD`), which is the documented-harmless outcome —
cloud providers don't apply persona sampling params, so a persona-
voiced-but-cloud-answered prompt costs nothing.

**Item 2, confirmed live two ways — the second one a genuinely honest
finding, not a clean win.** The standalone smoke test
(`python -m tools.git_diff`, new case added) confirmed the note fires
correctly against this real repo's real state:
```
note PRESENT: 21 file(s) have uncommitted changes NOT included in this
diff, because a specific ref/range ('HEAD~1..HEAD') was requested
instead of the default. Call git_diff with no ref to see the current
uncommitted state.
```
Separately, a real direct-address test
(`ask "Boromir, check what changed in core/agent.py between HEAD~1 and
HEAD"`) had Boromir choose `git_diff({'ref': 'HEAD~1..HEAD'})` — a
two-dot range, so the note *was* present in the tool's raw JSON result
— but his synthesized final answer (a plain insertion/deletion count
summary) never mentioned it at all. **This isn't a bug in the fix; it's
the fix confirming exactly the risk it exists to hedge against.** The
tool is now structurally honest regardless of what any given local
model chooses to narrate — a careful reader of the raw tool result (or
a future, more careful model) has the caveat available — but nothing
guarantees a terse, non-thinking Boromir answer surfaces every field of
a JSON blob in prose. Logged as a real, confirmed-live limitation
rather than swept into "closed."

### Item 3 — still genuinely open
The per-persona step-budget ceiling (`_PERSONA_MAX_STEPS["boromir"] = 6`)
has still never been exercised by a query long enough to approach it —
every live test across both this session and the last resolved in 1-2
steps. Not chased this session since neither of the two fixes above
depended on it; needs a deliberately multi-hop query next time
(e.g. "read file A, then file B, then summarize every place X is used
across both") to actually stress it.

### Status
- [x] Persona-known-before-prompt-build gap: CLOSED, live-verified
  (`specialist routing:` log line now fires before `step=1 | sending`)
- [x] `git_diff` honesty note (`_uncommitted_changes_note()`): built,
  live-verified two ways (smoke test + real tool-call), and its live
  test surfaced a real, honestly-logged limitation (Boromir didn't
  narrate the note even though it was present)
- [ ] Step-budget stress test: still not done, needs a deliberately
  multi-hop query next session
- [x] `OLORIN_PROJECT.md` Sections 11, 13, and 16 updated with the full
  account, including the honest git_diff-narration gap

**Two of specialist routing's three real open loose ends are now
genuinely closed — one of them (`git_diff`'s note) closed in the more
honest sense of "the underlying risk is now confirmed and structurally
mitigated" rather than "the symptom can never occur again."** The
project's own discipline held here: a live test that could have been
read as a clean pass (the note fired!) was instead read carefully
enough to notice the model didn't use it — the same "verify live, read
the actual output, don't just check that it ran" standard this project
has applied since bug #13.

---
