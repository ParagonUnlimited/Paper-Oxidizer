# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24", "boto3>=1.34",
#                 "mistralai>=1.2", "pillow>=10.0"]
# ///
"""Adjustment worker -- Loop 1 of the Paper-Oxidizer pipeline.

    uv run adjuster.py --once            # one polling pass, then exit
    uv run adjuster.py --doc 1318        # adjust exactly one document (testing)
    uv run adjuster.py                   # poll forever (container mode)

WHAT IT IS. When a reviewer clicks Submit, their edits and notes stop being
messages to a future self and become INSTRUCTIONS to this worker. It reads
the notes and tags on every submitted document, applies the remedy each one
names, writes the results back as NEW readings (originals are never touched),
stamps the document with a system revision badge (v2, v3, ...), and returns
it to the review queue. A note that says the problem is pervasive fans the
same remedy out to sibling documents -- same issuer, same kind -- so one
observation fixes the whole family.

REMEDIES, chosen per page from tags + notes:

  bad-geometry / reading-order   -> local geometry rebuild: re-linearise the
      page text from Mistral's blocks sorted top-to-bottom, left-to-right.
      Free, no API call. New reading method  adjust:geometry:vN
  needs-reocr / repetition /     -> targeted re-OCR: pull THIS page of the HQ
  illegible                         source PDF from R2 (RAW-GENIUS-V2/),
      optionally enhance the raster (grayscale, autocontrast, median denoise
      -- for illegible and repetition, whose root cause is low contrast),
      and run Mistral OCR on just that page. ~$0.005/page.
      New reading method  adjust:reocr:vN

WHAT IT NEVER DOES. It never edits a Mistral row, never touches a human
correction, never deletes anything, and never delivers anywhere -- Loop 2
owns delivery. Every write is additive; a bad adjustment round is undone by
deleting the adjust:* rows and the vN tag.

STATE MACHINE. Candidates are documents whose EFFECTIVE state is 'submitted'
(any reviewer submitted, nobody holds, nobody final-approved). On completion:
the submitted verdicts are cleared (the submission has been CONSUMED -- the
document returns to the queue as unreviewed), stale page approvals on pages
whose text changed are removed, and the vN tag is stamped. The job table
records what was done, page by page, including Mistral cost.

CONCURRENCY. Job claiming is FOR UPDATE SKIP LOCKED on the job table, so a
second worker (or a redeploy mid-run) never double-processes a document.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import traceback

import boto3
import fitz
import psycopg
from botocore.config import Config
from PIL import Image, ImageFilter, ImageOps

MISTRAL_METHOD = "mistral-ocr-4-1"
MISTRAL_MODEL = "mistral-ocr-4-1"          # pinned corpus-wide on 2026-08-12
HUMAN = "human-corrected"
BUILD = "recut-v2"
SOURCE_PREFIX = "RAW-GENIUS-V2"

# Remedy triggers. Tags are exact; note phrases are substring matches over the
# lowercased note text. A tag is a reviewer clicking a preset; a phrase is the
# reviewer writing what they saw -- both are instructions.
GEOMETRY_TAGS = {"bad-geometry", "reading-order"}
REOCR_TAGS = {"needs-reocr", "repetition", "illegible"}
ENHANCE_TAGS = {"illegible", "repetition"}   # low-contrast root causes
GEOMETRY_PHRASES = ("reading order", "out of order", "scrambled", "geometry")
REOCR_PHRASES = ("re-ocr", "reocr", "re ocr", "unreadable", "illegible",
                 "repetition loop", "invented row")
PERVASIVE_PHRASES = ("pervasive", "all similar", "every similar", "same issue on",
                     "across similar", "all of these", "whole batch")

MAX_REOCR_PAGES = int(os.environ.get("ADJUST_MAX_REOCR_PAGES") or 200)
MAX_FANOUT_DOCS = int(os.environ.get("ADJUST_MAX_FANOUT_DOCS") or 40)
POLL_SECONDS = int(os.environ.get("ADJUST_POLL_SECONDS") or 30)
RENDER_DPI = 400


def db():
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        sys.exit("NEON_DATABASE_URL not set")
    return psycopg.connect(url, connect_timeout=30)


_S3 = None


def s3():
    """Lazy: only the re-OCR remedy touches R2. A geometry-only pass (and the
    test suite) must work with no R2 credentials at all -- demanding them up
    front turned 'adjust one document locally' into a config failure."""
    global _S3
    if _S3 is not None:
        return _S3
    for var in ("R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise RuntimeError(f"{var} not set (required for re-OCR)")
    _S3 = boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4",
                      retries={"max_attempts": 5, "mode": "standard"}))
    return _S3


def migrate(cur):
    """The shared pipeline job table (also used by Loop 2's build runner).
    Idempotent; the Rust server carries the same DDL."""
    cur.execute("""
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
        create index if not exists job_document_idx on job (document_id);
    """)


# ---------------------------------------------------------------------------
# candidate discovery + claiming
# ---------------------------------------------------------------------------

def effective_state(review: dict | None) -> str:
    verdicts = set()
    for v in (review or {}).values():
        if isinstance(v, dict) and v.get("verdict"):
            verdicts.add(v["verdict"])
    if "hold" in verdicts:
        return "hold"
    if "approved" in verdicts:
        return "approved"
    if "submitted" in verdicts:
        return "submitted"
    return "unreviewed"


def find_candidates(cur, only_doc: int | None):
    """Documents whose effective state is submitted, with no queued/running
    adjust job. Enqueued as job rows; claiming happens separately so a crash
    between enqueue and claim loses nothing."""
    cur.execute("""
        select d.id, d.meta->'ocr_review'
        from document d
        where d.meta->'ocr_review' is not null
          and not exists (select 1 from job j where j.document_id = d.id
                          and j.kind = 'adjust'
                          and j.state in ('queued','running'))
          -- errored jobs retry, but with a 1-hour backoff -- otherwise a
          -- missing API key would spawn a fresh error job every poll.
          and not exists (select 1 from job j where j.document_id = d.id
                          and j.kind = 'adjust' and j.state = 'error'
                          and j.updated_at > now() - interval '1 hour')
    """)
    enqueued = 0
    for did, review in cur.fetchall():
        if only_doc is not None and did != only_doc:
            continue
        if effective_state(review) != "submitted":
            continue
        cur.execute("insert into job (kind, document_id) values ('adjust', %s)",
                    (did,))
        enqueued += 1
    return enqueued


def claim(cur, only_doc: int | None = None):
    """--doc MODE MUST CONSTRAIN THE CLAIM, NOT JUST THE ENQUEUE. Learned the
    hard way on 2026-08-15: a --doc test run drained the whole queue and
    consumed five of Alden's and Jeff's real submissions, because the filter
    lived only in find_candidates while claim() took anything queued."""
    if only_doc is not None:
        cur.execute("""
            select id, document_id from job
            where kind = 'adjust' and state = 'queued' and document_id = %s
            order by id for update skip locked limit 1
        """, (only_doc,))
    else:
        cur.execute("""
            select id, document_id from job
            where kind = 'adjust' and state = 'queued'
            order by id for update skip locked limit 1
        """)
    row = cur.fetchone()
    if not row:
        return None
    cur.execute("update job set state='running', attempts = attempts + 1, "
                "updated_at = now() where id = %s", (row[0],))
    return row


# ---------------------------------------------------------------------------
# gathering: what did the reviewers ask for?
# ---------------------------------------------------------------------------

def gather(cur, did):
    cur.execute("""
        select r.page_id, (r.meta->>'doc_page')::int, r.text, r.blocks
        from ocr_reading r
        where r.method = %s and (r.meta->>'document_id')::bigint = %s
        order by 2
    """, (MISTRAL_METHOD, did))
    pages = [{"page_id": p, "doc_page": dp, "text": t,
              "blocks": b if isinstance(b, dict) else {}}
             for p, dp, t, b in cur.fetchall()]

    pids = [p["page_id"] for p in pages]
    notes = {}
    if pids:
        cur.execute("""
            select page_id, coalesce(meta->>'note','')
            from ocr_reading
            where page_id = any(%s) and method like %s
        """, (pids, HUMAN + ":%"))
        for pid, note in cur.fetchall():
            if note.strip():
                notes[pid] = (notes.get(pid, "") + "\n" + note).strip()

    cur.execute("select d.meta->'tags', d.meta->'annotation', d.key "
                "from document d where d.id = %s", (did,))
    tags, annotation, key = cur.fetchone()
    tags = [t.lower() for t in tags] if isinstance(tags, list) else []
    cur.execute("select name from output_file where document_id = %s "
                "and build_version = %s limit 1", (did, BUILD))
    row = cur.fetchone()
    return {"id": did, "key": key, "pdf": row[0] if row else None,
            "pages": pages, "notes": notes, "tags": tags,
            "annotation": annotation if isinstance(annotation, dict) else {}}


def decide(doc) -> dict:
    """Map tags + notes to remedies. Returns
    {page_id: {"geometry": bool, "reocr": bool, "enhance": bool}} plus the
    document-wide pervasive flag. Document-level tags apply to every page;
    a page note applies to its page."""
    all_notes = " ".join(doc["notes"].values()).lower()
    tagset = set(doc["tags"])

    doc_geometry = bool(tagset & GEOMETRY_TAGS)
    doc_reocr = bool(tagset & REOCR_TAGS)
    doc_enhance = bool(tagset & ENHANCE_TAGS)
    pervasive = any(p in all_notes for p in PERVASIVE_PHRASES) \
        or "pervasive" in tagset

    plan = {}
    for p in doc["pages"]:
        note = doc["notes"].get(p["page_id"], "").lower()
        geometry = doc_geometry or any(x in note for x in GEOMETRY_PHRASES)
        reocr = doc_reocr or any(x in note for x in REOCR_PHRASES)
        enhance = doc_enhance or "illegible" in note or "repetition" in note
        # A submitted document with NO recognizable instruction still gets the
        # free remedy on noted pages: geometry rebuild is harmless and often
        # what an unstructured complaint is about.
        if note and not (geometry or reocr):
            geometry = True
        if geometry or reocr:
            plan[p["page_id"]] = {"geometry": geometry, "reocr": reocr,
                                  "enhance": enhance and reocr}
    return {"plan": plan, "pervasive": pervasive}


# ---------------------------------------------------------------------------
# remedy 1: geometry rebuild (local, free)
# ---------------------------------------------------------------------------

def geometry_rebuild(blocks_obj: dict) -> str:
    """Re-linearise page text from block geometry: sort blocks into reading
    order top-to-bottom with left-to-right tie-break inside a row band.

    Row banding: two blocks whose vertical overlap exceeds half the shorter
    block's height are 'the same row' and order left-to-right. This handles
    the gardener's-invoice failure (columns interleaved by the OCR's
    linearisation) without trying to be a full column detector."""
    blocks = [b for b in (blocks_obj.get("blocks") or [])
              if isinstance(b, dict) and b.get("type") != "image"
              and (b.get("content") or "").strip()]
    if not blocks:
        return ""

    def box(b):
        return (float(b["top_left_y"]), float(b["top_left_x"]),
                float(b["bottom_right_y"]), float(b["bottom_right_x"]))

    remaining = sorted(blocks, key=lambda b: (box(b)[0], box(b)[1]))
    ordered = []
    while remaining:
        anchor = remaining.pop(0)
        a_top, _, a_bot, _ = box(anchor)
        row = [anchor]
        rest = []
        for b in remaining:
            t, _, bo, _ = box(b)
            overlap = min(a_bot, bo) - max(a_top, t)
            shorter = min(a_bot - a_top, bo - t)
            if shorter > 0 and overlap > 0.5 * shorter:
                row.append(b)
            else:
                rest.append(b)
        row.sort(key=lambda b: box(b)[1])
        ordered.extend(row)
        remaining = rest

    parts = []
    for b in ordered:
        c = (b.get("content") or "").strip()
        parts.append(c)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# remedy 2: targeted re-OCR of one page from the HQ source
# ---------------------------------------------------------------------------

def fetch_source_page(s3c, pdf_name: str, doc_page: int,
                      enhance: bool) -> bytes:
    """One page of the HQ source PDF from R2, as a single-page PDF.

    enhance=True re-rasterises at 400 DPI grayscale with autocontrast and a
    median denoise -- the remedy for low-contrast pages that produced
    repetition loops or 'illegible'. enhance=False copies the page losslessly
    so Mistral sees exactly the original scan at full fidelity."""
    key = f"{SOURCE_PREFIX}/{pdf_name}"
    obj = s3c.get_object(Bucket=os.environ["R2_BUCKET"], Key=key)
    src = fitz.open(stream=obj["Body"].read(), filetype="pdf")
    try:
        idx = max(0, min(len(src) - 1, (doc_page or 1) - 1))
        if not enhance:
            out = fitz.open()
            out.insert_pdf(src, from_page=idx, to_page=idx)
            data = out.tobytes()
            out.close()
            return data
        pix = src[idx].get_pixmap(dpi=RENDER_DPI, colorspace=fitz.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        img = ImageOps.autocontrast(img, cutoff=1)
        img = img.filter(ImageFilter.MedianFilter(3))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out = fitz.open()
        page = out.new_page(width=src[idx].rect.width,
                            height=src[idx].rect.height)
        page.insert_image(page.rect, stream=buf.getvalue())
        data = out.tobytes()
        out.close()
        return data
    finally:
        src.close()


def mistral_reocr(page_pdf: bytes) -> dict:
    """Run Mistral OCR on a single-page PDF; returns the page object
    (markdown, blocks, tables, confidence_scores, dimensions...)."""
    from mistralai import Mistral
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise RuntimeError("MISTRAL_API_KEY not set")
    client = Mistral(api_key=key)
    b64 = base64.b64encode(page_pdf).decode()
    last = None
    for attempt in range(5):
        try:
            resp = client.ocr.process(
                model=MISTRAL_MODEL,
                document={"type": "document_url",
                          "document_url": "data:application/pdf;base64," + b64},
                include_blocks=True,
            )
            raw = resp.model_dump()
            pages = raw.get("pages") or []
            if not pages:
                raise RuntimeError("Mistral returned no pages")
            return pages[0]
        except Exception as e:                                # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Mistral re-OCR failed after retries: {last}")


# ---------------------------------------------------------------------------
# writing results
# ---------------------------------------------------------------------------

def next_revision(tags: list[str]) -> int:
    revs = [int(m.group(1)) for t in tags
            if (m := re.fullmatch(r"v(\d+)", t.strip(), re.I))]
    return max(revs, default=1) + 1


def write_reading(cur, page_id, did, doc_page, method, text, blocks, conf,
                  note):
    """Additive, resumable: UNIQUE (page_id, method) + ON CONFLICT UPDATE so a
    re-run of the same revision refreshes rather than duplicates. meta carries
    document_id/doc_page exactly like Mistral rows so every consumer's SQL
    stays uniform."""
    cur.execute("""
        insert into ocr_reading (page_id, method, text, blocks, confidence, meta)
        values (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
        on conflict (page_id, method) do update
          set text = excluded.text, blocks = excluded.blocks,
              confidence = excluded.confidence, meta = excluded.meta,
              ts = now()
    """, (page_id, method, text,
          json.dumps(blocks) if blocks is not None else None,
          json.dumps(conf) if conf is not None else None,
          json.dumps({"document_id": did, "doc_page": doc_page,
                      "source": "adjustment-worker", "why": note})))


def adjust_document(cur, did, reocr_budget: list[int]) -> dict:
    doc = gather(cur, did)
    decision = decide(doc)
    plan = decision["plan"]
    rev = next_revision(doc["tags"])
    method_geo = f"adjust:geometry:v{rev}"
    method_ocr = f"adjust:reocr:v{rev}"

    summary = {"document_id": did, "revision": rev, "pages": [],
               "pervasive": decision["pervasive"], "reocr_cost_usd": 0.0}
    changed_pages = []
    op_skips: list[str] = []

    for p in doc["pages"]:
        pid = p["page_id"]
        actions = plan.get(pid)
        if not actions:
            continue
        entry = {"page_id": pid, "doc_page": p["doc_page"], "did": []}

        if actions["geometry"]:
            rebuilt = geometry_rebuild(p["blocks"])
            if rebuilt and rebuilt.strip() != (p["text"] or "").strip():
                write_reading(cur, pid, did, p["doc_page"], method_geo,
                              rebuilt, None, None,
                              "geometry rebuild from block boxes")
                entry["did"].append("geometry")
                changed_pages.append(pid)

        if actions["reocr"]:
            if not os.environ.get("MISTRAL_API_KEY"):
                entry["did"].append("reocr-SKIPPED-no-api-key")
                op_skips.append("no-api-key")
            elif reocr_budget[0] <= 0:
                entry["did"].append("reocr-SKIPPED-budget")
                op_skips.append("budget")
            elif not doc["pdf"]:
                entry["did"].append("reocr-SKIPPED-no-source-pdf")
                op_skips.append("no-source-pdf")
            else:
                page_pdf = fetch_source_page(s3(), doc["pdf"], p["doc_page"],
                                             actions["enhance"])
                raw_page = mistral_reocr(page_pdf)
                conf = {"word_confidence_scores":
                        raw_page.get("confidence_scores") or []}
                write_reading(cur, pid, did, p["doc_page"], method_ocr,
                              raw_page.get("markdown") or "", raw_page, conf,
                              "targeted re-OCR"
                              + (" (enhanced raster)" if actions["enhance"]
                                 else " (original page)"))
                reocr_budget[0] -= 1
                summary["reocr_cost_usd"] = round(
                    summary["reocr_cost_usd"] + 0.005, 3)
                entry["did"].append(
                    "reocr+enhance" if actions["enhance"] else "reocr")
                changed_pages.append(pid)

        if entry["did"]:
            summary["pages"].append(entry)

    def consume_submission():
        """Clear every 'submitted' verdict: the submission has been handled
        and the document returns to the queue. Holds and finals untouched."""
        cur.execute("select meta->'ocr_review' from document where id = %s",
                    (did,))
        review = cur.fetchone()[0] or {}
        cleared = {who: v for who, v in review.items()
                   if not (isinstance(v, dict)
                           and v.get("verdict") == "submitted")}
        cur.execute("""
            update document set meta = coalesce(meta,'{}'::jsonb) ||
              jsonb_build_object('ocr_review', %s::jsonb) where id = %s
        """, (json.dumps(cleared), did))

    def add_tag(tag):
        cur.execute("""
            update document set meta = coalesce(meta,'{}'::jsonb) ||
              jsonb_build_object('tags',
                coalesce(meta->'tags','[]'::jsonb) || to_jsonb(%s::text))
            where id = %s and not (coalesce(meta->'tags','[]'::jsonb) ? %s)
        """, (tag, did, tag))

    # THREE OUTCOMES, each with different verdict semantics (2026-08-15
    # incident: the old unconditional consume ate four real submissions that
    # received no remedy at all).
    if changed_pages:
        # Remedied: stale approvals fall (text changed under them), vN badge
        # stamped, submission consumed -> re-review the worker's output.
        cur.execute("delete from page_review where page_id = any(%s)",
                    (list(set(changed_pages)),))
        add_tag(f"v{rev}")
        consume_submission()
    elif op_skips:
        # Wanted to act but operationally couldn't (no API key, budget, no
        # source PDF). The submission MUST survive: leave it, mark the job
        # error, and a later pass retries when the condition clears.
        summary["retryable"] = "reocr skipped: " + ", ".join(sorted(set(op_skips)))
    else:
        # Nothing to do -- edits-only submission or an unmappable note. The
        # submission is consumed (the reviewer's own correction already IS the
        # fix; Loop 2 will build from it), and 'adjust-noop' tells them the
        # worker changed no machine text. No vN: nothing machine-side changed.
        add_tag("adjust-noop")
        consume_submission()

    summary["pages_changed"] = len(set(changed_pages))
    return summary


def fanout(cur, did, summary) -> list[int]:
    """Pervasive note: enqueue adjust jobs for sibling documents -- same
    issuer + doc_kind, not final, not held, no active job. The sibling run
    re-reads ITS OWN tags/notes; if it has none, the document-level tags this
    fanout copies (the remedy tags only, never vN) drive the same remedy."""
    # NULL guard is load-bearing: with IS NOT DISTINCT FROM, a document with
    # issuer=NULL would 'match' every other NULL-issuer document and a single
    # pervasive note could fan out to most of the corpus. No identity, no
    # fanout.
    cur.execute("""
        select d2.id
        from document d1
        join document d2 on d2.id <> d1.id
          and d2.issuer = d1.issuer
          and d2.doc_kind = d1.doc_kind
        where d1.id = %s
          and d1.issuer is not null and d1.issuer <> ''
          and d1.doc_kind is not null and d1.doc_kind <> ''
        limit %s
    """, (did, MAX_FANOUT_DOCS))
    siblings = [r[0] for r in cur.fetchall()]
    queued = []
    for sid in siblings:
        cur.execute("select meta->'ocr_review', meta->'tags' from document "
                    "where id = %s", (sid,))
        review, tags = cur.fetchone()
        if effective_state(review) in ("approved", "hold"):
            continue
        cur.execute("""
            select 1 from job where document_id = %s and kind = 'adjust'
            and state in ('queued','running')""", (sid,))
        if cur.fetchone():
            continue
        # Copy the remedy tags from the pervasive doc so decide() fires.
        remedy = [t for t in summary.get("copied_tags", [])
                  if t in (GEOMETRY_TAGS | REOCR_TAGS)]
        if remedy:
            existing = [t for t in tags] if isinstance(tags, list) else []
            merged = existing + [t for t in remedy if t not in existing]
            cur.execute("""
                update document set meta = coalesce(meta,'{}'::jsonb) ||
                  jsonb_build_object('tags', %s::jsonb) where id = %s
            """, (json.dumps(merged), sid))
        cur.execute("insert into job (kind, document_id, detail) "
                    "values ('adjust', %s, %s::jsonb)",
                    (sid, json.dumps({"fanout_from": did})))
        queued.append(sid)
    return queued


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def run_pass(only_doc: int | None) -> int:
    """One full pass: enqueue candidates, then drain the queue. Returns the
    number of documents processed."""
    processed = 0
    with db() as con:
        with con.cursor() as cur:
            migrate(cur)
            n = find_candidates(cur, only_doc)
            con.commit()
            if n:
                print(f"enqueued {n} submitted document(s)")

    reocr_budget = [MAX_REOCR_PAGES]
    while True:
        with db() as con:
            with con.cursor() as cur:
                job = claim(cur, only_doc)
                con.commit()
                if not job:
                    break
                job_id, did = job
                try:
                    summary = adjust_document(cur, did, reocr_budget)
                    # Fanout after the primary doc so its tags are readable.
                    if summary["pervasive"]:
                        cur.execute("select meta->'tags' from document "
                                    "where id = %s", (did,))
                        t = cur.fetchone()[0]
                        summary["copied_tags"] = (
                            [x.lower() for x in t] if isinstance(t, list) else [])
                        summary["fanned_out_to"] = fanout(cur, did, summary)
                    if summary.get("retryable"):
                        cur.execute("""update job set state='error',
                                       detail=%s::jsonb, last_error=%s,
                                       updated_at=now() where id=%s""",
                                    (json.dumps(summary),
                                     summary["retryable"], job_id))
                        con.commit()
                        print(f"doc {did}: retryable, submission preserved "
                              f"({summary['retryable']})")
                        continue
                    cur.execute("""update job set state='done', detail=%s::jsonb,
                                   updated_at=now() where id=%s""",
                                (json.dumps(summary), job_id))
                    con.commit()
                    processed += 1
                    print(f"doc {did}: rev v{summary['revision']}, "
                          f"{summary['pages_changed']} page(s) changed, "
                          f"${summary['reocr_cost_usd']} re-OCR"
                          + (f", fanout {summary.get('fanned_out_to')}"
                             if summary.get("fanned_out_to") else ""))
                except Exception as e:                        # noqa: BLE001
                    con.rollback()
                    with con.cursor() as c2:
                        c2.execute("""update job set state='error',
                                      last_error=%s, updated_at=now()
                                      where id=%s""",
                                   (f"{type(e).__name__}: {e}"[:500], job_id))
                    con.commit()
                    print(f"doc {did} FAILED: {e}")
                    traceback.print_exc()
    return processed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--doc", type=int, help="process exactly this document id")
    args = ap.parse_args()

    if args.doc is not None or args.once:
        n = run_pass(args.doc)
        print(f"pass complete: {n} document(s) adjusted")
        return

    print(f"adjustment worker: polling every {POLL_SECONDS}s "
          f"(re-OCR cap {MAX_REOCR_PAGES} pages/pass)")
    while True:
        try:
            run_pass(None)
        except Exception as e:                                # noqa: BLE001
            print(f"pass error: {e}")
            traceback.print_exc()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
