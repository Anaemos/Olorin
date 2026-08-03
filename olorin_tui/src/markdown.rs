//! Minimal markdown-ish rendering for chat responses: headers, bold,
//! italic, inline code, bullet lists, and tables get real styled/
//! column-aligned output instead of raw markdown syntax showing up as
//! literal text (the "**Recommendation:**" problem). Deliberately NOT
//! full CommonMark -- no syntax highlighting, no nested lists, no links.
//! Fenced code blocks (```...```) ARE detected, but only so their
//! contents are shown as plain literal text with NO inline parsing --
//! without that, markdown the LLM writes as a literal example (e.g.
//! showing someone what "**bold**" syntax looks like) gets incorrectly
//! parsed as real formatting instead of displayed as-is. Real LLM
//! responses lean heavily on headers/bold/bullets/tables, so that's
//! what this covers.
//!
//! Plain ASCII only for any rendering "chrome" we add ourselves (bullet
//! markers, truncation marks) -- same lesson already learned the hard
//! way with setup.ps1's em-dash encoding bug on the Python side; no
//! reason to reintroduce that fragility here.

use ratatui::{
    style::{Modifier, Style},
    text::{Line, Span},
};

use crate::theme;

/// Render one turn's full response text into styled, word-wrapped lines
/// that fit within `width` columns.
pub fn render(text: &str, width: usize) -> Vec<Line<'static>> {
    let raw_lines: Vec<&str> = text.lines().collect();
    let mut output: Vec<Line<'static>> = Vec::new();
    let mut i = 0;

    while i < raw_lines.len() {
        let trimmed = raw_lines[i].trim();

        if trimmed.is_empty() {
            output.push(Line::from(""));
            i += 1;
            continue;
        }

        // Fenced code block: everything between an opening and closing
        // "```" line is shown as literal, unparsed text. Without this,
        // markdown the LLM writes AS AN EXAMPLE (showing literal
        // "**bold**" syntax to explain it) gets misread as real
        // formatting instead of displayed as-is.
        if trimmed.starts_with("```") {
            i += 1;
            let mut code_lines: Vec<&str> = Vec::new();
            while i < raw_lines.len() && !raw_lines[i].trim().starts_with("```") {
                code_lines.push(raw_lines[i]);
                i += 1;
            }
            if i < raw_lines.len() {
                i += 1; // consume the closing fence
            }
            for code_line in code_lines {
                for wrapped in wrap_plain(code_line, width) {
                    output.push(Line::from(Span::styled(
                        wrapped,
                        Style::default().fg(theme::TEXT_MUTED),
                    )));
                }
            }
            continue;
        }

        // Table block: gather every contiguous line that looks like a
        // table row, then render the whole block together (column
        // widths only make sense computed across all rows at once).
        if is_table_row(trimmed) {
            let mut block: Vec<&str> = Vec::new();
            while i < raw_lines.len() && is_table_row(raw_lines[i].trim()) {
                block.push(raw_lines[i].trim());
                i += 1;
            }

            if block.len() >= 2 {
                let mut has_header = false;
                let mut data_rows: Vec<Vec<String>> = Vec::new();
                for (idx, row_line) in block.iter().enumerate() {
                    // A markdown separator row ("|---|---|") only ever
                    // appears right after the header -- skip it as a
                    // data row, but remember it confirmed row 0 is a
                    // real header.
                    if idx == 1 && is_separator_row(row_line) {
                        has_header = true;
                        continue;
                    }
                    data_rows.push(parse_table_row(row_line));
                }
                output.extend(render_table(&data_rows, has_header, width));
            } else {
                // Just one stray pipe-containing line, not a real table
                // -- fall back to ordinary paragraph handling.
                output.extend(render_paragraph_line(block[0], width));
            }
            continue;
        }

        if let Some(heading) = parse_heading(trimmed) {
            output.push(Line::from(Span::styled(
                heading.to_string(),
                Style::default()
                    .fg(theme::ACCENT_HAT_BLUE)
                    .add_modifier(Modifier::BOLD),
            )));
            i += 1;
            continue;
        }

        if let Some(rest) = trimmed
            .strip_prefix("- ")
            .or_else(|| trimmed.strip_prefix("* "))
        {
            let fragments = parse_inline(rest);
            let wrapped = wrap_styled_line(fragments, width.saturating_sub(2));
            output.extend(prefix_bullet(wrapped));
            i += 1;
            continue;
        }

        // Ordinary paragraph text -- also covers numbered list items
        // ("1. text"); the digits/period are already readable as plain
        // text once inline bold/italic within them render correctly, so
        // no special-case handling needed there.
        output.extend(render_paragraph_line(trimmed, width));
        i += 1;
    }

    if output.is_empty() {
        output.push(Line::from(""));
    }
    output
}

fn render_paragraph_line(line: &str, width: usize) -> Vec<Line<'static>> {
    let fragments = parse_inline(line);
    wrap_styled_line(fragments, width)
}

// Plain (unstyled) word-wrap, used only for fenced-code-block content --
// deliberately no inline parsing there (it's meant to be shown exactly
// as written), so this doesn't need the styled-fragment machinery
// wrap_styled_line uses. Known, accepted limitation: leading whitespace/
// indentation within a code line is lost (split_whitespace strips it) --
// fine given this project's explicit "no real code rendering" scope.
fn wrap_plain(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut result = Vec::new();
    let mut current = String::new();

    for word in text.split_whitespace() {
        if current.is_empty() {
            current.push_str(word);
        } else if current.chars().count() + 1 + word.chars().count() <= width {
            current.push(' ');
            current.push_str(word);
        } else {
            result.push(std::mem::take(&mut current));
            current.push_str(word);
        }
    }
    if !current.is_empty() {
        result.push(current);
    }
    if result.is_empty() {
        result.push(String::new());
    }
    result
}

// ATX-style headings only ("# Heading" through "###### Heading"). Checks
// the character right after the run of '#'s is a real space, so
// something like "#hashtag" (no space) correctly falls through to being
// treated as plain text instead.
fn parse_heading(trimmed: &str) -> Option<&str> {
    let hashes_end = trimmed.find(|c: char| c != '#')?;
    if hashes_end == 0 || hashes_end > 6 {
        return None;
    }
    if trimmed.as_bytes().get(hashes_end) != Some(&b' ') {
        return None;
    }
    Some(trimmed[hashes_end..].trim_start())
}

fn prefix_bullet(lines: Vec<Line<'static>>) -> Vec<Line<'static>> {
    let mut result = Vec::new();
    for (idx, line) in lines.into_iter().enumerate() {
        // Marker on the first wrapped line, matching indent (no visible
        // marker) on any continuation lines so wrapped bullet text lines
        // up under the marker rather than the left margin.
        let prefix = if idx == 0 { "* " } else { "  " };
        let mut spans = vec![Span::styled(
            prefix,
            Style::default().fg(theme::TEXT_MUTED),
        )];
        spans.extend(line.spans);
        result.push(Line::from(spans));
    }
    result
}

// ---- Table handling ----

fn is_table_row(trimmed: &str) -> bool {
    trimmed.starts_with('|') && trimmed.ends_with('|') && trimmed.matches('|').count() >= 2
}

fn is_separator_row(trimmed: &str) -> bool {
    let cells = parse_table_row(trimmed);
    !cells.is_empty()
        && cells
            .iter()
            .all(|c| !c.is_empty() && c.chars().all(|ch| ch == '-' || ch == ':'))
}

fn parse_table_row(trimmed: &str) -> Vec<String> {
    let inner = trimmed.trim_start_matches('|').trim_end_matches('|');
    inner.split('|').map(|c| c.trim().to_string()).collect()
}

fn truncate_to_width(text: &str, width: usize) -> String {
    let char_count = text.chars().count();
    if char_count <= width {
        text.to_string()
    } else if width == 0 {
        String::new()
    } else if width == 1 {
        ".".to_string()
    } else {
        let truncated: String = text.chars().take(width - 1).collect();
        format!("{truncated}.")
    }
}

// Column widths are sized to guarantee the WHOLE table fits `width` --
// natural (content-driven) width when there's room, otherwise scaled
// down proportionally with a hard floor of 3 chars/column, truncating
// any cell that doesn't fit. This is what actually fixes the "elongated
// weird table" problem: by construction, a row can never exceed `width`.
fn render_table(rows: &[Vec<String>], has_header: bool, width: usize) -> Vec<Line<'static>> {
    if rows.is_empty() {
        return Vec::new();
    }
    let num_cols = rows.iter().map(|r| r.len()).max().unwrap_or(0);
    if num_cols == 0 {
        return Vec::new();
    }

    let mut natural = vec![1usize; num_cols];
    for row in rows {
        for (i, cell) in row.iter().enumerate() {
            natural[i] = natural[i].max(cell.chars().count());
        }
    }

    // Overhead: "| " + " | " between columns + trailing " |".
    let overhead = num_cols.saturating_sub(1) * 3 + 4;
    let available = width.saturating_sub(overhead).max(num_cols * 3);
    let natural_total: usize = natural.iter().sum();

    let col_widths: Vec<usize> = if natural_total <= available {
        natural.clone()
    } else {
        natural
            .iter()
            .map(|&n| ((n * available) / natural_total.max(1)).max(3))
            .collect()
    };

    let mut lines = Vec::new();
    for (row_idx, row) in rows.iter().enumerate() {
        let mut spans = vec![Span::styled("| ", Style::default().fg(theme::BORDER))];
        // Headers get a uniform bold style regardless of any inline
        // markdown within the cell -- the row's already visually marked
        // as a header, so there's nothing to gain from also respecting
        // e.g. a stray "**" someone put in a header cell.
        let header_override = if has_header && row_idx == 0 {
            Some(
                Style::default()
                    .fg(theme::TEXT_PRIMARY)
                    .add_modifier(Modifier::BOLD),
            )
        } else {
            None
        };
        for (i, &col_width) in col_widths.iter().enumerate() {
            let cell = row.get(i).map(String::as_str).unwrap_or("");
            spans.extend(styled_cell_spans(cell, col_width, header_override));
            spans.push(Span::styled(" | ", Style::default().fg(theme::BORDER)));
        }
        lines.push(Line::from(spans));

        if has_header && row_idx == 0 {
            let mut sep = String::from("|");
            for &cw in &col_widths {
                sep.push_str(&"-".repeat(cw + 2));
                sep.push('|');
            }
            lines.push(Line::from(Span::styled(
                sep,
                Style::default().fg(theme::BORDER),
            )));
        }
    }
    lines
}

// A table cell's content, inline-markdown-parsed (so "**Bold**" inside a
// cell renders the same as it would anywhere else) and truncated/padded
// to exactly `col_width` characters so column alignment holds regardless
// of how much markdown syntax the cell's raw text contained. When
// `header_override` is Some, every piece uses that style instead of its
// own parsed style (see call site).
fn styled_cell_spans(
    cell: &str,
    col_width: usize,
    header_override: Option<Style>,
) -> Vec<Span<'static>> {
    let fragments = parse_inline(cell);
    let mut spans = Vec::new();
    let mut used = 0usize;

    for (text, style) in fragments {
        if used >= col_width {
            break;
        }
        let remaining = col_width - used;
        let text_width = text.chars().count();
        let piece_style = header_override.unwrap_or_else(|| inline_style_to_ratatui(style));

        if text_width <= remaining {
            used += text_width;
            spans.push(Span::styled(text, piece_style));
        } else {
            let truncated = truncate_to_width(&text, remaining);
            used += truncated.chars().count();
            spans.push(Span::styled(truncated, piece_style));
            break;
        }
    }

    if used < col_width {
        spans.push(Span::raw(" ".repeat(col_width - used)));
    }

    spans
}

// ---- Inline formatting (bold / italic / code) ----

#[derive(Clone, Copy)]
enum InlineStyle {
    Plain,
    Bold,
    Italic,
    BoldItalic,
    Code,
}

fn inline_style_to_ratatui(style: InlineStyle) -> Style {
    match style {
        InlineStyle::Plain => Style::default().fg(theme::TEXT_SECONDARY),
        InlineStyle::Bold => Style::default()
            .fg(theme::TEXT_PRIMARY)
            .add_modifier(Modifier::BOLD),
        InlineStyle::Italic => Style::default()
            .fg(theme::TEXT_SECONDARY)
            .add_modifier(Modifier::ITALIC),
        InlineStyle::BoldItalic => Style::default()
            .fg(theme::TEXT_PRIMARY)
            .add_modifier(Modifier::BOLD | Modifier::ITALIC),
        InlineStyle::Code => Style::default().fg(theme::ACCENT_SEA_BLUE),
    }
}

// A simple linear scanner, not a recursive parser -- markdown's inline
// formatting doesn't nest in ways that matter for chat responses, so a
// single left-to-right pass looking for the next marker is enough. Any
// marker with no matching close on the same line (e.g. a stray "*") just
// falls through and gets treated as a literal character, rather than
// erroring or eating the rest of the line.
fn parse_inline(line: &str) -> Vec<(String, InlineStyle)> {
    let chars: Vec<char> = line.chars().collect();
    let mut result = Vec::new();
    let mut current = String::new();
    let mut i = 0;

    while i < chars.len() {
        // ***bold+italic*** -- checked before ** so the longer marker
        // wins whenever both could match at this position (three stars
        // always implies the ** check below would also match).
        if chars[i] == '*' && i + 2 < chars.len() && chars[i + 1] == '*' && chars[i + 2] == '*' {
            if let Some(end) = find_closing_triple(&chars, i + 3) {
                flush_plain(&mut current, &mut result);
                let inner: String = chars[i + 3..end].iter().collect();
                if !inner.is_empty() {
                    result.push((inner, InlineStyle::BoldItalic));
                }
                i = end + 3;
                continue;
            }
        }

        if chars[i] == '*' && i + 1 < chars.len() && chars[i + 1] == '*' {
            if let Some(end) = find_closing_double(&chars, i + 2) {
                flush_plain(&mut current, &mut result);
                let inner: String = chars[i + 2..end].iter().collect();
                if !inner.is_empty() {
                    result.push((inner, InlineStyle::Bold));
                }
                i = end + 2;
                continue;
            }
        } else if chars[i] == '*' || chars[i] == '_' {
            let marker = chars[i];
            // CommonMark restricts underscore emphasis from firing
            // INSIDE a word (so "finish_reason", "tool_calls", any
            // snake_case identifier written outside backticks, doesn't
            // get its underscores silently eaten as emphasis markers).
            // Asterisk emphasis has no such restriction. Require a
            // non-alphanumeric character (or start/end of line) on both
            // sides of an underscore pair; asterisk pairs skip this
            // check entirely.
            let left_ok = marker != '_' || i == 0 || !chars[i - 1].is_alphanumeric();
            if left_ok {
                if let Some(end) = find_closing_single(&chars, i + 1, marker) {
                    let right_ok = marker != '_'
                        || end + 1 >= chars.len()
                        || !chars[end + 1].is_alphanumeric();
                    if right_ok {
                        flush_plain(&mut current, &mut result);
                        let inner: String = chars[i + 1..end].iter().collect();
                        if !inner.is_empty() {
                            result.push((inner, InlineStyle::Italic));
                        }
                        i = end + 1;
                        continue;
                    }
                }
            }
        } else if chars[i] == '`' {
            if let Some(end) = find_closing_single(&chars, i + 1, '`') {
                flush_plain(&mut current, &mut result);
                let inner: String = chars[i + 1..end].iter().collect();
                result.push((inner, InlineStyle::Code));
                i = end + 1;
                continue;
            }
        }
        current.push(chars[i]);
        i += 1;
    }
    flush_plain(&mut current, &mut result);
    result
}

fn flush_plain(current: &mut String, result: &mut Vec<(String, InlineStyle)>) {
    if !current.is_empty() {
        result.push((std::mem::take(current), InlineStyle::Plain));
    }
}

fn find_closing_double(chars: &[char], start: usize) -> Option<usize> {
    let mut i = start;
    while i + 1 < chars.len() {
        if chars[i] == '*' && chars[i + 1] == '*' {
            return Some(i);
        }
        i += 1;
    }
    None
}

fn find_closing_triple(chars: &[char], start: usize) -> Option<usize> {
    let mut i = start;
    while i + 2 < chars.len() {
        if chars[i] == '*' && chars[i + 1] == '*' && chars[i + 2] == '*' {
            return Some(i);
        }
        i += 1;
    }
    None
}

fn find_closing_single(chars: &[char], start: usize, marker: char) -> Option<usize> {
    (start..chars.len()).find(|&i| chars[i] == marker)
}

// ---- Word-wrapping a styled line ----

// Wraps a sequence of (text, style) fragments at word boundaries into
// however many Lines fit `width` columns each, preserving each word's
// own style. Fragments are first flattened into "words" (runs of
// non-whitespace, possibly spanning multiple styles if formatting
// changes mid-word with no space -- rare, but handled rather than
// panicking), since wrapping has to happen at word boundaries in the
// RENDERED text, not at the markdown syntax boundaries.
fn wrap_styled_line(fragments: Vec<(String, InlineStyle)>, width: usize) -> Vec<Line<'static>> {
    let width = width.max(1);

    let mut words: Vec<Vec<(String, InlineStyle)>> = Vec::new();
    let mut current_word: Vec<(String, InlineStyle)> = Vec::new();

    for (text, style) in fragments {
        let mut piece = String::new();
        for ch in text.chars() {
            if ch.is_whitespace() {
                if !piece.is_empty() {
                    current_word.push((std::mem::take(&mut piece), style));
                }
                if !current_word.is_empty() {
                    words.push(std::mem::take(&mut current_word));
                }
            } else {
                piece.push(ch);
            }
        }
        if !piece.is_empty() {
            current_word.push((piece, style));
        }
    }
    if !current_word.is_empty() {
        words.push(current_word);
    }

    let mut out_lines: Vec<Line<'static>> = Vec::new();
    let mut current_spans: Vec<Span<'static>> = Vec::new();
    let mut current_width = 0usize;

    for word in words {
        let word_width: usize = word.iter().map(|(t, _)| t.chars().count()).sum();
        let needs_space = !current_spans.is_empty();
        let extra = if needs_space { 1 } else { 0 };

        if current_width + extra + word_width > width && !current_spans.is_empty() {
            out_lines.push(Line::from(std::mem::take(&mut current_spans)));
            current_width = 0;
        }

        if !current_spans.is_empty() {
            current_spans.push(Span::raw(" "));
            current_width += 1;
        }

        for (text, style) in word {
            current_spans.push(Span::styled(text, inline_style_to_ratatui(style)));
        }
        current_width += word_width;
    }

    if !current_spans.is_empty() {
        out_lines.push(Line::from(current_spans));
    }
    if out_lines.is_empty() {
        out_lines.push(Line::from(""));
    }

    out_lines
}
