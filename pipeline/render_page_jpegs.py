# /// script
# requires-python = ">=3.10"
# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24"]
# ///
"""Render every page to a 300 DPI JPEG for Cloudflare R2.

    uv run render_page_jpegs.py --dry
    uv run render_page_jpegs.py                 # resumable; skips what exists

WHY: the review app currently rasterises pages on demand from 3.09 GB of local
PDFs. That cannot be served to a remote reviewer -- one document is 118 MB, and
sending a 47-page PDF to display page 4 is the real latency cost, more than
resolution is. Pre-rendering one JPEG per page turns every page view into a
single small GET from R2.

NAMED BY page_id. That is the primary key the app already uses to fetch text,
confidence and corrections, so the image lands in the same lookup with no extra
mapping table on the read path.

SAME DIMENSIONS: every page is rendered at exactly 300 DPI with no cropping and
no forced aspect, so the JPEG's pixel box is an exact scalar multiple of the PDF
page box. That is what lets Mistral's bounding boxes be drawn over the image --
a box at (x, y) in page coordinates maps to (x * k, y * k) in the JPEG for a
single k per page. Any crop or letterbox would silently break that overlay.
"""
import argparse
import io
import json
import os
import sys
from collections import Counter

import fitz
import psycopg

MISTRAL = "mistral-ocr-4-1"
BUILD = "recut-v2"
DEFAULT_RECUT = r"C:\Users\busin\Documents\Document Splitting for Paperless\recut"
DEFAULT_OUT = r"C:\Users\busin\Documents\Document Splitting for Paperless\pages-r2"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--quality", type=int, default=82)
    ap.add_argument("--recut", default=os.environ.get("PAGE_SOURCE") or DEFAULT_RECUT)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    if not os.path.isdir(args.recut):
        sys.exit("recut folder not found: %s" % args.recut)
    os.makedirs(args.out, exist_ok=True)

    con = psycopg.connect(os.environ["NEON_DATABASE_URL"], connect_timeout=30)
    cur = con.cursor()
    cur.execute("""
        select r.page_id,
               (r.meta->>'document_id')::bigint,
               (r.meta->>'doc_page')::int,
               o.name
        from ocr_reading r
        join output_file o
          on o.document_id = (r.meta->>'document_id')::bigint
         and o.build_version = %s
        where r.method = %s
        order by o.name, (r.meta->>'doc_page')::int
    """, (BUILD, MISTRAL))
    rows = cur.fetchall()
    con.close()
    print("pages to render : %d   dpi=%d q=%d" % (len(rows), args.dpi, args.quality))
    print("output          : %s" % args.out)

    by_pdf = {}
    for page_id, did, dp, name in rows:
        by_pdf.setdefault(name, []).append((page_id, did, dp))

    zoom = args.dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    stats = Counter()
    manifest = []
    dims = Counter()

    for i, (name, items) in enumerate(sorted(by_pdf.items()), 1):
        path = os.path.join(args.recut, name)
        if not os.path.exists(path):
            stats["pdf_missing"] += len(items)
            continue
        try:
            doc = fitz.open(path)
        except Exception as e:
            stats["open_failed"] += len(items)
            print("  OPEN FAILED %s: %s" % (name[:45], str(e)[:60]))
            continue

        for page_id, did, dp in items:
            idx = (dp or 1) - 1
            if idx < 0 or idx >= len(doc):
                stats["out_of_range"] += 1
                continue
            dest = os.path.join(args.out, "%d.jpg" % page_id)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                stats["skipped_existing"] += 1
                continue
            if args.dry:
                stats["would_render"] += 1
                continue
            try:
                page = doc[idx]
                pix = page.get_pixmap(matrix=mat, alpha=False)
                pix.save(dest, jpg_quality=args.quality)
                stats["rendered"] += 1
                stats["bytes"] += os.path.getsize(dest)
                dims["%dx%d" % (pix.width, pix.height)] += 1
                manifest.append({
                    "page_id": page_id, "document_id": did, "doc_page": dp,
                    "key": "pages/%d.jpg" % page_id,
                    "w": pix.width, "h": pix.height,
                    "bytes": os.path.getsize(dest),
                    "dpi": args.dpi,
                    # scale factor from PDF points to JPEG pixels, for overlays
                    "pt_to_px": round(pix.width / page.rect.width, 6),
                    "pdf": name,
                })
            except Exception as e:
                stats["render_failed"] += 1
                print("  RENDER FAILED %s p%s: %s" % (name[:40], dp, str(e)[:60]))
        doc.close()
        if i % 100 == 0:
            mb = stats["bytes"] / 1048576.0
            print("  %d/%d PDFs   rendered=%d  %.0f MB"
                  % (i, len(by_pdf), stats["rendered"], mb))

    if manifest:
        mf = os.path.join(args.out, "_manifest.json")
        # append-safe: merge with anything a previous run wrote
        old = []
        if os.path.exists(mf):
            try:
                old = json.load(io.open(mf, encoding="utf-8"))
            except Exception:
                old = []
        seen = {m["page_id"] for m in manifest}
        merged = manifest + [m for m in old if m.get("page_id") not in seen]
        json.dump(merged, io.open(mf, "w", encoding="utf-8"), indent=1)
        print("\nwrote %s (%d entries)" % (mf, len(merged)))

    print()
    for k, v in sorted(stats.items()):
        if k == "bytes":
            print("  %-18s %.1f MB" % ("total size", v / 1048576.0))
        else:
            print("  %-18s %d" % (k, v))
    if stats["rendered"]:
        print("  %-18s %.0f KB" % ("mean per page",
                                   stats["bytes"] / stats["rendered"] / 1024.0))
    if dims:
        print("\n  distinct pixel dimensions: %d" % len(dims))
        for d, n in dims.most_common(6):
            print("     %-14s %d pages" % (d, n))


if __name__ == "__main__":
    main()
