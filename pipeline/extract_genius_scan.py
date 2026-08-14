# /// script
# requires-python = ">=3.10"
# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24"]
# ///
"""Pull the Genius Scan text layer out of every recut PDF and into Neon.

    uv run extract_genius_scan.py --dry     # verify mapping, write nothing
    uv run extract_genius_scan.py           # write genius_scan_v2 rows

WHY THIS EXISTS
---------------
Every recut PDF still carries the text layer Genius Scan produced when the page
was originally scanned. It is poor text ("San Jos0 Water Campay") but it is the
ONLY copy that exists: measured 2026-08-14, zero of the 1,762 v2 pages have a
genius_scan_v1 row in Neon -- all 2,327 legacy readings attach to the old
v1-merged files, which describe different images.

embed_ocr.py deletes that layer (strip_text) before writing the Mistral text in.
That delete is irreversible and the bytes exist nowhere else, so this script
copies them into Neon first. Nothing consumes the result. It is archival
insurance on a one-way door, and it is cheap.

SAFETY
------
Additive only. Writes a NEW method ('genius_scan_v2'); never touches the Mistral
rows or any human correction. ocr_reading has UNIQUE (page_id, method), so
re-running is a no-op on pages already captured -- the script is resumable and
safe to run twice.

Every page gets a row, including pages whose text layer is empty, because
"we looked and there was nothing" is itself a fact worth keeping.
"""
import argparse
import os
import sys
from collections import Counter

import fitz
import psycopg

MISTRAL = "mistral-ocr-4-1"
METHOD = "genius_scan_v2"
BUILD = "recut-v2"

DEFAULT_RECUT = r"C:\Users\busin\Documents\Document Splitting for Paperless\recut"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="verify only, write nothing")
    ap.add_argument("--recut", default=os.environ.get("PAGE_SOURCE") or DEFAULT_RECUT)
    args = ap.parse_args()

    if not os.path.isdir(args.recut):
        sys.exit("recut folder not found: %s" % args.recut)

    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        sys.exit("NEON_DATABASE_URL is not set")

    con = psycopg.connect(url, connect_timeout=30)
    cur = con.cursor()

    # One row per Mistral page, carrying the PDF it came from and its 1-based
    # index inside that PDF. This is the same mapping the review app uses to
    # render page images, so it is already proven against the real corpus.
    cur.execute("""
        select r.page_id,
               (r.meta->>'document_id')::bigint  as document_id,
               (r.meta->>'doc_page')::int        as doc_page,
               o.name
        from ocr_reading r
        join output_file o
          on o.document_id = (r.meta->>'document_id')::bigint
         and o.build_version = %s
        where r.method = %s
        order by o.name, (r.meta->>'doc_page')::int
    """, (BUILD, MISTRAL))
    rows = cur.fetchall()

    cur.execute("select count(*) from ocr_reading where method = %s", (MISTRAL,))
    total_mistral = cur.fetchone()[0]

    print("mistral pages          : %d" % total_mistral)
    print("mapped to a recut PDF  : %d" % len(rows))
    if len(rows) != total_mistral:
        print("  !! %d Mistral pages have no recut-v2 output_file row"
              % (total_mistral - len(rows)))

    cur.execute("select count(*) from ocr_reading where method = %s", (METHOD,))
    already = cur.fetchone()[0]
    print("already captured       : %d" % already)
    print()

    # ---- verify lineage: does every page actually exist in its PDF? ----
    by_pdf = {}
    for page_id, did, dp, name in rows:
        by_pdf.setdefault(name, []).append((page_id, did, dp))

    missing_pdf, page_oob = [], []
    for name, items in by_pdf.items():
        p = os.path.join(args.recut, name)
        if not os.path.exists(p):
            missing_pdf.append(name)
    print("distinct recut PDFs referenced : %d" % len(by_pdf))
    print("referenced PDFs missing on disk: %d" % len(missing_pdf))
    if missing_pdf:
        for n in missing_pdf[:5]:
            print("   %s" % n)

    if args.dry:
        print("\n--dry: verifying page counts and sampling text, writing nothing\n")

    stats = Counter()
    sample_shown = 0
    written = 0
    BATCH = 200
    pending = []

    for i, (name, items) in enumerate(sorted(by_pdf.items()), 1):
        path = os.path.join(args.recut, name)
        if not os.path.exists(path):
            stats["pdf_missing"] += len(items)
            continue
        try:
            doc = fitz.open(path)
        except Exception as e:
            stats["pdf_open_failed"] += len(items)
            print("  OPEN FAILED %s: %s" % (name[:50], str(e)[:60]))
            continue

        for page_id, did, dp in items:
            idx = (dp or 1) - 1
            if idx < 0 or idx >= len(doc):
                page_oob.append((name, dp, len(doc)))
                stats["page_out_of_range"] += 1
                continue
            try:
                text = doc[idx].get_text() or ""
            except Exception:
                stats["extract_failed"] += 1
                continue
            stats["pages_read"] += 1
            if text.strip():
                stats["with_text"] += 1
            else:
                stats["empty_layer"] += 1

            if args.dry:
                if text.strip() and sample_shown < 3:
                    sample_shown += 1
                    print("  sample %s p%d: %r" % (name[:40], dp, text[:110]))
                continue

            pending.append((page_id, METHOD, text,
                            psycopg.types.json.Jsonb({
                                "chars": len(text),
                                "source_pdf": name,
                                "doc_page": dp,
                                "document_id": did,
                                "captured_by": "extract_genius_scan.py",
                                "why": "archival copy before embed_ocr.strip_text()",
                            })))
            if len(pending) >= BATCH:
                written += flush(cur, con, pending)
                pending.clear()
        doc.close()
        if i % 200 == 0:
            print("  %d/%d PDFs" % (i, len(by_pdf)))

    if pending and not args.dry:
        written += flush(cur, con, pending)

    print()
    print("pages read       : %d" % stats["pages_read"])
    print("  with text      : %d" % stats["with_text"])
    print("  EMPTY layer    : %d" % stats["empty_layer"])
    for k in ("pdf_missing", "pdf_open_failed", "page_out_of_range", "extract_failed"):
        if stats[k]:
            print("  %-14s : %d" % (k, stats[k]))
    if page_oob:
        print("  out-of-range examples: %s" % page_oob[:3])

    if not args.dry:
        cur.execute("select count(*) from ocr_reading where method = %s", (METHOD,))
        print("\nrows inserted this run : %d" % written)
        print("genius_scan_v2 total   : %d" % cur.fetchone()[0])
    con.close()


def flush(cur, con, pending):
    """Insert a batch. ON CONFLICT makes the whole script resumable."""
    cur.executemany("""
        insert into ocr_reading (page_id, method, text, meta)
        values (%s, %s, %s, %s)
        on conflict (page_id, method) do nothing
    """, pending)
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(pending)
    con.commit()
    return n


if __name__ == "__main__":
    main()
