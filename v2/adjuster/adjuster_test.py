# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24", "boto3>=1.34",
#                 "mistralai>=1.2", "pillow>=10.0"]
# ///
"""Loop 1 end-to-end against live Neon + R2, leaving no trace.

Stages a real submission on a quiet document (bad-geometry tag + a note +
verdict=submitted), runs the worker for exactly that document, and asserts the
whole contract:

  - an adjust:geometry:v2 reading exists with the reordered text
  - the v2 revision badge is stamped, exactly once
  - the submitted verdict was consumed (doc back to unreviewed)
  - stale page approvals on changed pages were removed
  - the job row is 'done' and its detail names the remedy per page
  - the app's queue/doc view would serve the adjusted reading (DISTINCT ON
    check run as SQL, same expression the Rust server uses)
  - re-OCR was NOT triggered (no MISTRAL spend from a test)

Then deletes everything it created and restores the document byte-for-byte.
"""
import json
import os
import subprocess
import sys

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
MISTRAL = "mistral-ocr-4-1"

ok = fail = 0
def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %s" % label)
    else:
        fail += 1
        print("  FAIL  %s   %s" % (label, str(detail)[:200]))


con = psycopg.connect(os.environ["NEON_DATABASE_URL"], connect_timeout=30)
con.autocommit = True
cur = con.cursor()

# ---- pick a victim: unreviewed, multi-block first page, no tags ------------
cur.execute("""
    select d.id, d.meta
    from document d
    where d.meta->'ocr_review' is null
      and (d.meta->'tags' is null or d.meta->'tags' = '[]'::jsonb)
    order by d.id desc limit 50
""")
victim = None
for did, meta in cur.fetchall():
    cur.execute("""select r.page_id, jsonb_array_length(r.blocks->'blocks')
                   from ocr_reading r
                   where r.method = %s
                     and (r.meta->>'document_id')::bigint = %s
                   order by (r.meta->>'doc_page')::int limit 1""",
                (MISTRAL, did))
    row = cur.fetchone()
    if row and (row[1] or 0) >= 6:
        victim = (did, meta, row[0])
        break
if not victim:
    sys.exit("no suitable victim document found")
did, orig_meta, page_id = victim
print(f"victim: document {did}, page {page_id}")
orig_meta_json = json.dumps(orig_meta)

cur.execute("select count(*) from page_review where page_id = %s", (page_id,))
had_page_reviews = cur.fetchone()[0]
# Legitimate adjust rows exist in the corpus (real submissions get processed);
# leftover-detection is therefore snapshot-vs-after, not assume-zero.
cur.execute("select count(*) from ocr_reading where method like 'adjust:%'")
adjust_rows_before = cur.fetchone()[0]

try:
    # ---- stage the submission ----------------------------------------------
    cur.execute("""update document set meta = coalesce(meta,'{}'::jsonb) ||
        jsonb_build_object(
          'tags', '["bad-geometry"]'::jsonb,
          'ocr_review', '{"testbot":{"verdict":"submitted"}}'::jsonb)
        where id = %s""", (did,))
    # a page approval that must be invalidated by the text change
    cur.execute("""insert into page_review (page_id, reviewer, status)
                   values (%s, 'testbot', 'approved')
                   on conflict (page_id, reviewer) do update set status='approved'""",
                (page_id,))
    # a note naming the problem (also exercises note-driven detection)
    cur.execute("""insert into ocr_reading (page_id, method, text, meta)
                   values (%s, 'human-corrected:testbot', '', %s::jsonb)
                   on conflict (page_id, method) do update set meta = excluded.meta""",
                (page_id, json.dumps({"source": "adjuster_test",
                                      "by": "testbot",
                                      "note": "reading order is scrambled"})))

    # ---- run the worker for exactly this document --------------------------
    # Snapshot other documents' state first: on 2026-08-15 a --doc run drained
    # the whole queue and consumed five REAL submissions. Never again.
    cur.execute("""select count(*) from job where kind='adjust'
                   and document_id <> %s""", (did,))
    other_jobs_before = cur.fetchone()[0]
    cur.execute("""select count(*) from document
                   where id <> %s and meta->'ocr_review' @>
                     '{}'::jsonb and exists (
                       select 1 from jsonb_each(meta->'ocr_review') e
                       where e.value->>'verdict' = 'submitted')""", (did,))
    other_submitted_before = cur.fetchone()[0]

    env = dict(os.environ)
    env.pop("MISTRAL_API_KEY", None)      # a test must never spend money
    r = subprocess.run(
        ["uv", "run", os.path.join(HERE, "adjuster.py"), "--doc", str(did)],
        capture_output=True, text=True, timeout=600, env=env)
    print(r.stdout.strip()[-400:])
    check("worker exited 0", r.returncode == 0, r.stderr[-300:])

    cur.execute("""select count(*) from job where kind='adjust'
                   and document_id <> %s""", (did,))
    check("ISOLATION: no jobs created/processed for other documents",
          cur.fetchone()[0] == other_jobs_before)
    cur.execute("""select count(*) from document
                   where id <> %s and meta->'ocr_review' @>
                     '{}'::jsonb and exists (
                       select 1 from jsonb_each(meta->'ocr_review') e
                       where e.value->>'verdict' = 'submitted')""", (did,))
    check("ISOLATION: other documents' submissions untouched",
          cur.fetchone()[0] == other_submitted_before)

    # ---- assertions ---------------------------------------------------------
    cur.execute("""select text from ocr_reading
                   where page_id = %s and method = 'adjust:geometry:v2'""",
                (page_id,))
    row = cur.fetchone()
    check("adjust:geometry:v2 reading written", row is not None)
    if row:
        check("rebuilt text is non-trivial", len(row[0] or "") > 100,
              len(row[0] or ""))

    cur.execute("select meta->'tags', meta->'ocr_review' from document where id=%s",
                (did,))
    tags, review = cur.fetchone()
    check("v2 badge stamped exactly once",
          isinstance(tags, list) and tags.count("v2") == 1, tags)
    check("submitted verdict consumed",
          not any(isinstance(v, dict) and v.get("verdict") == "submitted"
                  for v in (review or {}).values()), review)

    cur.execute("""select count(*) from page_review
                   where page_id = %s and reviewer = 'testbot'""", (page_id,))
    check("stale page approval removed", cur.fetchone()[0] == 0)

    cur.execute("""select state, detail from job
                   where kind='adjust' and document_id = %s
                   order by id desc limit 1""", (did,))
    state, detail = cur.fetchone()
    check("job done", state == "done", state)
    pages_detail = (detail or {}).get("pages", [])
    check("job detail names the remedy",
          any("geometry" in p.get("did", []) for p in pages_detail), detail)
    check("no re-OCR happened (no key, no spend)",
          (detail or {}).get("reocr_cost_usd", 0) == 0
          and not any("reocr" == a for p in pages_detail
                      for a in p.get("did", [])), detail)

    # the server's DISTINCT ON view now serves the adjusted text
    cur.execute("""
        select method from (
          select distinct on (page_id) *
          from ocr_reading
          where (method = %s or method like 'adjust:%%')
            and page_id = %s
          order by page_id, (method like 'adjust:%%') desc, ts desc) r
    """, (MISTRAL, page_id))
    check("app view serves the adjusted reading",
          cur.fetchone()[0] == "adjust:geometry:v2")

finally:
    # ---- restore everything. Each step independent: one missing table must
    # never leave the victim document carrying test state. -------------------
    def cleanup(sql, args):
        try:
            cur.execute(sql, args)
        except psycopg.Error as e:
            print("  cleanup step failed (continuing):", str(e)[:120])

    # DOCUMENT-wide, not page-wide: a document-level bad-geometry tag makes
    # the worker adjust EVERY page, so cleaning only the staged page left 7
    # orphan readings on the first run. Delete by document_id.
    cleanup("delete from ocr_reading where method like 'adjust:%%' "
            "and (meta->>'document_id')::bigint = %s", (did,))
    cleanup("delete from ocr_reading where page_id = %s "
            "and method = 'human-corrected:testbot'", (page_id,))
    cleanup("delete from page_review where reviewer='testbot' and page_id in "
            "(select page_id from ocr_reading where method = %s "
            " and (meta->>'document_id')::bigint = %s)", (MISTRAL, did))
    cleanup("delete from job where kind='adjust' and document_id = %s", (did,))
    cleanup("update document set meta = %s::jsonb where id = %s",
            (orig_meta_json, did))

    cur.execute("select meta from document where id = %s", (did,))
    restored = cur.fetchone()[0]
    # Corpus-wide leftover check against the pre-test snapshot -- the first
    # run's lesson, adapted to a corpus where legitimate adjust rows live.
    cur.execute("select count(*) from ocr_reading where method like 'adjust:%'")
    leftovers = cur.fetchone()[0] - adjust_rows_before
    cur.execute("select count(*) from page_review where page_id = %s", (page_id,))
    pr_now = cur.fetchone()[0]
    check("document meta restored byte-for-byte",
          json.dumps(restored, sort_keys=True) == json.dumps(orig_meta,
                                                             sort_keys=True))
    check("no adjust readings left behind", leftovers == 0, leftovers)
    check("page_review count back to original", pr_now == had_page_reviews,
          (pr_now, had_page_reviews))

con.close()
print()
print("PASS %d   FAIL %d" % (ok, fail))
sys.exit(1 if fail else 0)
