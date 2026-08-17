//! The review API — a faithful port of v1's endpoints. Same SQL, same JSON
//! shapes, so the TS frontend and the v1 test semantics carry over unchanged.
//!
//! One addition over v1: PAGE-LEVEL approval (`page_review` table). v1's
//! document verdict remains authoritative for the queue states; page approvals
//! are the finer grain M3's pipeline will trigger on ("all pages approved").

use crate::scoring::{self, GATE, HUMAN, MAX_REPEAT, MIN_WORDS, MISTRAL};
use anyhow::Result;
use deadpool_postgres::Pool;
use serde_json::{json, Map, Value};
use std::collections::HashMap;

/// Runs once at startup. Idempotent, like v1's link_page_images DDL: the table
/// appears on first boot, and the backfill seeds page-level approvals from the
/// 22 documents already Final at DOCUMENT level under v1 — attributed to the
/// reviewer who approved them, so they trigger M3's pipeline like any other
/// document instead of being stranded by the model change.
pub async fn migrate(pool: &Pool) -> Result<()> {
    let client = pool.get().await?;
    client.batch_execute(
        "create table if not exists page_review (
           page_id  bigint not null,
           reviewer text   not null,
           status   text   not null,
           ts       timestamptz not null default now(),
           primary key (page_id, reviewer)
         );
         create index if not exists page_review_status_idx
           on page_review (status);
         -- Shared pipeline job table (adjust worker / Loop 2 build runner).
         -- The adjuster carries the same idempotent DDL so whichever starts
         -- first creates it.
         create table if not exists job (
           id bigserial primary key,
           kind text not null,
           document_id bigint not null,
           state text not null default 'queued',
           attempts int not null default 0,
           detail jsonb,
           last_error text,
           created_at timestamptz not null default now(),
           updated_at timestamptz not null default now()
         );
         create index if not exists job_kind_state_idx on job (kind, state);
         create index if not exists job_document_idx on job (document_id);",
    ).await?;
    let backfilled = client.execute(
        "insert into page_review (page_id, reviewer, status)
         select r.page_id, j.key, 'approved'
         from document d
         cross join lateral jsonb_each(coalesce(d.meta->'ocr_review','{}'::jsonb)) j
         join ocr_reading r on r.method = $1
          and (r.meta->>'document_id')::bigint = d.id
         where jsonb_typeof(j.value) = 'object'
           and j.value->>'verdict' = 'approved'
         on conflict (page_id, reviewer) do nothing",
        &[&MISTRAL],
    ).await?;
    if backfilled > 0 {
        tracing::info!(rows = backfilled, "backfilled page_review from v1 document approvals");
    }
    Ok(())
}

fn s(v: Option<&Value>) -> String {
    v.and_then(Value::as_str).unwrap_or("").to_string()
}

/// Port of build_queue: EVERY document, flagged first; the gate decides
/// FLAGGED and the sort, never visibility.
pub async fn queue(pool: &Pool, reviewer: &str) -> Result<Value> {
    let client = pool.get().await?;

    // One reading per page, adjusted-first: after the adjustment worker
    // writes an adjust:reocr:vN / adjust:geometry:vN row, THAT is the machine
    // text under review, not the original Mistral row. DISTINCT ON picks the
    // newest adjusted reading when one exists, else Mistral. Adjust rows carry
    // the same meta.document_id/doc_page, so the join is unchanged.
    let rows = client.query(
        "select d.id, d.key, o.name, r.confidence, r.blocks,
                d.meta->'ocr_review' as review,
                d.meta->'tags' as tags
         from (select distinct on (page_id) *
               from ocr_reading
               where method = $1 or method like 'adjust:%'
               order by page_id, (method like 'adjust:%') desc, ts desc) r
         join document d on d.id = (r.meta->>'document_id')::bigint
         left join output_file o on o.document_id = d.id
                         and o.build_version = 'recut-v2'",
        &[&MISTRAL],
    ).await?;

    // Note + edited-page activity, joined back through the Mistral row because
    // human rows carry only page_id.
    let activity = client.query(
        "select (m.meta->>'document_id')::bigint as did,
                count(*) filter (where coalesce(h.meta->>'note','') <> '') as noted,
                count(*) as edited
         from ocr_reading h
         join ocr_reading m on m.page_id = h.page_id and m.method = $1
         where h.method like $2
         group by 1",
        &[&MISTRAL, &format!("{HUMAN}:%")],
    ).await?;
    let activity: HashMap<i64, (i64, i64)> = activity.iter()
        .map(|r| (r.get::<_, i64>(0), (r.get::<_, i64>(1), r.get::<_, i64>(2))))
        .collect();

    // Page-level approvals per document (v2's finer grain).
    let approvals = client.query(
        "select (m.meta->>'document_id')::bigint as did,
                count(distinct pr.page_id) as pages_approved
         from page_review pr
         join ocr_reading m on m.page_id = pr.page_id and m.method = $1
         where pr.status = 'approved'
         group by 1",
        &[&MISTRAL],
    ).await?;
    let approvals: HashMap<i64, i64> = approvals.iter()
        .map(|r| (r.get::<_, i64>(0), r.get::<_, i64>(1)))
        .collect();

    struct Doc {
        id: i64, key: String, pdf: Option<String>,
        pages: i64, bad: i64, words: i64, tbad: i64, twords: i64,
        max_rep: i64, dup_rows: i64,
        verdict: Option<String>, peers: Map<String, Value>, tags: Value,
    }
    let mut docs: HashMap<i64, Doc> = HashMap::new();

    for row in &rows {
        let did: i64 = row.get(0);
        let conf: Value = row.try_get(3).unwrap_or(Value::Null);
        let blocks: Value = row.try_get(4).unwrap_or(Value::Null);
        let review: Value = row.try_get(5).unwrap_or(Value::Null);
        let tags: Value = row.try_get(6).unwrap_or(Value::Null);

        let d = docs.entry(did).or_insert_with(|| {
            // Every reviewer's verdict travels with the document. Guard the
            // two legacy shapes ({"approved":true} and flat {"verdict":..})
            // exactly as v1 does: only object values under a reviewer key.
            let rv = review.as_object().cloned().unwrap_or_default();
            let mut peers = Map::new();
            let mut mine = None;
            for (who, v) in &rv {
                let Some(obj) = v.as_object() else { continue };
                let Some(verdict) = obj.get("verdict").and_then(Value::as_str) else { continue };
                if who == reviewer {
                    mine = Some(verdict.to_string());
                } else {
                    peers.insert(who.clone(), Value::String(verdict.into()));
                }
            }
            Doc {
                id: did,
                key: s(Some(&Value::String(row.get::<_, String>(1)))),
                pdf: row.try_get::<_, Option<String>>(2).unwrap_or(None),
                pages: 0, bad: 0, words: 0, tbad: 0, twords: 0,
                max_rep: 0, dup_rows: 0,
                verdict: mine, peers,
                tags: if tags.is_array() { tags.clone() } else { json!([]) },
            }
        });

        let words = scoring::page_words(&conf);
        let (b, t) = scoring::score(&words);
        let tw = scoring::table_words(&blocks);
        let (tb, tt) = scoring::score(&tw);
        let (rep, dup) = scoring::repetition(&blocks);
        d.pages += 1;
        d.bad += b;
        d.words += t;
        d.tbad += tb;
        d.twords += tt;
        d.max_rep = d.max_rep.max(rep);
        d.dup_rows += dup;
    }

    let mut out: Vec<Value> = docs.into_values().map(|d| {
        let allw = d.words + d.twords;
        let allb = d.bad + d.tbad;
        let rate = if d.words > 0 { (100.0 * d.bad as f64 / d.words as f64 * 100.0).round() / 100.0 } else { 0.0 };
        let all_rate = if allw > 0 { (100.0 * allb as f64 / allw as f64 * 100.0).round() / 100.0 } else { 0.0 };
        let thin = allw < MIN_WORDS;
        let repeats = d.max_rep >= MAX_REPEAT;
        let flagged = rate > GATE || all_rate > GATE || repeats;
        let worst = rate.max(all_rate);
        let conf_tier = if flagged { "low" } else if worst > 0.5 { "medium" } else { "high" };
        // Effective state across BOTH reviewers: hold trumps approved trumps
        // submitted. A document one person approved and another held is NOT
        // safe to embed.
        let mut verdicts: Vec<&str> = d.peers.values()
            .filter_map(Value::as_str).collect();
        if let Some(v) = &d.verdict { verdicts.push(v); }
        let state = if verdicts.contains(&"hold") { "hold" }
            else if verdicts.contains(&"approved") { "approved" }
            else if verdicts.contains(&"submitted") { "submitted" }
            else { "unreviewed" };
        let (noted, edited) = activity.get(&d.id).copied().unwrap_or((0, 0));
        json!({
            "id": d.id, "key": d.key, "pdf": d.pdf,
            "pages": d.pages, "bad": d.bad, "words": d.words,
            "tbad": d.tbad, "twords": d.twords,
            "maxRep": d.max_rep, "dupRows": d.dup_rows,
            "verdict": d.verdict, "peers": d.peers,
            "tags": d.tags, "noted": noted, "edited": edited,
            "rate": rate, "allRate": all_rate, "thin": thin,
            "repeats": repeats, "flagged": flagged, "conf": conf_tier,
            "state": state,
            "pagesApproved": approvals.get(&d.id).copied().unwrap_or(0),
        })
    }).collect();

    // Flagged first (repetition before rate), then the clean tail by rate.
    out.sort_by(|a, b| {
        let key = |v: &Value| (
            !v["flagged"].as_bool().unwrap_or(false),
            v["thin"].as_bool().unwrap_or(false),
            !v["repeats"].as_bool().unwrap_or(false),
            -v["maxRep"].as_i64().unwrap_or(0),
            -(v["rate"].as_f64().unwrap_or(0.0)
              .max(v["allRate"].as_f64().unwrap_or(0.0)) * 100.0) as i64,
        );
        key(a).cmp(&key(b))
    });
    Ok(Value::Array(out))
}

/// Port of load_doc: all pages of one document — text, suspect spans, saved
/// correction, note, other reviewers' work, editable tables, page approvals.
pub async fn doc(pool: &Pool, did: i64, reviewer: &str) -> Result<Value> {
    let client = pool.get().await?;
    // Adjusted-first, same rule as queue(): the newest adjust:* reading is the
    // page's machine text once the worker has run. r.method rides along so the
    // UI can label provenance.
    let rows = client.query(
        "select r.page_id, r.meta->>'doc_page', r.text, r.confidence, r.blocks,
                r.method
         from (select distinct on (page_id) *
               from ocr_reading
               where (method = $1 or method like 'adjust:%')
                 and (meta->>'document_id')::bigint = $2
               order by page_id, (method like 'adjust:%') desc, ts desc) r
         order by (r.meta->>'doc_page')::int",
        &[&MISTRAL, &did],
    ).await?;
    let pids: Vec<i64> = rows.iter().map(|r| r.get(0)).collect();

    // Corrections + notes, all reviewers, ordered by time.
    let mut corrected: HashMap<i64, String> = HashMap::new();
    let mut corr_tbl: HashMap<i64, Value> = HashMap::new();
    let mut notes: HashMap<i64, String> = HashMap::new();
    let mut others: HashMap<i64, Vec<Value>> = HashMap::new();
    if !pids.is_empty() {
        let hrows = client.query(
            "select page_id, method, text, blocks, meta,
                    to_char(ts, 'YYYY-MM-DD HH24:MI') as when
             from ocr_reading
             where page_id = any($1) and method like $2
             order by ts",
            &[&pids, &format!("{HUMAN}:%")],
        ).await?;
        for r in &hrows {
            let pid: i64 = r.get(0);
            let method: String = r.get(1);
            let text: Option<String> = r.get(2);
            let blocks: Value = r.try_get(3).unwrap_or(Value::Null);
            let meta: Value = r.try_get(4).unwrap_or(Value::Null);
            let when: String = r.get(5);
            let who = method.split_once(':').map(|x| x.1).unwrap_or("unknown")
                .to_lowercase();
            if who == reviewer {
                corrected.insert(pid, text.clone().unwrap_or_default());
                corr_tbl.insert(pid, blocks.get("tables").cloned().unwrap_or(Value::Null));
                notes.insert(pid, s(meta.get("note")));
            } else {
                others.entry(pid).or_default().push(json!({
                    "by": who,
                    "text": text.unwrap_or_default(),
                    "note": s(meta.get("note")),
                    "when": when,
                }));
            }
        }
    }

    // Page-level approvals for these pages.
    let mut page_ok: HashMap<i64, Vec<Value>> = HashMap::new();
    if !pids.is_empty() {
        for r in &client.query(
            "select page_id, reviewer, status from page_review
             where page_id = any($1)", &[&pids]).await?
        {
            let pid: i64 = r.get(0);
            page_ok.entry(pid).or_default().push(json!({
                "by": r.get::<_, String>(1), "status": r.get::<_, String>(2)}));
        }
    }

    let pdf: Option<String> = client.query_opt(
        "select name from output_file where document_id = $1
         and build_version = 'recut-v2' limit 1", &[&did]).await?
        .map(|r| r.get(0));

    let pages: Vec<Value> = rows.iter().map(|r| {
        let pid: i64 = r.get(0);
        let doc_page: i64 = r.get::<_, Option<String>>(1)
            .and_then(|v| v.parse().ok()).unwrap_or(1);
        let text: String = r.get::<_, Option<String>>(2).unwrap_or_default();
        let conf: Value = r.try_get(3).unwrap_or(Value::Null);
        let blocks: Value = r.try_get(4).unwrap_or(Value::Null);

        let words = scoring::page_words(&conf);
        // Suspect-word spans by exact character offset (start_index into the
        // page markdown), so the second occurrence of a word is never
        // mis-marked. Length in Unicode scalar values to match v1's Python.
        let spans: Vec<Value> = words.iter().filter_map(|w| {
            let c = w.get("confidence").and_then(Value::as_f64).unwrap_or(1.0);
            let si = w.get("start_index").and_then(Value::as_i64)?;
            if c >= scoring::BAD_WORD { return None; }
            let wl = w.get("text").and_then(Value::as_str).unwrap_or("")
                .chars().count() as i64;
            Some(json!({"s": si, "e": si + wl, "c": (c * 1000.0).round() / 1000.0}))
        }).collect();
        let (b, t) = scoring::score(&words);

        // Tables: real HTML plus this reviewer's saved edit and the suspect
        // strings inside it (matched by value in the rendered cells).
        let saved_by_id: HashMap<String, Value> = corr_tbl.get(&pid)
            .and_then(Value::as_array).into_iter().flatten()
            .filter_map(|x| Some((s(x.get("id")), x.get("content").cloned()?)))
            .collect();
        let tables: Vec<Value> = blocks.get("tables").and_then(Value::as_array)
            .into_iter().flatten().map(|tb| {
                let tw = tb.get("word_confidence_scores")
                    .and_then(Value::as_array).cloned().unwrap_or_default();
                let mut suspect: Vec<String> = tw.iter().filter_map(|w| {
                    let c = w.get("confidence").and_then(Value::as_f64).unwrap_or(1.0);
                    if c >= scoring::BAD_WORD { return None; }
                    let v = w.get("text").and_then(Value::as_str)?.trim();
                    (!v.is_empty()).then(|| v.to_string())
                }).collect::<std::collections::BTreeSet<_>>()
                    .into_iter().collect();
                suspect.sort();
                let (tbad, twords) = scoring::score(&tw);
                let id = s(tb.get("id"));
                json!({
                    "id": id, "html": s(tb.get("content")),
                    "saved": saved_by_id.get(&id).cloned(),
                    "suspect": suspect, "bad": tbad, "words": twords,
                })
            }).collect();

        // src: which reading this page's machine text came from -- "mistral"
        // or the adjustment method (adjust:reocr:v2 ...). The UI shows it so
        // a reviewer always knows whether they are re-reviewing worker output.
        let method: String = r.get::<_, Option<String>>(5).unwrap_or_default();
        let src = if method == MISTRAL { "mistral".to_string() } else { method };
        json!({
            "pageId": pid, "docPage": doc_page, "text": text, "spans": spans,
            "src": src,
            "corrected": corrected.get(&pid),
            "note": notes.get(&pid).cloned().unwrap_or_default(),
            "others": others.get(&pid).cloned().unwrap_or_default(),
            "tables": tables, "bad": b, "words": t,
            "approvals": page_ok.get(&pid).cloned().unwrap_or_default(),
        })
    }).collect();

    Ok(json!({"id": did, "pdf": pdf, "pages": pages}))
}

/// Port of save_page: DELETE+INSERT of this reviewer's correction row in one
/// transaction. Additive to the corpus — the Mistral rows are never touched,
/// and a correction can be withdrawn by deleting one row.
pub async fn save_page(
    pool: &Pool, page_id: i64, text: &str, tables: &Value, note: &str,
    reviewer: &str,
) -> Result<()> {
    let mut client = pool.get().await?;
    let tx = client.transaction().await?;
    let method = format!("{HUMAN}:{reviewer}");
    tx.execute("delete from ocr_reading where page_id = $1 and method = $2",
               &[&page_id, &method]).await?;
    let has_tables = tables.as_array().map(|a| !a.is_empty()).unwrap_or(false);
    if !text.trim().is_empty() || has_tables || !note.trim().is_empty() {
        tx.execute(
            "insert into ocr_reading (page_id, method, text, blocks, meta)
             values ($1, $2, $3, $4, $5)",
            &[&page_id, &method, &text,
              &json!({"tables": tables}),
              &json!({"source": "ocr_review_app_v2", "by": reviewer,
                      "note": note.trim()})],
        ).await?;
    }
    tx.commit().await?;
    Ok(())
}

/// Port of verdict: stamps document.meta.ocr_review.<reviewer>.verdict.
/// document.state is load-bearing for the pipeline and is not touched.
pub async fn verdict(pool: &Pool, did: i64, value: Option<&str>, reviewer: &str)
    -> Result<()>
{
    let client = pool.get().await?;
    client.execute(
        "update document set meta = coalesce(meta,'{}'::jsonb) ||
           jsonb_build_object('ocr_review',
             coalesce(meta->'ocr_review','{}'::jsonb) -
               'verdict' - 'approved' ||
             jsonb_build_object($1::text,
               jsonb_build_object('verdict', $2::text)))
         where id = $3",
        &[&reviewer, &value, &did],
    ).await?;
    Ok(())
}

/// Port of set_tags: shared document-level tags, deduped case-insensitively,
/// order preserved, last write wins.
pub async fn set_tags(pool: &Pool, did: i64, tags: &[String]) -> Result<()> {
    let mut seen = std::collections::HashSet::new();
    let clean: Vec<&str> = tags.iter().map(|t| t.trim())
        .filter(|t| !t.is_empty() && seen.insert(t.to_lowercase()))
        .collect();
    let client = pool.get().await?;
    client.execute(
        "update document set meta = coalesce(meta,'{}'::jsonb) ||
           jsonb_build_object('tags', $1::jsonb) where id = $2",
        &[&json!(clean), &did],
    ).await?;
    Ok(())
}

/// NEW in v2: page-level review. The 0.2.2 fix made the flow 3-step
/// (Submitted → Approved), because the 2026-08-16 Jeff-incident was him
/// clicking Approve Final when he meant Submit. Server accepts either
/// 'submitted' or 'approved'; None clears the row.
pub async fn page_verdict(pool: &Pool, page_id: i64, status: Option<&str>,
                          reviewer: &str) -> Result<()> {
    let client = pool.get().await?;
    if let Some(s) = status {
        if s != "submitted" && s != "approved" {
            return Err(anyhow::anyhow!("bad page status"));
        }
    }
    match status {
        Some(st) => {
            client.execute(
                "insert into page_review (page_id, reviewer, status)
                 values ($1, $2, $3)
                 on conflict (page_id, reviewer)
                 do update set status = excluded.status, ts = now()",
                &[&page_id, &reviewer, &st],
            ).await?;
        }
        None => {
            client.execute(
                "delete from page_review where page_id = $1 and reviewer = $2",
                &[&page_id, &reviewer],
            ).await?;
        }
    }
    Ok(())
}

/// 0.2.2: the rejection path. A reviewer sends a document back with a reason
/// + optional note + tag. Stored on the document's review meta as
/// verdict='rejected' alongside the reason/note/tag so the round-trip is
/// auditable. Tags dedup case-insensitively against existing tags.
pub async fn reject_doc(
    pool: &Pool, did: i64, reviewer: &str, reason: &str,
    note: &str, tag: Option<&str>,
) -> Result<()> {
    if reason.trim().is_empty() {
        return Err(anyhow::anyhow!("rejection requires a reason"));
    }
    let client = pool.get().await?;

    let mut tags: Vec<String> = Vec::new();
    if let Some(row) = client
        .query_opt("select meta->'tags' from document where id = $1",
        &[&did]).await?
    {
        let v: Option<serde_json::Value> = row.get::<_, Option<serde_json::Value>>(0);
        if let Some(serde_json::Value::Array(arr)) = v {
            for x in arr {
                if let Some(s) = x.as_str() { tags.push(s.to_lowercase()); }
            }
        }
    }
    if let Some(t) = tag {
        let t = t.trim().to_lowercase();
        if !t.is_empty() && !tags.iter().any(|x| x == &t) { tags.push(t); }
    }

    let mut rev = serde_json::Map::new();
    rev.insert("verdict".into(), serde_json::Value::String("rejected".into()));
    rev.insert("reason".into(), serde_json::Value::String(reason.into()));
    if !note.trim().is_empty() {
        rev.insert("note".into(), serde_json::Value::String(note.into()));
    }
    let rev = serde_json::Value::Object(rev);
    let tags = serde_json::json!(tags);

    client.execute(
        "update document set
            meta = coalesce(meta,'{}'::jsonb) ||
              jsonb_build_object('ocr_review',
                coalesce(meta->'ocr_review','{}'::jsonb) ||
                  jsonb_build_object($1::text, $2::jsonb)) ||
              jsonb_build_object('tags', $3::jsonb)
          where id = $4",
        &[&reviewer, &rev, &tags, &did],
    ).await?;
    Ok(())
}
