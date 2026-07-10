// indexer_core — parallel, .gitignore-aware file walker for Olorin.
//
// Responsibilities (per OLORIN_PROJECT.md Section 8):
//   - Walk a repo respecting .gitignore (via the `ignore` crate — same crate ripgrep uses)
//   - Do it in parallel (WalkBuilder::build_parallel(), backed by rayon internally)
//   - Hash each file's contents (SHA-256) so the Python side can skip re-embedding
//     unchanged files on subsequent indexing runs — hash-based change detection
//   - Emit a single JSON array on stdout: [{path, size, language, hash, modified}, ...]
//
// Called from Python via subprocess; this binary does no chunking, embedding, or
// storage — that's deliberately left to the Python side (indexer/chunker.py,
// indexer/embedder.py, indexer/store.py). This binary's only job is: find files,
// fast, correctly, in parallel.

use ignore::{WalkBuilder, WalkState};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::UNIX_EPOCH;

#[derive(Serialize)]
struct FileEntry {
    path: String,
    size: u64,
    language: String,
    hash: String,
    modified: u64,
}

/// Maps a file extension to a coarse language label. Deliberately simple —
/// this is just metadata for the chunker to decide which tree-sitter grammar
/// to load; it is not itself doing any parsing.
fn detect_language(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()) {
        Some("py") => "python",
        Some("rs") => "rust",
        Some("js") | Some("jsx") | Some("mjs") => "javascript",
        Some("ts") | Some("tsx") => "typescript",
        Some("md") | Some("markdown") => "markdown",
        Some("json") => "json",
        Some("toml") => "toml",
        Some("yaml") | Some("yml") => "yaml",
        Some("html") | Some("htm") => "html",
        Some("css") => "css",
        Some("c") => "c",
        Some("cpp") | Some("cc") | Some("cxx") => "cpp",
        Some("h") | Some("hpp") => "cpp_header",
        Some("go") => "go",
        Some("java") => "java",
        Some("sh") | Some("bash") => "shell",
        Some("sql") => "sql",
        _ => "unknown",
    }
}

/// Streams the file through SHA-256 in fixed-size chunks rather than reading
/// it fully into memory — matters once this walks a repo with large binary
/// blobs sitting alongside source files.
fn hash_file(path: &Path) -> std::io::Result<String> {
    let mut file = fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 8192];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let repo_path = args.get(1).cloned().unwrap_or_else(|| ".".to_string());

    if !Path::new(&repo_path).exists() {
        eprintln!("error: path does not exist: {}", repo_path);
        std::process::exit(1);
    }

    // Canonicalize once up front so relative-path stripping below is a
    // straightforward prefix match regardless of whether the caller
    // passed a relative (".") or absolute path — the JSON output's
    // "path" field must always be relative to repo_path (Section 8's
    // contract: "file": "src/auth.py", not an absolute path), since
    // that's what indexer/store.py and tools/reader.py key everything
    // off of downstream.
    let repo_root_canon = Arc::new(
        fs::canonicalize(&repo_path).unwrap_or_else(|_| Path::new(&repo_path).to_path_buf())
    );

    let results: Arc<Mutex<Vec<FileEntry>>> = Arc::new(Mutex::new(Vec::new()));

    // build_parallel() spins up a worker per core and walks the directory
    // tree respecting .gitignore/.ignore rules, calling this visitor from
    // multiple threads concurrently. Hashing happens inside the visitor, so
    // both traversal AND hashing are parallelized — not just the walk.
    let walker = WalkBuilder::new(&repo_path).require_git(false).build_parallel();

    walker.run(|| {
        let results = Arc::clone(&results);
        let repo_root_canon = Arc::clone(&repo_root_canon);
        Box::new(move |entry_result| {
            let entry = match entry_result {
                Ok(e) => e,
                Err(_) => return WalkState::Continue,
            };

            let path = entry.path();
            if !path.is_file() {
                return WalkState::Continue;
            }

            let metadata = match fs::metadata(path) {
                Ok(m) => m,
                Err(_) => return WalkState::Continue,
            };

            let modified = metadata
                .modified()
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);

            let hash = match hash_file(path) {
                Ok(h) => h,
                Err(_) => return WalkState::Continue, // unreadable file (permissions, etc.) — skip, don't crash the whole walk
            };

            // Emit a path relative to repo_root, forward-slash-normalized
            // so the same repo indexed on Windows vs. Linux/Mac produces
            // identical "file" metadata — matters for chunk IDs in
            // store.py (hashed from file+lines) staying stable across
            // machines, and for tools/reader.py's read_file, which joins
            // this path onto repo_root and would double up (or silently
            // ignore repo_root entirely, since os.path.join treats an
            // absolute second argument as the whole path) if this were
            // left absolute.
            let path_canon = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
            let relative_path = path_canon
                .strip_prefix(repo_root_canon.as_path())
                .unwrap_or(&path_canon)
                .to_string_lossy()
                .replace('\\', "/");

            let file_entry = FileEntry {
                path: relative_path,
                size: metadata.len(),
                language: detect_language(path).to_string(),
                hash,
                modified,
            };

            results.lock().unwrap().push(file_entry);
            WalkState::Continue
        })
    });

    let final_results = results.lock().unwrap();
    let json = serde_json::to_string(&*final_results).expect("serialization should not fail");
    println!("{}", json);
}
