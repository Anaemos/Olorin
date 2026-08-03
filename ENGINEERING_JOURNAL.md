# Olorin — Engineering Journal

A running build log. Each entry: what got built, why, and the interview-ready
explanation for it. Full architectural rationale lives in `OLORIN_PROJECT.md`;
this file is the day-by-day record of actually building it.

**Archive chain:**
- `ENGINEERING_JOURNAL_ARCHIVE_01.md` — 2026-07-06 → 2026-07-12 (V1 build,
  V1.5 measurement, start of V2)
- `ENGINEERING_JOURNAL_ARCHIVE_02.md` — 2026-07-12 (continued) → 2026-07-21
  (V2's close-out, all of V3, and V4's specialist-routing work)
- `ENGINEERING_JOURNAL_ARCHIVE_03.md` — 2026-07-22 → 2026-07-26 (closing
  specialist routing's loose ends, context inheritance, the LangGraph
  decision, `COMPLEXITY_THRESHOLD` confirmation, `GROQ_MODEL` swap,
  `crawl_page`/searxNcrawl integration, and V5's packaging + TUI +
  Olorin Server planning)

**This file picks up fresh from here** — `OLORIN_PROJECT.md`'s V5 roadmap
(Section 11) is the current plan of record; this journal starts logging
against it as work actually happens. Split off purely for readability,
same as every prior split — nothing lost or changed, just a fresh file
for the next stretch of real build entries.

---

## 2026-07-27 — V5 Phase 0, first two items closed

Picked up Phase 0 (environment hygiene) as the agreed next step from last
session's close. Working via filesystem MCP access to the real repo on
Aryavart's machine this session, not the sandboxed computer-use
container — install/execution commands are being run by Aryavart
directly; this session reads and edits files and reports back.

**`requirements.txt` written.** Rather than a raw `pip freeze` (the real
venv carries ~150+ packages unrelated to Olorin — jupyter, kubernetes,
litellm, fastmcp, keyring, pywin32 — picked up from other work sharing
the same environment), did a real import audit across `cli.py`, `core/`,
`providers/`, `tools/`, `indexer/` to confirm what Olorin actually
imports directly, then cross-referenced each against the exact installed
version in `venv/Lib/site-packages`'s dist-info folders. Direct deps:
`openai`, `ollama`, `httpx` (used directly for `httpx.TimeoutException`
in `providers/ollama_provider.py`, not just transitively via the `ollama`
package), `python-dotenv`, `tree-sitter` + `tree-sitter-language-pack`,
`torch==2.5.1+cu121`, `sentence-transformers`, `chromadb`, `pdfplumber`,
`typer`, `requests`, `crawl4ai`, `playwright`. `searxNcrawl` itself is
deliberately NOT in the file — it's a local editable install from the
sibling `..\searxNcrawl` checkout (`pip install -e ..\searxNcrawl`),
which isn't expressible as a portable pin anyway. Added a
`--extra-index-url` line for torch's CUDA wheel with an explicit
CPU-fallback note (indexer/embedder.py's `_get_device()` already handles
that gracefully). `memory/` and `indexer_core/` needed nothing added —
stdlib + Rust respectively.

**Not yet installed from clean — flagging honestly, not assuming it's
correct just because it reads correctly.** This file was built by
reading installed versions off Aryavart's existing, already-working venv,
the same category of risk this project has hit before (a fix that reads
correctly but hasn't been run against real conditions — bugs #15/#16 in
Section 13). Real next-session action: have Aryavart (or a clean venv)
run `pip install -r requirements.txt` on a fresh environment and confirm
`python config.py` / `python cli.py index .` / `python cli.py ask ...`
all still work, before this gets marked verified rather than just
written.

**Doc staleness fixed, closing a gap flagged 2026-07-25 but not acted on
until now.** `OLORIN_PROJECT.md` Section 2's lore table and Section 12's
tech-stack table both still described Faramir and Boromir as two
separate named Ollama models (`faramir` FROM `deepseek-r1:8b`, `boromir`
FROM `qwen3:8b`) — stale since the 2026-07-20 specialist-routing model
collapse (Section 13), which correctly updated Section 16's environment
reminder at the time but missed these two tables. Both fixed to show
`local:latest` (`FROM qwen3:8b`) with persona differentiation
(`think` flag, sampling params, system-prompt voice) explained inline.
Section 2's original "shared lineage" note (the qwen3-architecture
discovery that actually motivated the later collapse) was kept, not
deleted — relabeled as a pre-collapse historical record rather than a
description of the current setup, so the reasoning trail stays intact
for anyone reading the doc top-to-bottom.

**Both Phase 0 checklist items marked done in `OLORIN_PROJECT.md`'s
Section 11**, with the requirements.txt item honestly caveated as
"written, not yet clean-installed." Remaining Phase 0 item: the prebuilt
`indexer_core` Windows binary on GitHub Releases — needs a real
`cargo build --release` and an actual GitHub Releases upload, both
Aryavart-side actions, not something to fake from here.

**Next up:** confirm the clean-install verification above, then either
finish Phase 0 (indexer_core binary) or move to Phase 1 (README rewrite +
PowerShell setup script), Aryavart's call.

---

## 2026-07-27 (continued) - Phase 1 done, plus two more real staleness bugs caught while writing the setup script

Continued straight into Phase 1 rather than waiting on the clean-install verification (that's a blocking action for Aryavart, not something to sit idle on).

**Found two more stale doc sections while drafting setup.ps1, not just the two already flagged 2026-07-25.** Writing the setup script meant actually working through Section 4's Ollama setup steps to script them faithfully - and Section 4 turned out to still walk through creating two separate named models (boromir FROM qwen3:8b, faramir FROM deepseek-r1:8b), the exact pre-2026-07-20-collapse setup Section 2/12 already got fixed for this morning. Section 3's VRAM/storage budget tables had the same problem, and Storage's total (~13-14GB) was genuinely wrong, not just stale wording - it double-counted a model that's now one file on disk, not two. Lesson: a partial fix (Section 2/12 this morning) doesn't guarantee every other reference got caught in the same pass - it took writing something that has to actually match reality (the script) to force a real check rather than a skim.

**Fixed both.** Section 3: VRAM budget now shows one local:latest entry instead of two, peak-usage number left alone since it was already quietly correct (only one local model was ever loaded into VRAM at a time, even pre-collapse). Storage estimate recalculated honestly: ~8-9GB total, down from ~13-14GB. Section 4: rewritten around the real local:latest setup (pull qwen3:8b, create one Modelfile-based model, think set per-request in code rather than two separate ollama create calls) - kept the original qwen2.5-coder-to-qwen3 rationale comment since that reasoning is still true.

**setup.ps1 written**, now that its source-of-truth section actually matches the real setup. Idempotent by design (checks venv/, ollama list output, .env existence, indexer_core.exe presence before doing anything) - same posture as index-on-demand's hash-skip logic elsewhere in this project. Deliberately does NOT install Ollama itself or build indexer_core automatically - both stay documented prerequisites the script detects and points at. .env prompts skip cleanly on Enter for every key except GROQ_API_KEY, which loops until non-empty since config.py hard-fails without it.

**README.md rewritten.** Same staleness class as the doc fixes above (still said llama-3.3-70b-versatile and separate Boromir/Faramir models in the architecture paragraph) plus the actual Phase 1 asks: a real Prerequisites section, a genuine Quickstart (both setup.ps1 and the manual walkthrough, kept consistent with Section 4's just-corrected steps), and Section 14's recruiter pitch quoted directly. Benchmark table left as-is.

**Both Phase 1 checklist items marked done in OLORIN_PROJECT.md**, with the same honest "written, not yet run on a clean machine" caveat requirements.txt already carries - setup.ps1 hasn't been executed anywhere yet, this session has no way to run PowerShell directly.

**Genuinely open, not yet done:** requirements.txt's clean-install verification (still pending), and Phase 0's remaining item (prebuilt indexer_core binary - needs a real cargo build --release + GitHub Releases upload, both Aryavart-side). Suggested next: run setup.ps1 on this machine (or a clean one) as the single verification pass that closes out both requirements.txt AND setup.ps1's open caveats at once, since setup.ps1 already does the pip install step.

---

## 2026-07-27 (continued again) - requirements.txt corrected: curated list replaced with a real pip freeze

Aryavart ran pip freeze > newreq.txt and pushed back directly on the first requirements.txt: too curated, not verbatim enough. Right call, not just a preference difference - the roadmap item literally says "freeze the actual working environment," and a full freeze reproduces a known-working install exactly, including whatever transitive version constraint made this specific combination work. The curated version's reasoning (~150 unrelated packages from other work sharing this venv would make the file lie about what Olorin needs) was true but optimized for the wrong property - readability instead of reproducibility, when reproducibility is what this file is actually for.

Replaced requirements.txt with the real freeze verbatim, keeping only the two things pip freeze structurally can't capture: the --extra-index-url line for torch's CUDA wheel, and a comment that playwright install chromium still needs a separate manual run.

**Real, non-obvious bonus from making the correction, not just a wash:** the freeze captured searxNcrawl's exact installed state as a git+commit editable reference (pip recording the local editable install's git remote and commit hash via PEP 610's direct_url.json, since the local searxNcrawl checkout is itself a git repo with that remote configured). That's genuinely more reproducible than the first version's own comment ("run pip install -e the local checkout"), which only worked given that exact local path on this exact machine and said nothing about which commit. Neither version of this file was wrong about searxNcrawl not being on PyPI - the freeze just captured more real information about it than a hand-written comment did.

OLORIN_PROJECT.md's Phase 0 checklist entry updated to log the correction honestly, not silently overwritten - same discipline this project applies to every other reversed decision.

---

## 2026-07-27 (continued a third time) - setup.ps1's first real run failed immediately: encoding bug, fixed

Aryavart actually ran setup.ps1 (after renaming the working venv aside as a fallback, the right call for a first real test) and it failed on line 98 with "Missing closing ')' in expression" - a parse error, not a runtime one, meaning the script never even started executing.

Root cause: setup.ps1 used em dashes throughout. Windows PowerShell 5.1 (not PowerShell 7/pwsh) reads a .ps1 file with no byte-order-mark using the system codepage, not UTF-8 - so the em dash's UTF-8 byte sequence decoded as mojibake, which happened to land inside a string literal in a way that broke the parser's bracket/quote matching. This is a real, first-run-blocking bug, not a cosmetic one.

Fix: rewrote setup.ps1 entirely in plain ASCII - every em dash replaced with a plain hyphen, no other non-ASCII characters introduced. Deliberately did not fix this by adding a UTF-8 BOM instead (the more surgical-looking fix) - a BOM'd .ps1 can itself cause friction with some tools/diff viewers/git configurations, and plain ASCII sidesteps the whole encoding question rather than trading one encoding assumption for another. Added a note directly in the script's own .NOTES block explaining why, so a future edit doesn't accidentally reintroduce a smart quote or dash and reopen the same bug.

**Not yet re-verified live** - this fix hasn't been re-run yet. Real next step: Aryavart re-runs .\setup.ps1 against the renamed-aside venv setup, confirms it completes end-to-end this time, then the dummy-repo smoke test as the actual pipeline verification.

---

## 2026-07-27 (continued a fourth time) - two more real bugs from actually running it: comment verbosity, wrong Python version

Aryavart pushed back on comment verbosity across setup.ps1 and requirements.txt - trimmed both to functional comments only.

Re-ran setup.ps1. It got further this time but failed on torch: venv was created with Python 3.14.3, not 3.11.9 - python on PATH resolves to 3.14 on this machine now, different from the original working venv's 3.11. torch==2.5.1+cu121 has no wheel for 3.14 (last supported was 3.13), so pip failed there while roughly 250 other pinned packages installed fine on cp314 wheels.

Fixed: setup.ps1 now checks for Python 3.11 specifically via the Windows py launcher (py -3.11) and uses it for venv creation if found, instead of trusting whatever python happens to resolve to on PATH. Falls back to plain python with an explicit torch-compatibility warning if py -3.11 isn't available.

Not yet re-verified - existing half-built venv (3.14) needs deleting before the next run.

---

## 2026-07-27 (continued a fifth time) - setup.ps1 verified clean end-to-end

Re-ran after deleting the 3.14 venv. py -3.11 resolved correctly, torch installed as cp311+cu121, all ~250 packages installed successfully. local:latest and .env were both correctly detected as already present and skipped rather than re-prompted. One harmless warning (build==1.5.1 is yanked upstream but still installable and still what's pinned).

Phase 0 and Phase 1 are now genuinely closed, not just written - requirements.txt and setup.ps1 both proven against a real clean install. Remaining open items: playwright install chromium (manual, not yet run), the dummy-repo pipeline smoke test (not yet run), and Phase 0's last item (prebuilt indexer_core binary for GitHub Releases).

---

## 2026-07-27 (continued a sixth time) - full pipeline verified, Phase 0 fully closed with the indexer_core release workflow

playwright install chromium ran clean with no download (Chromium was already cached at the user level from the previous venv's crawl_page work, keyed by Playwright version rather than living inside the venv itself - nothing to clean up there).

Dummy-repo pipeline test: indexed a fresh 2-file throwaway repo, ran a real ask against it. Full chain worked - Rust walker, chunking, GPU embedding, ChromaDB, capsule generation, specialist routing to Boromir, a grounded two-sentence answer. One thing noted but deliberately not chased on a throwaway repo: capsule generation only counted 1 of the 2 indexed files, and the final answer only mentioned hello.py, never README.md - possibly capsule generation excluding non-code files by design, possibly a real gap. Logged as something to watch for on a real repo with real docs, not investigated further here.

Real-repo test: ran ask against the actual olorin repo under the new venv (not just the dummy). Full multi-step ReAct loop through Groq (list_files -> read_file -> final answer), entity extraction, and an automatic 14-file reindex (picking up this session's own doc edits) all worked cleanly. Confirmed the new venv is a genuine drop-in replacement, not just capable of trivial cases. venv_old deleted afterward - both venvs proven equivalent, no reason to keep the duplicate several GB around.

indexer_core prebuilt binary (Phase 0's last item): wrote .github/workflows/release-indexer-core.yml - triggers on an indexer_core-v* tag push, builds indexer_core.exe on windows-latest via cargo build --release, computes a SHA256 checksum (reusing the same sha2/hex crates the walker itself already depends on for hash-based reindex-skip), publishes both as release assets via softprops/action-gh-release. Tag scoped to indexer_core-v* rather than a bare v* so it won't collide with any future whole-project version tag.

First real run failed: "Resource not accessible by integration." Root cause: GitHub's default GITHUB_TOKEN is read-only for a workflow unless it explicitly requests write access, and action-gh-release needs contents: write to actually create a release. Fixed by adding an explicit permissions: contents: write block to the workflow. Re-run succeeded: indexer_core-v0.1.0 published with indexer_core.exe and its checksum as real release assets.

Phase 0 and Phase 1 are now both fully closed and verified, not just written - requirements.txt, setup.ps1, the full pipeline (dummy repo and real repo), and the indexer_core release pipeline all confirmed live, including two real bugs (the encoding bug, the Python-version bug) already covered above and this session's third real bug (the Actions permissions error).

---

## 2026-07-27 (continued a seventh time) - Olorin Server built and verified, closing Phase 2

Moved to V5 Phase 2 (the Olorin Server), already scoped in a prior session: session-scoped, synchronous request/response, Agent.run() completely unmodified, streaming deferred until real need. Read cli.py's actual ask() command first to confirm the real Agent(repo_root, extra_repo_roots).run(query, force_provider) interface rather than assuming it, and to see exactly what index-on-demand/context-inheritance/cross-repo logic already exists so the server wraps the real thing rather than a guess.

server/app.py written: stdlib http.server, no new dependency - checked requirements.txt first and confirmed fastapi itself isn't actually installed despite starlette/uvicorn being present as mcp/fastmcp transitive dependencies, so reaching for fastapi would have meant either a wrong assumption or a genuinely new dependency neither was worth it for a three-endpoint wrapper. POST /ask (query, optional path/repos/provider/force_local/skip_index), GET /health, POST /shutdown for a clean session-scoped exit - not a background daemon, matching the design already locked in. Bound to one primary repo passed at startup (--path) rather than reimplementing cli.py ask's context-inheritance/session_state fallback - deliberately narrower than the CLI for this first pass, since a TUI will always know which repo it's launched into.

Verified live immediately: GET /health returned the bound repo root correctly, POST /ask ran a real request end-to-end through the unmodified Agent.run() - index-on-demand, full ReAct loop (Faramir capsule regen, Groq routing, final answer), entity extraction all fired exactly as they do through cli.py ask, confirming the wrapper genuinely adds nothing that changes Agent's behavior. POST /shutdown returned its response and the process exited cleanly afterward.

Second half of Phase 2: migrating cli.py's ask to talk to the same server, so there's exactly one backend entry point into Agent.run(). Real design question worked through before writing code: ask is a one-shot CLI command run constantly for single queries, so requiring a server would regress the exact cold-start cost the server exists to fix. Resolved as: prefer an already-running server if reachable, otherwise fall back to the existing direct path unchanged. _try_server_ask() does a fast localhost health check (connection-refused returns near-instantly when nothing's listening, so the common no-server case pays negligible overhead) and routes through POST /ask if reachable; falls through silently otherwise. Added --no-server to force the direct path explicitly, and made --profile do so implicitly, since profiling measures this process's own cost, not a server sitting in a different one.

Verification caught a real methodology mistake before it caught a code bug: the first "no server running" test was actually a duplicate paste of an old server-verification transcript, which would have falsely confirmed the fallback path without ever really exercising it. Caught by checking the actual content (timestamps matching an earlier message) rather than trusting that the pasted output said what was expected. Re-ran properly - and the very next attempt still went through a leftover server process from the previous test that had never been shut down, again silently passing without proving anything. Caught the second time by checking for the ABSENCE of local log output (Loading weights, step-by-step trace) as the actual signal, not just whether an answer came back. Only after a real POST /shutdown and a confirmed-clean re-run did the no-server case show the full local log trace, genuinely confirming the fallback path works. The server-running case was verified cleanly on the first real attempt, and usefully exercised a real Groq 413 -> Cerebras timeout -> local Boromir fallback chain inside the server process along the way, not just a simple happy path.

Both Phase 2 checklist items are now closed and genuinely verified, not just written - server/app.py's three endpoints all confirmed live, and cli.py's dual-path routing confirmed under both real conditions, with the verification process itself catching two false-positive test attempts before accepting the result.

**Session status: Phases 0, 1, and 2 of V5 are now fully closed.** Requirements.txt, setup.ps1, the full indexing/agent pipeline, the indexer_core GitHub Actions release, and the Olorin Server (plus its CLI integration) are all written, live-verified, and documented in OLORIN_PROJECT.md's Section 11 checklist with real bugs and real fixes logged inline rather than glossed over. Six real issues found and fixed this session: the requirements.txt curation-vs-freeze tradeoff (a design correction, treated with the same rigor as a bug), the setup.ps1 encoding bug, the setup.ps1 Python-version bug, the GitHub Actions permissions bug, and two test-methodology near-misses in the server verification that would have produced false-positive results if not caught. Phase 3 (the Ratatui TUI) is real Rust frontend work and is deliberately being picked up fresh in a new session rather than started here.

---

## 2026-07-29 -- Phase 3 begins: olorin_tui scaffolded, steps 1-4 built and live-verified

Picked up Phase 3 fresh, as planned. Aryavart is a Rust beginner (prior exposure limited to reviewing/understanding `indexer_core/main.rs`, not writing interactive/async Rust), so this phase runs with explanations at each step rather than dumping code -- explicitly requested and honored throughout.

**New crate, `olorin_tui/`, scaffolded as a sibling to `indexer_core/`, not a Cargo workspace** -- they share zero code/deps today, a workspace would add ceremony for no benefit yet. `Cargo.toml`: `ratatui`, `crossterm` (`event-stream` feature from the start, since async input was always the plan), `tokio` (`full`).

**Step 1 (static shell):** ratatui + crossterm terminal setup/teardown (raw mode, alternate screen), a fixed three-box layout, `q` to quit. First real concept taught: immediate-mode rendering (`terminal.draw()` redraws the whole screen from state every frame, no partial-update API) vs. the retained-mode mental model a beginner might expect. Compiled and ran clean first try.

**Step 2 (real input):** an `App` state struct (`input: String`), live keystroke echo, a real terminal cursor via `frame.set_cursor_position`. Quit moved from `q` to `Esc` since `q` needed to be a typable character. **Real bug found immediately on first run: every keystroke produced DOUBLED characters.** Diagnosed correctly as a known Windows-specific crossterm quirk -- Windows reports both a Press and a Release `KeyEvent` per physical keystroke, where Linux/Mac terminals typically only send Press. Fixed by checking `key.kind == KeyEventKind::Press`, ignoring Release.

**Fixed colors requested, live iterated same day.** Aryavart supplied a "Grey Pilgrim" color-scheme reference image with exact hex values (Background `#0F1116`, Surface `#161B22`, Border `#283241` later brightened to `#3A4B63`, Text Primary/Secondary/Muted, six accent colors). Built `mod theme` (a `const fn rgb(hex: u32) -> Color` helper plus named constants) specifically so nothing relies on terminal-dependent named colors (`Color::DarkGray` etc., which is exactly why the UI had looked different across Zed's terminal vs. cmd vs. PowerShell before this). Also locked the cursor shape via `crossterm::cursor::SetCursorStyle` for the same cross-terminal-consistency reason. Explicitly discussed and accepted: font family is NOT controllable from inside a terminal program -- a real, structural limit of "TUI," not a missing feature.

**Step 3 (real networking):** `reqwest` + `serde`/`serde_json` added. `POST /ask` wired up with a `GET /health` fail-fast check before ever entering the TUI screen (clear error on a normal terminal beats a silent hang inside one). Deliberately, honestly blocking for this step -- the whole UI freezes during a request, flagged as a known limitation step 4 exists to fix, not hidden.

**Step 4 (responsiveness via `tokio::select!`):** switched from crossterm's blocking `event::poll`/`event::read` to its async `EventStream`. Raced two branches each loop iteration: a keyboard event vs. the pending request's channel. **Real mistake made and caught here, worth logging precisely:** initially explained `tokio::select!`'s `if <guard>` as skipping evaluation of the branch expression entirely when false -- WRONG. The actual behavior is the expression is always evaluated, only *polling* is skipped when the guard is false. This produced an immediate panic (`called Option::unwrap() on a None value`) the very first run, since `app.pending_rx.as_mut().unwrap()` ran unconditionally every iteration regardless of the guard. Fixed by wrapping the risky logic inside a lazy `async {}` block (code inside only runs when actually polled) with a defensive `None => std::future::pending().await` arm as a second layer, so even a wrong assumption about select!'s semantics couldn't cause a second panic. **That same panic also surfaced a real, until-then-theoretical rough edge:** a panic unwinds straight past the file's normal terminal-teardown code, leaving the real terminal stuck in raw mode/alternate screen (Zed's terminal tab visibly "froze" showing the last TUI frame). Installed a proper panic hook (`std::panic::set_hook`) that best-effort restores the terminal (disable raw mode, leave alternate screen, reset cursor style) BEFORE the panic message prints -- added the moment the rough edge actually bit, not deferred again.

**Live-verified under real adverse conditions, not just the happy path:** a real query that hit Groq's TPM limit, tripped the circuit breaker, and cascaded through Cerebras across 4+ minutes and multiple tool-call steps -- `Esc` still quit the TUI instantly mid-request. This also surfaced a genuinely new, separate, and expected consequence server-side: quitting mid-request now really does abandon the TCP connection, and `server/app.py`'s `_send_json()` had no handling for a client disconnecting mid-write, dumping a full traceback for something completely benign. Fixed with a targeted `except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)` catch, logging one clean line instead.

---

## 2026-07-30 (approximate) -- Steps 5-6: real chat history, mouse support, cursor editing, markdown rendering

**Step 5 (chat history + manual scrollback), requested with real, specific critique:** the existing two-box layout wholesale-overwrote "Response" every turn, and the user's own question briefly visible in the input box vanished the instant Enter was pressed. Replaced with `history: Vec<Turn>` (never overwritten, only appended -- `Role::User`/`Olorin`/`Error`), plus manual scrollback (`Up`/`Down`/`PageUp`/`PageDown`) with a `follow_bottom: bool` that detaches on manual scroll-up and reattaches either by scrolling all the way back down or by sending a new query (a clear "I want to see this" signal). Own word-wrap function written (`wrap_line`, later superseded by `markdown.rs`'s wrapping) rather than trusting an uncertain ratatui API for wrapped-line counts.

**Mouse support added same stretch, deliberately scoped:** scroll wheel (mapped onto the exact same scroll logic as Up/Down) and click-to-position-cursor in the input box. Explicitly explained and accepted tradeoff: enabling `EnableMouseCapture` means the terminal's native click-drag-to-select-and-copy stops working (most terminals let Shift+drag bypass this, a convention outside this program's control). Full text selection/copy was explicitly NOT built -- a separate, bigger feature than "basic mouse support."

**Real cursor movement added** (`Left`/`Right`/`Home`/`End`) -- until this point the input box only supported append-at-end and delete-at-end, a real, flagged gap. Implemented with a `cursor: usize` CHARACTER index (not byte index, since Rust strings are byte-indexed but UTF-8) plus a `char_byte_index()` helper converting between the two for insert/delete operations.

**Step 6 (markdown rendering), triggered by a real, visible problem:** responses were showing literal `**bold**` asterisks and raw `| pipe | tables |` with no column alignment. Built `olorin_tui/src/markdown.rs` as a genuinely separate file/module (self-contained text-parsing logic, no UI/async concerns) -- Aryavart's first multi-file Rust project structure, explained (`mod markdown;` + a private `mod theme` inside `main.rs` still reachable via `crate::theme::...` since privacy is scoped to defining-module-and-descendants, which includes the whole crate when the defining module is the root). Deliberately non-CommonMark scope, agreed directly beforehand: headers, bold/italic/bold-italic, inline code, bullet lists, tables -- explicitly NOT fenced-code syntax highlighting or diagrams.

Real design decisions made along the way: (1) tables sized by `min(natural content width, available/num_columns)`, GUARANTEEING no row can ever exceed the box width (the "elongated weird table" bug's actual fix), truncating with a plain ASCII `.` rather than a Unicode ellipsis, consistent with the plain-ASCII-for-generated-chrome lesson already learned from setup.ps1's em-dash encoding bug; (2) word-wrapping a sequence of STYLED spans (not just plain text) required tokenizing fragments into "words" that can span multiple styles (rare, e.g. `**bold**text` glued with no space) before wrapping at word boundaries in the actual rendered text, not the markdown syntax.

**Two real follow-up bugs found via real screenshots, both fixed same session:** (1) table CELLS weren't being inline-parsed at all -- `**Boromir**` inside a table cell showed literal asterisks even though the exact same markup outside a table rendered correctly, since `render_table()` only ever truncated/padded raw cell text. Fixed with a `styled_cell_spans()` helper that runs `parse_inline()` per cell before truncating at the SPAN level. (2) A response containing a literal markdown SOURCE EXAMPLE (the LLM showing "here's what `**bold**` syntax looks like" inside a fenced code block) got its example syntax partially, incorrectly parsed as real formatting -- root cause: no fenced-code-block detection existed, so content meant to be shown literally was being run through the real parser. Fixed by detecting ` ``` ` fences and rendering their contents as plain, unparsed, unstyled text (still word-wrapped via a new `wrap_plain()`, just with zero markdown interpretation) -- explicitly NOT syntax highlighting, just correct literal passthrough. A related, separate bug found in the same review: `**_bold italic_**`-style NESTED emphasis showed literal stray underscores, since the simple linear-scan parser doesn't recursively re-parse a span's own contents -- added `***text***` (triple-asterisk) as its own directly-recognized `InlineStyle::BoldItalic` pattern rather than building real recursion, a cheap, common-case fix, not full nesting support.

**A third, more subtle bug found from a real response mentioning `finish_reason` in plain prose (not backticked):** the word lost its underscore, rendering as `finishreason`. Root cause: CommonMark's real rule that underscore-emphasis must NOT fire inside a word (specifically so `snake_case_identifiers` don't get mangled) was never implemented -- the parser treated any `_..._` pair as italic regardless of word-boundary context, unlike asterisk-emphasis which has no such restriction. Fixed by requiring a non-alphanumeric character (or line start/end) on both sides of an underscore pair specifically, leaving asterisk/backtick markers untouched (matching real CommonMark behavior, not an invented rule).

---

## 2026-08-01 (approximate) -- Step 7: real multi-panel layout matching the mockup, plus iterative feedback fixes

Aryavart supplied a full "Grey Pilgrim" layout mockup (header, Context/Tools/System Status/Indicators left sidebar, Chat panel, Activity/Context Window/Current Model right sidebar). Rebuilt the shell around it as Step 7. Deliberate honesty principle set up front: only show fields with REAL data (repo path, server URL, static tool list) -- fields the mockup shows that require data not yet available (Model, Complexity, live Activity) get an explicit, honest placeholder rather than a fabricated number.

**First pass shipped with several real, specific gaps, caught by direct screenshot review and fixed same session:**
- Context panel's box height (`Length(6)`) was one row short for its own 5 lines of content (5 + 2 borders = 7 needed) -- a real off-by-one, not a design choice, silently clipping the Server value. Fixed by matching box height to content exactly.
- Input box had NO wrapping and NO horizontal scroll -- typed text past the visible width had nowhere to render and silently vanished from view while still being sent correctly underneath. Fixed properly, not just enlarged: character-based (not word-based) hard-wrap specifically because it makes cursor (row, col) math trivial and exact (`row = cursor / width`, `col = cursor % width`), plus auto-scroll to keep the cursor's row always visible.
- Tools panel had no height bound, risking pushing other boxes around as the list grows -- fixed with a fixed height + truncation-safe "+N more" fallback (not yet interactively scrollable -- no key/mouse binding assigned to it, a real, named, deferred gap).
- System Status and Indicators boxes were entirely missing from the first pass (mockup has them) -- added; Indicators specifically needed zero new data, since it's just a legend explaining the existing palette's colors.
- Border color contrast was noticeably lower than the mockup's -- brightened `#283241` to `#3A4B63`, a named, checkable change (with an honest caveat: some remaining mockup-vs-terminal visual gap is structural -- font rendering/anti-aliasing differences a design tool and a real terminal can't fully reconcile via hex-matching alone).

**Input scrollback added as a distinct follow-up request:** the auto-scroll-to-cursor logic ran unconditionally every frame, meaning any attempt to manually scroll UP to review earlier wrapped input would be immediately undone by the very next redraw before ever being visible. Fixed by giving the input box the identical `follow_bottom`-style detach/reattach pattern the chat panel already had (`input_follow_cursor: bool`), bound to `Ctrl+Up`/`Ctrl+Down` specifically since plain `Up`/`Down` were already the chat panel's keys -- re-engages automatically on any real edit action (typing, cursor movement), matching the chat panel's exact philosophy.

---

## 2026-08-02 (approximate) -- Step 8: real live Activity streaming, server + Rust both

**Server-side first, verified independently before any Rust consumed it** -- same "prove the foundation before building on it" discipline as the rest of this project. New `POST /ask/stream` (SSE), added ALONGSIDE the existing `/ask` (untouched, still what `cli.py` uses) rather than replacing it. `_QueueLogHandler` (a `logging.Handler` subclass) attached temporarily to the shared `"olorin"` root logger per streaming request, pushing every captured record onto a thread-safe queue; `Agent.run()` (plus, critically, index-on-demand/capsule regeneration) runs on a background thread so the main request-handling thread stays free to write queued events out as SSE frames the instant they arrive. `core/logging_config.py`'s existing file/console handlers untouched -- Aryavart's explicit choice to keep both (server terminal output unchanged, streaming is additive).

**A standalone Python test script (`test_stream_endpoint.py`) written specifically to verify streaming actually streams before writing a line of Rust** -- and it immediately caught a real, precisely-diagnosed bug: a 60-SECOND gap before the first event ever arrived. Traced exactly: `_prepare_agent()` (including index-on-demand) ran entirely BEFORE response headers were sent or the log handler attached, so any slow indexing/capsule-regen work happened invisibly. Fixed by splitting fast synchronous VALIDATION (query present, path exists) from the slow WORK (indexing, Agent construction, `agent.run()`), sending headers immediately after validation, then doing all the slow work inside the background thread with the log handler already attached.

**Re-tested, and a second, more precise timing question emerged:** events now arrived progressively, but multiple events logged by the server at genuinely different times still arrived bunched at the same client timestamp. Correctly diagnosed as a CLIENT-side artifact, not a server bug -- confirmed by changing `test_stream_endpoint.py`'s `requests.iter_lines()` to `chunk_size=1`, after which every event's arrival gap matched its real `latency_ms` almost exactly. A genuine empirical lesson, not a guess: streaming client libraries buffer internally by default even when reading a genuinely incremental server response.

**One more real gap closed the same day: individual slow LLM calls still showed as one long silent stretch**, since the only log line for a call fired on completion, never on start. Added `"requesting from {provider}..."` log lines at the actual START of each provider call in `core/llm_client.py` (five call sites, including `_call_local()` which covers every local-persona dispatch path in one place). Re-verified: every remaining gap in the stream now matches its real logged latency almost to the millisecond, confirmed against real Groq/Cerebras/Ollama calls side-by-side.

**Rust side built once the server side was independently proven.** `pending_rx` changed from a `oneshot::Receiver<Result<String,String>>` (one result, once) to an `mpsc::UnboundedReceiver<StreamEvent>` (a genuine stream of events over time) -- a real new async concept for this project, since every prior use of channels here was one-shot. `ask_server_stream()` reads `reqwest::Response::bytes_stream()` incrementally, buffers partial SSE frames across TCP chunk boundaries (frames can legitimately split mid-frame across reads), and parses complete `data: {...}\n\n` frames into `StreamEvent::Log`/`StreamEvent::Done`. The `select!` branch handling this was written to explicitly handle all three real outcomes (`Some(Log)`, `Some(Done)`, and `None` for an unexpectedly-closed channel) rather than pattern-matching only the success case -- a channel closing without ever sending Done (background task panic, dropped connection) would otherwise have caused an infinite busy-loop, not just a missed update, since a closed `mpsc` channel's `recv()` resolves to `None` immediately and repeatedly.

**Live-verified end to end:** a real query showed live Activity events -- `requesting from groq...`, `step=1 | executing search_codebase(...)`, entity-extraction lines -- streaming into the right sidebar in real time as the request actually progressed, not appearing all at once when it finished.

**Same-day follow-up: richer response metadata wired into the newly-real panels.** `Agent.run()` already computed backend/provider/model/complexity-score/token-counts internally for its own logging and entity-extraction -- it just never exposed them to callers. Added `Agent.last_backend_used`/`last_provider`/`last_model`/`last_complexity_score`/`last_input_tokens`/`last_output_tokens` as plain instance attributes, set at each of `run()`'s three return points via a small `_record_run_metadata()` helper -- deliberately additive (no return-type change), so `cli.py`'s `ask` command and the existing `/ask` response both needed zero changes and carry zero risk from this. Both `/ask`'s JSON response and `/ask/stream`'s final `done` event now include these fields. Rust-side: the stream's `Done` variant grew from `Result<String, String>` to `Result<RunMeta, String>` carrying the full metadata; wired into real rendering across four panels -- Context gained Model/Complexity fields, System Status shows real input/output/total token counts, and two new right-sidebar boxes (Context Window, Current Model) were added, all showing `--` honestly until the first real request completes rather than a fabricated placeholder number.

**Also discussed, not acted on this session:** `build.nvidia.com`'s NIM API catalog, researched live (current free-tier terms confirmed via web search, not assumed) as a real, well-suited fourth cloud-provider candidate -- its request-count-based rate limit (not a tight per-request token cap) directly targets this project's actual recurring pain (`Request too large... Limit 8000` from Groq, hit repeatedly and directly this very session). Logged as a real, scoped next Python-side task (`providers/nim_provider.py`), not started here.

**Session-ending state:** both `OLORIN_PROJECT.md` (Section 11's Phase 3 checklist, Section 16's wrap-up) and this journal updated to reflect real, live-verified progress -- Phase 3 substantially built, Phase 4 (tray/hotkey/watcher/briefing) still ahead. `README.md`/Section 14's recruiter pitch do NOT yet mention the TUI -- a real, flagged next-session item.

---
