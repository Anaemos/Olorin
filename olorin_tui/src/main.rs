//! olorin_tui -- Step 8: live activity streaming.
//!
//! Step 7 built the real multi-panel shell, but Activity was a static
//! placeholder. This step wires it to server/app.py's POST /ask/stream
//! (Server-Sent Events, already built and verified server-side) --
//! genuinely new shape of concept for this program: instead of one
//! result coming back once (a `oneshot`), we now get a STREAM of events
//! over time, so `pending_rx` becomes a `tokio::sync::mpsc` channel
//! instead. Each parsed SSE frame becomes either a log event (pushed
//! into app.activity for the Activity panel) or the final "done" event
//! (which resolves the turn, exactly like the old oneshot did).

mod markdown;

use std::io;

use crossterm::{
    cursor::SetCursorStyle,
    event::{
        DisableMouseCapture, EnableMouseCapture, Event, EventStream, KeyCode, KeyEventKind,
        KeyModifiers, MouseButton, MouseEventKind,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use futures_util::StreamExt;
use ratatui::backend::CrosstermBackend;
use ratatui::{
    layout::{Alignment, Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, Borders, Paragraph, Wrap},
    Terminal,
};
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

// The request shape matching server/app.py's contract: {"query": ...}.
// Shared by both /ask/stream's request body construction.
#[derive(Serialize)]
struct AskRequest<'a> {
    query: &'a str,
}

// One captured server-side log record, as sent by a "type": "log" SSE
// event -- see server/app.py's _QueueLogHandler for where this comes
// from. This is real backend activity, not synthesized -- the same log
// lines that print in the server's own terminal.
struct ActivityEvent {
    level: String,
    message: String,
}

// Real metadata about the run that just finished -- backend/provider/
// model/complexity/tokens, all read straight off core/agent.py's
// Agent.last_* attributes server-side, none of it invented client-side.
// Every field is optional because the server itself can't always supply
// all of them (e.g. complexity_score is None for a forced-provider call
// that never went through the scorer) -- the UI shows "--" for whichever
// ones are absent rather than a fake number.
struct RunMeta {
    answer: String,
    backend: Option<String>,
    provider: Option<String>,
    model: Option<String>,
    complexity_score: Option<i64>,
    input_tokens: Option<i64>,
    output_tokens: Option<i64>,
}

// What a background streaming task hands back to the main loop, one item
// at a time, over the mpsc channel. Log arrives zero or more times;
// exactly one Done always arrives last (or the channel just closes on a
// genuine connection failure, handled separately as a None case).
enum StreamEvent {
    Log(ActivityEvent),
    Done(Result<RunMeta, String>),
}

// The raw SSE payload shape -- a union of both "type": "log" and
// "type": "done" fields, since serde needs one struct that can
// deserialize either shape (server/app.py sends one or the other, never
// both, per event). `r#type` isn't valid Rust, hence the rename.
#[derive(Deserialize)]
struct RawStreamEvent {
    #[serde(rename = "type")]
    event_type: String,
    #[serde(default)]
    level: Option<String>,
    #[serde(default)]
    message: Option<String>,
    #[serde(default)]
    answer: Option<String>,
    #[serde(default)]
    error: Option<String>,
    #[serde(default)]
    backend: Option<String>,
    #[serde(default)]
    provider: Option<String>,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    complexity_score: Option<i64>,
    #[serde(default)]
    input_tokens: Option<i64>,
    #[serde(default)]
    output_tokens: Option<i64>,
}

// "Grey Pilgrim" palette, fixed hex values -- deliberately NOT terminal-
// theme-dependent named colors (Color::DarkGray etc. map through whatever
// palette the host terminal defines, which is exactly what looked
// inconsistent across Zed/cmd/PowerShell). Color::Rgb is a specific,
// literal color everywhere that supports truecolor (virtually all modern
// terminals do).
mod theme {
    #![allow(dead_code)]

    use ratatui::style::Color;

    const fn rgb(hex: u32) -> Color {
        Color::Rgb(
            ((hex >> 16) & 0xFF) as u8,
            ((hex >> 8) & 0xFF) as u8,
            (hex & 0xFF) as u8,
        )
    }

    // ── Foundation ─────────────────────────────────────
    pub const BACKGROUND: Color = rgb(0x101218); // deep blue-charcoal
    pub const SURFACE: Color = rgb(0x22232D); // visible slate panel
    pub const SURFACE_ALT: Color = rgb(0x2B2D39); // activity rows / selection

    // ── Structure: Gandalf's silver-grey ───────────────
    pub const BORDER: Color = rgb(0x9A9EB2); // clearly visible silver
    pub const BORDER_DIM: Color = rgb(0x5E6378);

    // ── Typography ─────────────────────────────────────
    pub const TEXT_PRIMARY: Color = rgb(0xEEEAE2); // warm white
    pub const TEXT_SECONDARY: Color = rgb(0xC5C7D0);
    pub const TEXT_MUTED: Color = rgb(0x85899B);
    pub const WHITE_PURE: Color = rgb(0xF7F4ED);

    // ── Olorin ─────────────────────────────────────────
    pub const ACCENT_HAT_BLUE: Color = rgb(0x7699E6); // storm blue
    pub const ACCENT_SEA_BLUE: Color = rgb(0x5ED5D1); // Maia / Olorin
    pub const INFO_SKY_BLUE: Color = rgb(0x82B8F5);

    // ── Gandalf's warmer secondary tones ───────────────
    pub const STAFF_BROWN: Color = rgb(0xB29372);
    pub const BEIGE_SOFT: Color = rgb(0xE3D6C2);
    pub const BEIGE_DIM: Color = rgb(0xBDAF9C);

    // ── Semantic only ──────────────────────────────────
    pub const SUCCESS_GREEN: Color = rgb(0x8FD17F);
    pub const WARNING_AMBER: Color = rgb(0xE0B65B);
    pub const ERROR_CRIMSON: Color = rgb(0xD56776);
}

// The full tool registry (tools/registry.py), 9 tools as of V3.
// Hardcoded rather than fetched -- this list is stable, known-at-
// compile-time data, not something that changes per-request. If the
// server's real tool registry ever changes, this needs a matching
// update -- a known, accepted coupling, not an oversight.
const TOOL_NAMES: &[&str] = &[
    "search_codebase",
    "read_file",
    "list_files",
    "search_history",
    "search_entities",
    "web_search",
    "file_importance",
    "git_diff",
    "crawl_page",
];

// A single entry in the transcript. Never mutated once pushed -- history
// only ever grows, matching how a real terminal works (you can't go back
// and edit what already printed).
enum Role {
    User,
    Olorin,
    Error,
}

struct Turn {
    role: Role,
    text: String,
}

// Rust strings are indexed by byte, not by character -- for ASCII text
// these are the same thing, but the moment a multi-byte character shows
// up, a raw char count no longer equals a valid byte offset. This finds
// the byte offset of the Nth character, used to insert/delete at the
// cursor's logical (character) position rather than corrupting the
// string on non-ASCII input.
fn char_byte_index(s: &str, char_idx: usize) -> usize {
    s.char_indices()
        .nth(char_idx)
        .map(|(b, _)| b)
        .unwrap_or(s.len())
}

// All the state our UI needs to render itself. Step 1 had none -- the
// screen was static. From here on, `draw()` (below) is purely a function
// of this struct: give it the same `App`, get the same screen, every time.
struct App {
    input: String,
    // Character index (not byte index) into `input` -- tracked
    // separately since Rust strings are UTF-8 and indexed by byte, but
    // we want cursor movement to count actual typed characters.
    cursor: usize,
    history: Vec<Turn>,
    // Some(...) exactly while a request is in flight -- this doubles as
    // both "is something loading" state (checked in the draw closure)
    // and the actual channel we're waiting on, rather than keeping a
    // separate bool that could drift out of sync with reality. An
    // mpsc receiver now, not a oneshot -- a stream of events over time
    // (log lines as they happen, then one final Done), not a single
    // result delivered once.
    pending_rx: Option<mpsc::UnboundedReceiver<StreamEvent>>,
    // Real, live backend activity for the current (or most recent)
    // request -- each entry is one actual log record captured
    // server-side, not synthesized. Capped (see the draw closure) so a
    // very long session can't grow this unboundedly.
    activity: Vec<ActivityEvent>,
    // How many lines down from the top of the transcript we're showing.
    scroll_offset: u16,
    // true = stay pinned to the newest content (normal terminal
    // behavior). Set false the moment the user manually scrolls up;
    // reset to true either by scrolling all the way back down, or by
    // sending a new query (a clear signal they want to see it).
    follow_bottom: bool,
    // The input box's on-screen position, updated every frame in the
    // draw closure. Needed outside the closure, in the click-handling
    // code, to translate a mouse click's screen column back into a
    // character index within `input`.
    input_rect: Rect,
    // How many WRAPPED ROWS of the input box we're scrolled down --
    // separate from `cursor` (a flat character index into `input`,
    // unchanged by wrapping). Auto-managed each frame to keep the
    // cursor's row always visible, the same "keep cursor in view"
    // behavior any real multi-line text box has.
    input_scroll: u16,
    // true = the box above does that auto-follow. Set false by
    // Ctrl+Up/Ctrl+Down (manually reviewing earlier wrapped lines
    // without losing your place); reset to true by any actual editing
    // action (typing, moving the cursor) -- mirrors the chat panel's
    // follow_bottom pattern exactly, same reasoning: don't yank the
    // view around while someone's deliberately looking at something
    // else, but resume the sensible default the moment they're doing
    // something that needs it.
    input_follow_cursor: bool,
    // Fetched once from GET /health at startup. Real data, not a
    // placeholder -- deliberately the ONLY thing shown in the Context
    // panel for now; fields like model/complexity/tokens stay out until
    // step 5 wires them into a real response, rather than showing fake
    // numbers that look real.
    repo_root: String,
    // Real metadata from the most recently COMPLETED run -- None until
    // the first request finishes. Populated from the stream's final
    // "done" event (see RunMeta); feeds Context/System Status/Current
    // Model panels. Deliberately kept even after a NEW request starts
    // (only overwritten once the new one actually completes), so the
    // panels don't flash back to empty placeholders while waiting --
    // showing the last known real state is more honest than showing
    // nothing during a request that hasn't resolved yet.
    last_backend: Option<String>,
    last_provider: Option<String>,
    last_model: Option<String>,
    last_complexity_score: Option<i64>,
    last_input_tokens: Option<i64>,
    last_output_tokens: Option<i64>,
}

impl App {
    fn new(repo_root: String) -> Self {
        App {
            input: String::new(),
            cursor: 0,
            history: Vec::new(),
            pending_rx: None,
            activity: Vec::new(),
            scroll_offset: 0,
            follow_bottom: true,
            input_rect: Rect::default(),
            input_scroll: 0,
            input_follow_cursor: true,
            repo_root,
            last_backend: None,
            last_provider: None,
            last_model: None,
            last_complexity_score: None,
            last_input_tokens: None,
            last_output_tokens: None,
        }
    }
}

// The real streaming call: POSTs to /ask/stream, reads the response
// body incrementally as bytes arrive (reqwest's `bytes_stream()`, hence
// the "stream" Cargo feature), and parses out SSE frames as they
// complete -- pushing each one through `tx` immediately rather than
// waiting for the whole response. This is the Rust-side mirror of the
// Python empirical lesson from earlier: don't assume a streaming API
// reads incrementally just because it's *called* streaming -- verify it,
// the same way the Python `requests` client turned out to need a smaller
// chunk_size. reqwest's `bytes_stream()` genuinely does yield chunks as
// the underlying TCP data arrives, which is what makes this work at all.
async fn ask_server_stream(
    client: reqwest::Client,
    base_url: String,
    query: String,
    tx: mpsc::UnboundedSender<StreamEvent>,
) {
    let url = format!("{base_url}/ask/stream");
    let body = AskRequest { query: &query };

    let response = match client.post(&url).json(&body).send().await {
        Ok(r) => r,
        Err(e) => {
            let _ = tx.send(StreamEvent::Done(Err(format!(
                "Could not reach Olorin Server at {base_url}: {e}"
            ))));
            return;
        }
    };

    let mut stream = response.bytes_stream();
    let mut buffer = String::new();

    while let Some(chunk) = stream.next().await {
        let chunk = match chunk {
            Ok(c) => c,
            Err(e) => {
                let _ = tx.send(StreamEvent::Done(Err(format!("Stream error: {e}"))));
                return;
            }
        };
        buffer.push_str(&String::from_utf8_lossy(&chunk));

        // SSE frames are separated by a blank line ("\n\n"). A frame can
        // arrive split across multiple TCP chunks -- that's exactly why
        // we accumulate into `buffer` rather than parsing each chunk in
        // isolation, and only consume complete frames as they appear.
        while let Some(pos) = buffer.find("\n\n") {
            let frame = buffer[..pos].to_string();
            buffer.drain(..pos + 2);

            for line in frame.lines() {
                let Some(data) = line.strip_prefix("data: ") else {
                    continue;
                };
                let Ok(parsed) = serde_json::from_str::<RawStreamEvent>(data) else {
                    continue;
                };

                match parsed.event_type.as_str() {
                    "log" => {
                        let _ = tx.send(StreamEvent::Log(ActivityEvent {
                            level: parsed.level.unwrap_or_default(),
                            message: parsed.message.unwrap_or_default(),
                        }));
                    }
                    "done" => {
                        let outcome = match (parsed.answer, parsed.error) {
                            (Some(answer), _) => Ok(RunMeta {
                                answer,
                                backend: parsed.backend,
                                provider: parsed.provider,
                                model: parsed.model,
                                complexity_score: parsed.complexity_score,
                                input_tokens: parsed.input_tokens,
                                output_tokens: parsed.output_tokens,
                            }),
                            (None, Some(err)) => Err(format!("Server error: {err}")),
                            (None, None) => Err("Server sent an empty done event.".to_string()),
                        };
                        let _ = tx.send(StreamEvent::Done(outcome));
                        return;
                    }
                    _ => {}
                }
            }
        }
    }

    // The byte stream ended without ever sending a "done" event --
    // the server-side connection closed unexpectedly (crash, network
    // drop). Report it as an error rather than leaving the UI waiting
    // forever with no explanation.
    let _ = tx.send(StreamEvent::Done(Err(
        "Server closed the connection before finishing.".to_string(),
    )));
}

// A quick, one-shot GET /health check -- run once at startup, before we
// ever enter the alternate screen. Failing fast here with a plain,
// readable error (while we're still a normal terminal program) is much
// better than the alternative: entering the TUI, having the user type a
// question, and only then discovering there's nothing listening. Also
// doubles as how we learn the real repo_root the server is bound to --
// one call, reused for both purposes rather than a second round-trip.
#[derive(Deserialize)]
struct HealthResponse {
    repo_root: String,
}

async fn check_server_health(client: &reqwest::Client, base_url: &str) -> Result<String, String> {
    let url = format!("{base_url}/health");
    let response = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("Could not reach Olorin Server at {base_url}: {e}"))?
        .error_for_status()
        .map_err(|e| format!("Olorin Server at {base_url} responded with an error: {e}"))?;

    let parsed: HealthResponse = response.json().await.map_err(|e| {
        format!("Olorin Server at {base_url} sent an unreadable /health response: {e}")
    })?;
    Ok(parsed.repo_root)
}

// A real bug during step 4's development panicked mid-run and left a real
// terminal stuck in raw mode / on the alternate screen, since a panic
// unwinds straight past this file's normal teardown code at the bottom of
// main(). This installs a panic hook that restores the terminal FIRST
// (best-effort -- we're already panicking, so errors here are ignored
// rather than causing a second panic), then hands off to Rust's normal
// panic printing. Doesn't prevent bugs; just stops one bug from also
// corrupting the user's terminal session on top of crashing the program.
fn install_panic_hook() {
    let original_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |panic_info| {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            DisableMouseCapture,
            LeaveAlternateScreen,
            SetCursorStyle::DefaultUserShape
        );
        original_hook(panic_info);
    }));
}

// `#[tokio::main]` expands `main` into a small wrapper that starts a tokio
// runtime and then runs this function's body inside it -- necessary from
// step 1 onward so this step's real `.await` calls (the HTTP requests)
// have a runtime to run on without any restructuring.
#[tokio::main]
async fn main() -> io::Result<()> {
    // Minimal manual flag parsing -- just one optional flag, not worth a
    // crate dependency for. `--server-url http://host:port` overrides the
    // default; anything else on the command line is ignored.
    let args: Vec<String> = std::env::args().collect();
    let server_url = args
        .iter()
        .position(|a| a == "--server-url")
        .and_then(|i| args.get(i + 1))
        .cloned()
        .unwrap_or_else(|| "http://127.0.0.1:8756".to_string());

    let client = reqwest::Client::new();

    install_panic_hook();

    // Fail fast, as a normal (non-TUI) program, before ever touching raw
    // mode / the alternate screen -- a clear error on a normal terminal
    // beats a TUI that silently hangs on your first Enter press.
    let repo_root = match check_server_health(&client, &server_url).await {
        Ok(root) => root,
        Err(msg) => {
            eprintln!("{msg}");
            eprintln!("Start it with: python -m server.app --path <repo>");
            std::process::exit(1);
        }
    };

    // --- Terminal setup ---
    // A normal terminal scrolls and echoes what you type by default (this
    // is "cooked mode"). A TUI needs "raw mode": keystrokes come to us
    // immediately, one at a time, with no automatic echo -- because WE are
    // responsible for deciding what appears on screen, not the terminal.
    enable_raw_mode()?;

    // The "alternate screen" is the same mechanism `vim`/`less` use: it
    // swaps to a separate screen buffer so our TUI doesn't scroll away
    // your shell history, and swaps back cleanly when we exit.
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;

    // Lock the cursor to a fixed shape regardless of the host terminal's
    // own default (which is what made it look different in Zed vs. cmd).
    // EnableMouseCapture is what lets us receive scroll-wheel/click
    // events at all -- without it, the terminal handles mouse input
    // itself (e.g. native text selection) and we never see it.
    execute!(stdout, SetCursorStyle::SteadyBar, EnableMouseCapture)?;

    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let mut app = App::new(repo_root);
    let mut event_stream = EventStream::new();

    // --- Main loop ---
    // This is "immediate mode" rendering: every single frame, we redraw
    // the ENTIRE screen from scratch based on current state. There's no
    // API to say "just update this one label" -- you always describe what
    // the whole screen should look like right now, and ratatui figures out
    // the efficient terminal writes underneath. Unusual coming from
    // retained-mode UI (where widgets persist and you mutate them), but it
    // makes state management much simpler: the screen is always just a
    // pure function of your app's data.
    loop {
        terminal.draw(|frame| {
            let area = frame.area();

            // Paint the whole frame with our fixed background color first.
            // Without this, any area we don't explicitly draw over shows
            // through to whatever the host terminal's own default
            // background is -- which is exactly the inconsistency we're
            // fixing.
            frame.render_widget(
                Block::default().style(Style::default().bg(theme::BACKGROUND)),
                area,
            );

            // Three-region vertical split: header, body, status bar.
            let outer = Layout::default()
                .direction(Direction::Vertical)
                .constraints([
                    Constraint::Length(2),
                    Constraint::Min(0),
                    Constraint::Length(1),
                ])
                .split(area);
            let header_area = outer[0];
            let body_area = outer[1];
            let status_area = outer[2];

            let header = Paragraph::new(Line::from(vec![
                Span::styled(
                    "OLORIN",
                    Style::default()
                        .fg(theme::ACCENT_HAT_BLUE)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(" TUI", Style::default().fg(theme::TEXT_MUTED)),
            ]))
            .alignment(Alignment::Center)
            .style(Style::default().bg(theme::BACKGROUND))
            .block(
                Block::default()
                    .borders(Borders::BOTTOM)
                    .border_style(Style::default().fg(theme::BORDER)),
            );
            frame.render_widget(header, header_area);

            // Body: three columns -- Context/Tools sidebar, Chat (fills
            // remaining space), Activity sidebar. Fixed sidebar widths --
            // no responsive narrow-terminal handling yet, a known
            // limitation on very small terminals, not solved here.
            let body = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([
                    Constraint::Length(32),
                    Constraint::Min(0),
                    Constraint::Length(32),
                ])
                .split(body_area);
            let left_area = body[0];
            let middle_area = body[1];
            let right_area = body[2];

            // Left sidebar: Context, Tools, System Status, Indicators --
            // matching the mockup's real box order. All four heights are
            // fixed except System Status (Min(0), soaks up whatever's
            // left) since it's the one that's a pure placeholder anyway --
            // empty space inside a placeholder box is harmless, unlike a
            // truncated list would be.
            let left = Layout::default()
                .direction(Direction::Vertical)
                .constraints([
                    Constraint::Length(11),
                    Constraint::Length(13),
                    Constraint::Min(0),
                    Constraint::Length(9),
                ])
                .split(left_area);
            let context_area = left[0];
            let tools_area = left[1];
            let system_status_area = left[2];
            let indicators_area = left[3];

            // Backend name formatted for display -- "boromir"/"faramir"
            // get "(Local)" appended since that's real, useful context;
            // cloud backends (groq/cerebras) are shown as-is.
            let backend_display = app.last_backend.as_deref().map(|b| match b {
                "boromir" | "faramir" => {
                    let mut c = b.chars();
                    format!("{}{} (Local)", c.next().unwrap().to_uppercase(), c.as_str())
                }
                other => {
                    let mut c = other.chars();
                    format!("{}{}", c.next().map(|f| f.to_uppercase().to_string()).unwrap_or_default(), c.as_str())
                }
            });

            // Now shows real Model/Complexity fields alongside Repository/
            // Server, populated once the first request completes ("--"
            // until then, never a fabricated number). Exactly 9 content
            // lines -- Length(11) above gives exactly 9 interior rows.
            let context_lines = vec![
                Line::from(vec![
                    Span::styled("\u{25B8} ", Style::default().fg(theme::TEXT_MUTED)),
                    Span::styled("Repository", Style::default().fg(theme::TEXT_MUTED)),
                ]),
                Line::from(Span::styled(
                    app.repo_root.as_str(),
                    Style::default().fg(theme::INFO_SKY_BLUE),
                )),
                Line::from(vec![
                    Span::styled("\u{25B8} ", Style::default().fg(theme::TEXT_MUTED)),
                    Span::styled("Server", Style::default().fg(theme::TEXT_MUTED)),
                ]),
                Line::from(Span::styled(
                    server_url.as_str(),
                    Style::default().fg(theme::INFO_SKY_BLUE),
                )),
                Line::from(""),
                Line::from(vec![
                    Span::styled("\u{25B8} ", Style::default().fg(theme::TEXT_MUTED)),
                    Span::styled("Model", Style::default().fg(theme::TEXT_MUTED)),
                ]),
                Line::from(Span::styled(
                    backend_display.as_deref().unwrap_or("--").to_string(),
                    Style::default().fg(theme::SUCCESS_GREEN),
                )),
                Line::from(vec![
                    Span::styled("\u{25B8} ", Style::default().fg(theme::TEXT_MUTED)),
                    Span::styled("Complexity", Style::default().fg(theme::TEXT_MUTED)),
                ]),
                Line::from(Span::styled(
                    app.last_complexity_score
                        .map(|c| c.to_string())
                        .unwrap_or_else(|| "--".to_string()),
                    Style::default().fg(theme::TEXT_PRIMARY),
                )),
            ];
            let context_panel = Paragraph::new(context_lines)
                .style(Style::default().bg(theme::SURFACE))
                .wrap(Wrap { trim: false })
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER))
                        .title("CONTEXT")
                        .title_style(Style::default().fg(theme::ACCENT_HAT_BLUE)),
                );
            frame.render_widget(context_panel, context_area);

            // Fixed-height, truncation-safe: if TOOL_NAMES ever grows
            // past what fits, show a "+N more" line instead of silently
            // pushing System Status/Indicators off-screen or overflowing
            // into them. (Not interactively scrollable yet -- no key/
            // mouse mapping is assigned to this panel specifically; this
            // is the "never breaks the layout" half of that concern, the
            // "can browse the rest" half is a real future addition if the
            // list actually grows enough to need it.)
            let tools_interior_height = tools_area.height.saturating_sub(2) as usize;
            let mut tools_lines: Vec<Line> = Vec::new();
            if TOOL_NAMES.len() <= tools_interior_height {
                for name in TOOL_NAMES {
                    tools_lines.push(Line::from(vec![
                        Span::styled("\u{25CF} ", Style::default().fg(theme::INFO_SKY_BLUE)),
                        Span::styled(*name, Style::default().fg(theme::TEXT_SECONDARY)),
                    ]));
                }
            } else {
                let show = tools_interior_height.saturating_sub(1);
                for name in &TOOL_NAMES[..show] {
                    tools_lines.push(Line::from(vec![
                        Span::styled("\u{25CF} ", Style::default().fg(theme::INFO_SKY_BLUE)),
                        Span::styled(*name, Style::default().fg(theme::TEXT_SECONDARY)),
                    ]));
                }
                let hidden = TOOL_NAMES.len() - show;
                tools_lines.push(Line::from(Span::styled(
                    format!("  +{hidden} more"),
                    Style::default().fg(theme::TEXT_MUTED),
                )));
            }
            let tools_panel = Paragraph::new(tools_lines)
                .style(Style::default().bg(theme::SURFACE))
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER))
                        .title("TOOLS")
                        .title_style(Style::default().fg(theme::ACCENT_HAT_BLUE)),
                );
            frame.render_widget(tools_panel, tools_area);

            // System Status: real token counts from the most recently
            // completed run ("--" until the first one finishes) -- same
            // Agent.last_* data Context's Model/Complexity fields use.
            let total_tokens = match (app.last_input_tokens, app.last_output_tokens) {
                (Some(i), Some(o)) => Some(i + o),
                _ => None,
            };
            let fmt_tokens = |t: Option<i64>| t.map(|v| v.to_string()).unwrap_or_else(|| "--".to_string());
            let system_status_panel = Paragraph::new(vec![
                Line::from(Span::styled("Input tokens", Style::default().fg(theme::TEXT_MUTED))),
                Line::from(Span::styled(
                    fmt_tokens(app.last_input_tokens),
                    Style::default().fg(theme::TEXT_PRIMARY),
                )),
                Line::from(Span::styled("Output tokens", Style::default().fg(theme::TEXT_MUTED))),
                Line::from(Span::styled(
                    fmt_tokens(app.last_output_tokens),
                    Style::default().fg(theme::TEXT_PRIMARY),
                )),
                Line::from(Span::styled("Total tokens", Style::default().fg(theme::TEXT_MUTED))),
                Line::from(Span::styled(
                    fmt_tokens(total_tokens),
                    Style::default().fg(theme::SUCCESS_GREEN),
                )),
            ])
            .style(Style::default().bg(theme::SURFACE))
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::BORDER))
                    .title("SYSTEM STATUS")
                    .title_style(Style::default().fg(theme::ACCENT_HAT_BLUE)),
            );
            frame.render_widget(system_status_panel, system_status_area);

            // Indicators: a real, fully-functional color legend -- this
            // one needed no backend data at all, just honest use of the
            // palette we already have.
            let indicator_items: [(&str, ratatui::style::Color); 5] = [
                ("Success / Completed", theme::SUCCESS_GREEN),
                ("Active / Info", theme::INFO_SKY_BLUE),
                ("Warning / Rate limit", theme::WARNING_AMBER),
                ("Error / Failed", theme::ERROR_CRIMSON),
                ("Muted / Disabled", theme::TEXT_MUTED),
            ];
            let indicator_lines: Vec<Line> = indicator_items
                .iter()
                .map(|(label, color)| {
                    Line::from(vec![
                        Span::styled("\u{25CF} ", Style::default().fg(*color)),
                        Span::styled(*label, Style::default().fg(theme::TEXT_SECONDARY)),
                    ])
                })
                .collect();
            let indicators_panel = Paragraph::new(indicator_lines)
                .style(Style::default().bg(theme::SURFACE))
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER))
                        .title("INDICATORS")
                        .title_style(Style::default().fg(theme::ACCENT_HAT_BLUE)),
                );
            frame.render_widget(indicators_panel, indicators_area);

            // Right sidebar: Activity (largest, most dynamic), Context
            // Window, Current Model -- split the same way the left
            // sidebar was, matching the mockup's real box order.
            let right = Layout::default()
                .direction(Direction::Vertical)
                .constraints([
                    Constraint::Min(0),
                    Constraint::Length(6),
                    Constraint::Length(6),
                ])
                .split(right_area);
            let activity_area = right[0];
            let context_window_area = right[1];
            let current_model_area = right[2];

            // Activity -- real, live backend events now, not a
            // placeholder. Each entry is an actual log record captured
            // server-side (server/app.py's _QueueLogHandler), the same
            // lines that print in the server's own terminal. Shows the
            // most recent events that fit the box; older ones simply
            // scroll off the top as new ones arrive, matching how the
            // mockup's own Activity feed behaves.
            let activity_interior_height = activity_area.height.saturating_sub(2) as usize;
            let activity_width = activity_area.width.saturating_sub(4) as usize;
            let mut activity_lines: Vec<Line> = Vec::new();
            if app.activity.is_empty() {
                activity_lines.push(Line::from(Span::styled(
                    "No activity yet -- ask",
                    Style::default().fg(theme::TEXT_MUTED),
                )));
                activity_lines.push(Line::from(Span::styled(
                    "something below to see",
                    Style::default().fg(theme::TEXT_MUTED),
                )));
                activity_lines.push(Line::from(Span::styled(
                    "live steps here.",
                    Style::default().fg(theme::TEXT_MUTED),
                )));
            } else {
                let start = app.activity.len().saturating_sub(activity_interior_height);
                for event in &app.activity[start..] {
                    let color = match event.level.as_str() {
                        "WARNING" => theme::WARNING_AMBER,
                        "ERROR" => theme::ERROR_CRIMSON,
                        _ => theme::INFO_SKY_BLUE,
                    };
                    // Truncated, not wrapped -- one line per event,
                    // matching the mockup's single-line-per-event style;
                    // a wrapped long message would break the "last N fit
                    // the box" slicing above, since wrapped rows don't
                    // line up 1:1 with array entries anymore.
                    let char_count = event.message.chars().count();
                    let text = if char_count > activity_width {
                        let truncated: String =
                            event.message.chars().take(activity_width.saturating_sub(1)).collect();
                        format!("{truncated}.")
                    } else {
                        event.message.clone()
                    };
                    activity_lines.push(Line::from(vec![
                        Span::styled("\u{25CF} ", Style::default().fg(color)),
                        Span::styled(text, Style::default().fg(theme::TEXT_SECONDARY)),
                    ]));
                }
            }
            let activity_panel = Paragraph::new(activity_lines)
                .style(Style::default().bg(theme::SURFACE))
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER))
                        .title("ACTIVITY")
                        .title_style(Style::default().fg(theme::ACCENT_HAT_BLUE)),
                );
            frame.render_widget(activity_panel, activity_area);

            // Context Window: real token counts from the most recently
            // completed run. Deliberately NOT a fake percentage-of-
            // capacity bar -- max context size varies per backend/model
            // and isn't centrally tracked, so a real count is shown
            // instead of a number we'd have to guess the denominator for.
            let context_window_panel = Paragraph::new(vec![
                Line::from(Span::styled("Last request tokens", Style::default().fg(theme::TEXT_MUTED))),
                Line::from(Span::styled(
                    fmt_tokens(total_tokens),
                    Style::default().fg(theme::INFO_SKY_BLUE),
                )),
                Line::from(Span::styled("Complexity score", Style::default().fg(theme::TEXT_MUTED))),
                Line::from(Span::styled(
                    app.last_complexity_score
                        .map(|c| c.to_string())
                        .unwrap_or_else(|| "--".to_string()),
                    Style::default().fg(theme::INFO_SKY_BLUE),
                )),
            ])
            .style(Style::default().bg(theme::SURFACE))
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::BORDER))
                    .title("CONTEXT WINDOW")
                    .title_style(Style::default().fg(theme::ACCENT_HAT_BLUE)),
            );
            frame.render_widget(context_window_panel, context_window_area);

            // Current Model: backend/provider/model from the most
            // recently completed run.
            let current_model_panel = Paragraph::new(vec![
                Line::from(Span::styled("Backend", Style::default().fg(theme::TEXT_MUTED))),
                Line::from(Span::styled(
                    backend_display.as_deref().unwrap_or("--").to_string(),
                    Style::default().fg(theme::SUCCESS_GREEN),
                )),
                Line::from(Span::styled("Model", Style::default().fg(theme::TEXT_MUTED))),
                Line::from(Span::styled(
                    app.last_model.as_deref().unwrap_or("--").to_string(),
                    Style::default().fg(theme::TEXT_PRIMARY),
                )),
            ])
            .style(Style::default().bg(theme::SURFACE))
            .wrap(Wrap { trim: false })
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::BORDER))
                    .title("CURRENT MODEL")
                    .title_style(Style::default().fg(theme::ACCENT_HAT_BLUE)),
            );
            frame.render_widget(current_model_panel, current_model_area);

            // Middle column: chat/transcript fills the space, input box
            // pinned at the BOTTOM -- matches the mockup, and is simply
            // better UX than this layout's earlier input-on-top order.
            // Input gets real room now (Length(7), a bit taller again per
            // feedback) -- see the wrapping logic below for why that
            // alone doesn't fully fix long input.
            let middle = Layout::default()
                .direction(Direction::Vertical)
                .constraints([Constraint::Min(0), Constraint::Length(7)])
                .split(middle_area);
            let chat_area = middle[0];
            let input_area = middle[1];

            app.input_rect = input_area;

            // Character-based (not word-based) hard-wrap for the input,
            // deliberately simpler than the chat transcript's word-aware
            // wrapping. A real bug in the old single-line-only version:
            // typed text past the box's width had nowhere to render and
            // silently vanished from view (while still being sent
            // correctly) -- wrapping every `input_width` characters makes
            // the cursor's (row, col) trivial to compute exactly
            // (row = cursor / width, col = cursor % width), which matters
            // more here than nice word-boundary wrapping does for a
            // short query box.
            let input_width = input_area.width.saturating_sub(2).max(1) as usize;
            let input_chars: Vec<char> = app.input.chars().collect();
            let input_rows: Vec<String> = if input_chars.is_empty() {
                vec![String::new()]
            } else {
                input_chars
                    .chunks(input_width)
                    .map(|c| c.iter().collect())
                    .collect()
            };
            let cursor_row = (app.cursor / input_width) as u16;
            let cursor_col = (app.cursor % input_width) as u16;

            // Keep the cursor's row visible ONLY while input_follow_cursor
            // is true -- otherwise this would unconditionally snap the
            // view back to the cursor on every single frame, which would
            // make Ctrl+Up/Ctrl+Down (manual scrollback below) pointless:
            // the very next redraw would undo it before it was ever
            // visible. Mirrors the chat panel's follow_bottom logic
            // exactly.
            let input_viewport_height = input_area.height.saturating_sub(2);
            let input_total_rows = input_rows.len() as u16;
            let input_max_offset = input_total_rows.saturating_sub(input_viewport_height);
            if app.input_follow_cursor {
                if cursor_row < app.input_scroll {
                    app.input_scroll = cursor_row;
                } else if cursor_row >= app.input_scroll + input_viewport_height {
                    app.input_scroll = cursor_row + 1 - input_viewport_height;
                }
            } else {
                app.input_scroll = app.input_scroll.min(input_max_offset);
                if app.input_scroll <= cursor_row
                    && cursor_row < app.input_scroll + input_viewport_height
                {
                    // Scrolled back to where the cursor's row is visible
                    // again -- resume auto-follow.
                    app.input_follow_cursor = true;
                }
            }

            let input_lines: Vec<Line> = input_rows
                .iter()
                .map(|row| Line::from(Span::raw(row.clone())))
                .collect();
            let input_box = Paragraph::new(input_lines)
                .style(Style::default().fg(theme::TEXT_PRIMARY).bg(theme::SURFACE))
                .scroll((app.input_scroll, 0))
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER))
                        .title("ASK OLORIN")
                        .title_style(Style::default().fg(theme::ACCENT_HAT_BLUE)),
                );
            frame.render_widget(input_box, input_area);

            // Position the REAL terminal cursor (not a drawn character)
            // at its actual wrapped (row, col), adjusted for scroll.
            // +1/+1 skips past the input box's left/top border.
            frame.set_cursor_position((
                input_area.x + 1 + cursor_col,
                input_area.y + 1 + cursor_row.saturating_sub(app.input_scroll),
            ));

            // Build the transcript: one bold, colored role-label line per
            // turn, followed by that turn's text, our own word-wrapped to
            // fit the box's interior width. A transient "Olorin is
            // thinking..." line is appended (not stored in history) while
            // a request is pending -- it naturally disappears once the
            // real Turn gets pushed.
            let content_width = chat_area.width.saturating_sub(2).max(1) as usize;
            let mut lines: Vec<Line> = Vec::new();

            if app.history.is_empty() {
                lines.push(Line::from(Span::styled(
                    "(ask something below)",
                    Style::default().fg(theme::TEXT_MUTED),
                )));
            }

            for turn in &app.history {
                let (label, color) = match turn.role {
                    Role::User => ("You", theme::TEXT_PRIMARY),
                    Role::Olorin => ("Olorin", theme::ACCENT_HAT_BLUE),
                    Role::Error => ("Error", theme::ERROR_CRIMSON),
                };
                lines.push(Line::from(Span::styled(
                    label,
                    Style::default().fg(color).add_modifier(Modifier::BOLD),
                )));
                lines.extend(markdown::render(&turn.text, content_width));
                lines.push(Line::from("")); // spacer between turns
            }

            if app.pending_rx.is_some() {
                lines.push(Line::from(Span::styled(
                    "Olorin is thinking...",
                    Style::default().fg(theme::TEXT_MUTED),
                )));
            }

            // Scroll math. `lines.len()` is exact (we did our own
            // wrapping, so there's no need to ask ratatui to compute
            // wrapped-line counts) -- total content height vs. the box's
            // visible interior height gives the furthest we can legally
            // scroll down.
            let total_lines = lines.len() as u16;
            let viewport_height = chat_area.height.saturating_sub(2);
            let max_offset = total_lines.saturating_sub(viewport_height);

            if app.follow_bottom {
                app.scroll_offset = max_offset;
            } else {
                app.scroll_offset = app.scroll_offset.min(max_offset);
                if app.scroll_offset >= max_offset {
                    // Scrolled all the way back down -- resume following.
                    app.follow_bottom = true;
                }
            }

            let response_box = Paragraph::new(Text::from(lines))
                .style(Style::default().bg(theme::SURFACE))
                .scroll((app.scroll_offset, 0))
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER))
                        .title("CHAT")
                        .title_style(Style::default().fg(theme::ACCENT_HAT_BLUE)),
                );
            frame.render_widget(response_box, chat_area);

            let status = Paragraph::new("Esc quit | Up/Down/PgUp/PgDn scroll chat | Ctrl+Up/Down scroll input | Left/Right/Home/End edit")
                .style(Style::default().fg(theme::TEXT_MUTED).bg(theme::BACKGROUND));
            frame.render_widget(status, status_area);
        })?;

        // Race two things each iteration: a real keyboard event arriving
        // via crossterm's async EventStream, vs. the pending request's
        // mpsc receiver yielding its next item (a log event, or the final
        // Done). Whichever happens first wins this iteration; we handle
        // it, then loop back to redraw and race again.
        tokio::select! {
            maybe_event = event_stream.next() => {
                let Some(Ok(event)) = maybe_event else {
                    // Stream closed or an IO error -- nothing to act on.
                    continue;
                };

                match event {
                    Event::Mouse(mouse) => {
                        match mouse.kind {
                            MouseEventKind::ScrollUp => {
                                app.follow_bottom = false;
                                app.scroll_offset = app.scroll_offset.saturating_sub(3);
                            }
                            MouseEventKind::ScrollDown => {
                                app.scroll_offset = app.scroll_offset.saturating_add(3);
                            }
                            MouseEventKind::Down(MouseButton::Left) => {
                                if app.pending_rx.is_none() {
                                    // Click-to-position-cursor, now
                                    // accounting for the input box being
                                    // multiple wrapped rows tall, not
                                    // just one. Width/scroll are derived
                                    // from the stored input_rect rather
                                    // than needing a separate stashed
                                    // field -- the draw closure already
                                    // keeps input_rect current every
                                    // frame.
                                    let r = app.input_rect;
                                    let text_top = r.y + 1;
                                    let text_left = r.x + 1;
                                    let text_right = r.x + r.width.saturating_sub(1);
                                    let text_bottom = r.y + r.height.saturating_sub(1);
                                    let width = r.width.saturating_sub(2).max(1) as usize;
                                    if mouse.row >= text_top
                                        && mouse.row < text_bottom
                                        && mouse.column >= text_left
                                        && mouse.column < text_right
                                    {
                                        let clicked_row =
                                            (mouse.row - text_top) as usize + app.input_scroll as usize;
                                        let clicked_col = (mouse.column - text_left) as usize;
                                        let flat = clicked_row * width + clicked_col;
                                        app.cursor = flat.min(app.input.chars().count());
                                    }
                                }
                            }
                            _ => {}
                        }
                        continue;
                    }
                    Event::Key(key) => {
                        // Windows reports both a Press and a Release
                        // event per keystroke; Linux/Mac terminals
                        // typically only send Press. Filtering to
                        // Press-only keeps behavior consistent across
                        // platforms.
                        if key.kind != KeyEventKind::Press {
                            continue;
                        }

                        match key.code {
                    KeyCode::Esc => break,
                    // Manual input-box scrollback -- Ctrl+Up/Ctrl+Down
                    // specifically, since plain Up/Down are already the
                    // chat panel's scroll keys. Detaches from
                    // input_follow_cursor the same way Up/PageUp detach
                    // the chat panel from follow_bottom below. These
                    // guarded arms MUST come before the plain Up/Down
                    // arms -- match arms are checked in order, and an
                    // unguarded KeyCode::Up would otherwise match first
                    // regardless of whether Ctrl was held.
                    KeyCode::Up if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        app.input_follow_cursor = false;
                        app.input_scroll = app.input_scroll.saturating_sub(1);
                    }
                    KeyCode::Down if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        app.input_scroll = app.input_scroll.saturating_add(1);
                    }
                    // Manual scrollback. Up/PageUp detach from the live
                    // bottom; Down/PageDown move back toward it, and the
                    // draw closure above re-engages follow_bottom once
                    // scroll_offset reaches the real max (it knows the
                    // true max; we don't, out here, since it depends on
                    // the current terminal size).
                    KeyCode::Up => {
                        app.follow_bottom = false;
                        app.scroll_offset = app.scroll_offset.saturating_sub(1);
                    }
                    KeyCode::Down => {
                        app.scroll_offset = app.scroll_offset.saturating_add(1);
                    }
                    KeyCode::PageUp => {
                        app.follow_bottom = false;
                        app.scroll_offset = app.scroll_offset.saturating_sub(10);
                    }
                    KeyCode::PageDown => {
                        app.scroll_offset = app.scroll_offset.saturating_add(10);
                    }
                    // Standard terminal line-editing: move within the
                    // input without submitting or deleting anything.
                    // Also re-engages input_follow_cursor -- if you were
                    // mid Ctrl+Up/Down review, actually moving the cursor
                    // is a clear signal you want to see it again.
                    KeyCode::Left if app.pending_rx.is_none() => {
                        app.cursor = app.cursor.saturating_sub(1);
                        app.input_follow_cursor = true;
                    }
                    KeyCode::Right if app.pending_rx.is_none() => {
                        let char_count = app.input.chars().count();
                        app.cursor = (app.cursor + 1).min(char_count);
                        app.input_follow_cursor = true;
                    }
                    KeyCode::Home if app.pending_rx.is_none() => {
                        app.cursor = 0;
                        app.input_follow_cursor = true;
                    }
                    KeyCode::End if app.pending_rx.is_none() => {
                        app.cursor = app.input.chars().count();
                        app.input_follow_cursor = true;
                    }
                    // Guarded on pending_rx being None: don't let a
                    // second query start (or the input box change)
                    // while one is already in flight.
                    KeyCode::Enter if app.pending_rx.is_none() => {
                        let query = app.input.trim().to_string();
                        if !query.is_empty() {
                            app.input.clear();
                            app.cursor = 0;
                            app.input_scroll = 0;
                            app.input_follow_cursor = true;
                            app.history.push(Turn {
                                role: Role::User,
                                text: query.clone(),
                            });
                            // Sending a new query is a clear signal you
                            // want to see it, even if you were mid-
                            // scrollback reviewing older turns.
                            app.follow_bottom = true;
                            // Fresh request, fresh activity feed -- last
                            // request's steps aren't relevant anymore.
                            app.activity.clear();

                            let (tx, rx) = mpsc::unbounded_channel();
                            app.pending_rx = Some(rx);

                            // reqwest::Client is cheap to clone (an Arc
                            // internally) -- this spawns the request onto
                            // its own tokio task so it runs concurrently
                            // with the main loop instead of blocking it.
                            let client = client.clone();
                            let server_url = server_url.clone();
                            tokio::spawn(ask_server_stream(client, server_url, query, tx));
                        }
                    }
                    KeyCode::Char(c) if app.pending_rx.is_none() => {
                        let byte_idx = char_byte_index(&app.input, app.cursor);
                        app.input.insert(byte_idx, c);
                        app.cursor += 1;
                        app.input_follow_cursor = true;
                    }
                    KeyCode::Backspace if app.pending_rx.is_none() => {
                        if app.cursor > 0 {
                            let start = char_byte_index(&app.input, app.cursor - 1);
                            let end = char_byte_index(&app.input, app.cursor);
                            app.input.drain(start..end);
                            app.cursor -= 1;
                            app.input_follow_cursor = true;
                        }
                    }
                    _ => {}
                        }
                    }
                    _ => {}
                }
            }

            // Wrapped in an async block deliberately -- see this file's
            // history for why. The block is lazy: code inside only runs
            // when this branch is actually POLLED, which the `if` guard
            // prevents whenever pending_rx is None. The `None => pending()`
            // arm is a second, defensive layer: even if this branch were
            // ever polled while empty, it resolves to a future that simply
            // never completes, instead of touching an unwrap() at all.
            stream_item = async {
                match app.pending_rx.as_mut() {
                    Some(rx) => rx.recv().await,
                    None => std::future::pending().await,
                }
            }, if app.pending_rx.is_some() => {
                match stream_item {
                    Some(StreamEvent::Log(event)) => {
                        app.activity.push(event);
                        // Cap so a very long-running request (we've seen
                        // real ones take 4+ minutes across many steps)
                        // can't grow this unboundedly.
                        if app.activity.len() > 200 {
                            app.activity.remove(0);
                        }
                    }
                    Some(StreamEvent::Done(outcome)) => {
                        app.pending_rx = None;
                        app.history.push(match outcome {
                            Ok(meta) => {
                                app.last_backend = meta.backend;
                                app.last_provider = meta.provider;
                                app.last_model = meta.model;
                                app.last_complexity_score = meta.complexity_score;
                                app.last_input_tokens = meta.input_tokens;
                                app.last_output_tokens = meta.output_tokens;
                                Turn { role: Role::Olorin, text: meta.answer }
                            }
                            Err(err) => Turn { role: Role::Error, text: err },
                        });
                        app.follow_bottom = true;
                    }
                    None => {
                        // Channel closed without ever sending a Done --
                        // the background task panicked, or something
                        // dropped the connection in a way ask_server_
                        // stream's own error paths didn't catch. Clear
                        // pending_rx explicitly here (not just relying on
                        // the Done case above) so the UI can't get stuck
                        // waiting forever on a channel that will never
                        // produce anything else.
                        app.pending_rx = None;
                        app.history.push(Turn {
                            role: Role::Error,
                            text: "Connection to server lost before a response arrived."
                                .to_string(),
                        });
                    }
                }
            }
        }
    }

    // --- Terminal teardown ---
    // Mirror image of setup. If this doesn't run, your real terminal is
    // left in raw mode / on the alternate screen -- annoying but not
    // catastrophic (typing `reset` and pressing Enter blind fixes it).
    execute!(
        terminal.backend_mut(),
        SetCursorStyle::DefaultUserShape,
        DisableMouseCapture
    )?;
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;

    Ok(())
}
