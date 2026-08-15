//! Quality scoring — a faithful port of v1's measured, tuned rules. The
//! numbers in these comments are corpus measurements, not guesses; changing a
//! threshold here silently changes which of 1,464 probate documents a human
//! is asked to look at.

use regex::Regex;
use serde_json::Value;
use std::sync::LazyLock;

/// A word below this confidence is "suspect".
pub const BAD_WORD: f64 = 0.60;
/// % of suspect words that flags a document for review. Measured: >2% flags
/// 256 documents; minimum-confidence@0.90 flagged 1,330 of 1,464 (useless).
pub const GATE: f64 = 2.0;
/// Below this many scored words a percentage is noise, not a signal.
pub const MIN_WORDS: i64 = 20;
/// Consecutive identical non-blank table rows = a generation loop. run>=4
/// catches every confirmed loop (runs of 20, 12, 10, 10) at 10 flagged docs;
/// counting occurrences anywhere flagged 54, mostly legitimate line items.
pub const MAX_REPEAT: i64 = 4;

pub const MISTRAL: &str = "mistral-ocr-4-1";
pub const HUMAN: &str = "human-corrected";

static TR: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<tr>(.*?)</tr>").unwrap());
static CELL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"<[^>]*>|&nbsp;|\s").unwrap());

fn confidence(w: &Value) -> f64 {
    w.get("confidence").and_then(Value::as_f64).unwrap_or(1.0)
}

/// (suspect, total) over a word_confidence_scores array.
pub fn score(words: &[Value]) -> (i64, i64) {
    let bad = words.iter().filter(|w| confidence(w) < BAD_WORD).count() as i64;
    (bad, words.len() as i64)
}

pub fn page_words(conf: &Value) -> Vec<Value> {
    conf.get("word_confidence_scores")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

/// Table words are scored SEPARATELY by the API and are invisible in the page
/// markdown (it carries only a [tbl-N.html] placeholder). 784 of 1,762 pages
/// have one; ~18% of the corpus's words live in them.
pub fn table_words(blocks: &Value) -> Vec<Value> {
    let mut out = Vec::new();
    if let Some(tables) = blocks.get("tables").and_then(Value::as_array) {
        for t in tables {
            if let Some(ws) = t.get("word_confidence_scores").and_then(Value::as_array) {
                out.extend(ws.iter().cloned());
            }
        }
    }
    out
}

/// (longest consecutive run of one non-blank row, total duplicated rows).
///
/// A SECOND, INDEPENDENT failure mode: a repetition loop emits words the model
/// is entirely sure of, over and over — the worst case repeats one row 35
/// times at 0.0% suspect words, invisible to confidence. Blank rows are
/// ignored (printed ruled lines), and only CONSECUTIVE runs count (receipts
/// legitimately repeat items non-adjacently).
pub fn repetition(blocks: &Value) -> (i64, i64) {
    let (mut worst, mut dups) = (0i64, 0i64);
    let Some(tables) = blocks.get("tables").and_then(Value::as_array) else {
        return (0, 0);
    };
    for t in tables {
        let content = t.get("content").and_then(Value::as_str).unwrap_or("");
        let rows: Vec<&str> = TR
            .captures_iter(content)
            .map(|c| c.get(1).unwrap().as_str())
            .filter(|r| !CELL.replace_all(r, "").is_empty())
            .collect();
        if rows.is_empty() {
            continue;
        }
        let mut run = 1i64;
        let mut prev: Option<&str> = None;
        let mut counts = std::collections::HashMap::<&str, i64>::new();
        for r in &rows {
            *counts.entry(r).or_insert(0) += 1;
            run = if prev == Some(r) { run + 1 } else { 1 };
            prev = Some(r);
            worst = worst.max(run);
        }
        dups += counts.values().filter(|&&n| n > 1).map(|n| n - 1).sum::<i64>();
    }
    (worst, dups)
}
