# Review this change for security vulnerabilities.

- **Session:** `3f44381b-42bf-4b8e-a0c9-98e75e8c0a66`
- **Span:** 2026-08-14T22:56:02.589Z → 2026-08-14T22:59:25.219Z
- **Messages:** 1 user · 7 assistant
- **Source:** raw JSONL kept locally in `_session-recovery-backup/` (not in git — contains credentials). This file is the scrubbed conversation: all prose, tool calls as one-line markers, tool results omitted.

---

## 👤 User

Review this change for security vulnerabilities.

Changed files (you may Read these and any other file in the repo):
  - pipeline/extract_genius_scan.py
  - pipeline/link_page_images.py
  - pipeline/render_page_jpegs.py
  - pipeline/upload_pages_r2.py
  - review/Dockerfile
  - review/docker-compose.yml
  - review/ocr_review_app.py

Unified diff (only + lines are new):

=== DIFF: pipeline/extract_genius_scan.py ===
@@ -0,0 +1,204 @@
+# /// script
+# requires-python = ">=3.10"
+# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24"]
+# ///
+"""Pull the Genius Scan text layer out of every recut PDF and into Neon.
+
+    uv run extract_genius_scan.py --dry     # verify mapping, write nothing
+    uv run extract_genius_scan.py           # write genius_scan_v2 rows
+
+WHY THIS EXISTS
+---------------
+Every recut PDF still carries the text layer Genius Scan produced when the page
+was originally scanned. It is poor text ("San Jos0 Water Campay") but it is the
+ONLY copy that exists: measured 2026-08-14, zero of the 1,762 v2 pages have a
+genius_scan_v1 row in Neon -- all 2,327 legacy readings attach to the old
+v1-merged files, which describe different images.
+
+embed_ocr.py deletes that layer (strip_text) before writing the Mistral text in.
+That delete is irreversible and the bytes exist nowhere else, so this script
+copies them into Neon first. Nothing consumes the result. It is archival
+insurance on a one-way door, and it is cheap.
+
+SAFETY
+------
+Additive only. Writes a NEW method ('genius_scan_v2'); never touches the Mistral
+rows or any human correction. ocr_reading has UNIQUE (page_id, method), so
+re-running is a no-op on pages already captured -- the script is resumable and
+safe to run twice.
+
+Every page gets a row, including pages whose text layer is empty, because
+"we looked and there was nothing" is itself a fact worth keeping.
+"""
+import argparse
+import os
+import sys
+from collections import Counter
+
+import fitz
+import psycopg
+
+MISTRAL = "mistral-ocr-4-1"
+METHOD = "genius_scan_v2"
+BUILD = "recut-v2"
+
+DEFAULT_RECUT = r"C:\Users\busin\Documents\Document Splitting for Paperless\recut"
+
+
+def main():
+    ap = argparse.ArgumentParser()
+    ap.add_argument("--dry", action="store_true", help="verify only, write nothing")
+    ap.add_argument("--recut", default=os.environ.get("PAGE_SOURCE") or DEFAULT_RECUT)
+    args = ap.parse_args()
+
+    if not os.path.isdir(args.recut):
+        sys.exit("recut folder not found: %s" % args.recut)
+
+    url = os.environ.get("NEON_DATABASE_URL")
+    if not url:
+        sys.exit("NEON_DATABASE_URL is not set")
+
+    con = psycopg.connect(url, connect_timeout=30)
+    cur = con.cursor()
+
+    # One row per Mistral page, carrying the PDF it came from and its 1-based
+    # index inside that PDF. This is the same mapping the review app uses to
+    # render page images, so it is already proven against the real corpus.
+    cur.execute("""
+        select r.page_id,
+               (r.meta->>'document_id')::bigint  as document_id,
+               (r.meta->>'doc_page')::int        as doc_page,
+               o.name
+        from ocr_reading r
+        join output_file o
+          on o.document_id = (r.meta->>'document_id')::bigint
+         and o.build_version = %s
+        where r.method = %s
+        order by o.name, (r.meta->>'doc_page')::int
+    """, (BUILD, MISTRAL))
+    rows = cur.fetchall()
+
+    cur.execute("select count(*) from ocr_reading where method = %s", (MISTRAL,))
+    total_mistral = cur.fetchone()[0]
+
+    print("mistral pages          : %d" % total_mistral)
+    print("mapped to a recut PDF  : %d" % len(rows))
+    if len(rows) != total_mistral:
+        print("  !! %d Mistral pages have no recut-v2 output_file row"
+              % (total_mistral - len(rows)))
+
+    cur.execute("select count(*) from ocr_reading where method = %s", (METHOD,))
+    already = cur.fetchone()[0]
+    print("already captured       : %d" % already)
+    print()
+
+    # ---- verify lineage: does every page actually exist in its PDF? ----
+    by_pdf = {}
+    for page_id, did, dp, name in rows:
+        by_pdf.setdefault(name, []).append((page_id, did, dp))
+
+    missing_pdf, page_oob = [], []
+    for name, items in by_pdf.items():
+        p = os.path.join(args.recut, name)
+        if not os.path.exists(p):
+            missing_pdf.append(name)
+    print("distinct recut PDFs referenced : %d" % len(by_pdf))
+    print("referenced PDFs missing on disk: %d" % len(missing_pdf))
+    if missing_pdf:
+        for n in missing_pdf[:5]:
+            print("   %s" % n)
+
+    if args.dry:
+        print("\n--dry: verifying page counts and sampling text, writing nothing\n")
+
+    stats = Counter()
+    sample_shown = 0
+    written = 0
+    BATCH = 200
+    pending = []
+
+    for i, (name, items) in enumerate(sorted(by_pdf.items()), 1):
+        path = os.path.join(args.recut, name)
+        if not os.path.exists(path):
+            stats["pdf_missing"] += len(items)
+            continue
+        try:
+            doc = fitz.open(path)
+        except Exception as e:
+            stats["pdf_open_failed"] += len(items)
+            print("  OPEN FAILED %s: %s" % (name[:50], str(e)[:60]))
+            continue
+
+        for page_id, did, dp in items:
+            idx = (dp or 1) - 1
+            if idx < 0 or idx >= len(doc):
+                page_oob.append((name, dp, len(doc)))
+                stats["page_out_of_range"] += 1
+                continue
+            try:
+                text = doc[idx].get_text() or ""
+            except Exception:
+                stats["extract_failed"] += 1
+                continue
+            stats["pages_read"] += 1
+            if text.strip():
+                stats["with_text"] += 1
+            else:
+                stats["empty_layer"] += 1
+
+            if args.dry:
+                if text.strip() and sample_shown < 3:
+                    sample_shown += 1
+                    print("  sample %s p%d: %r" % (name[:40], dp, text[:110]))
+                continue
+
+            pending.append((page_id, METHOD, text,
+                            psycopg.types.json.Jsonb({
+                                "chars": len(text),
+                                "source_pdf": name,
+                                "doc_page": dp,
+                                "document_id": did,
+                                "captured_by": "extract_genius_scan.py",
+                                "why": "archival copy before embed_ocr.strip_text()",
+                            })))
+            if len(pending) >= BATCH:
+                written += flush(cur, con, pending)
+                pending.clear()
+        doc.close()
+        if i % 200 == 0:
+            print("  %d/%d PDFs" % (i, len(by_pdf)))
+
+    if pending and not args.dry:
+        written += flush(cur, con, pending)
+
+    print()
+    print("pages read       : %d" % stats["pages_read"])
+    print("  with text      : %d" % stats["with_text"])
+    print("  EMPTY layer    : %d" % stats["empty_layer"])
+    for k in ("pdf_missing", "pdf_open_failed", "page_out_of_range", "extract_failed"):
+        if stats[k]:
+            print("  %-14s : %d" % (k, stats[k]))
+    if page_oob:
+        print("  out-of-range examples: %s" % page_oob[:3])
+
+    if not args.dry:
+        cur.execute("select count(*) from ocr_reading where method = %s", (METHOD,))
+        print("\nrows inserted this run : %d" % written)
+        print("genius_scan_v2 total   : %d" % cur.fetchone()[0])
+    con.close()
+
+
+def flush(cur, con, pending):
+    """Insert a batch. ON CONFLICT makes the whole script resumable."""
+    cur.executemany("""
+        insert into ocr_reading (page_id, method, text, meta)
+        values (%s, %s, %s, %s)
+        on conflict (page_id, method) do nothing
+    """, pending)
+    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(pending)
+    con.commit()
+    return n
+
+
+if __name__ == "__main__":
+    main()


=== DIFF: pipeline/link_page_images.py ===
@@ -0,0 +1,91 @@
+# /// script
+# requires-python = ">=3.10"
+# dependencies = ["psycopg[binary]>=3.1"]
+# ///
+"""Record every page's R2 image in Neon.
+
+    uv run link_page_images.py --manifest <pages-r2/_manifest.json>
+
+WHY A TABLE AND NOT A CONVENTION
+--------------------------------
+The key itself is predictable ("pages/<page_id>.jpg"), so the app does not
+strictly need a lookup to FIND an image. What it cannot derive is the geometry:
+
+    pt_to_px -- how many JPEG pixels one PDF point is worth, for THIS page
+
+That single number is what lets Mistral's block boxes be drawn over the scan.
+Blocks are stored in Mistral's own coordinate space, the JPEG is in pixels, and
+the ratio differs per page because the source scans are not one size (measured:
+native resolution runs 91-328 DPI across the corpus). Storing it per page means
+the overlay is a multiplication rather than a guess.
+
+Width and height are kept for the same reason -- the browser can size the canvas
+before the image loads, so boxes do not jump on first paint.
+"""
+import argparse, io, json, os, sys
+import psycopg
+
+DDL = """
+create table if not exists page_image (
+  page_id     bigint primary key,
+  document_id bigint,
+  doc_page    int,
+  r2_key      text not null,
+  width       int,
+  height      int,
+  dpi         int,
+  pt_to_px    double precision,
+  bytes       bigint,
+  uploaded    boolean not null default false,
+  ts          timestamptz not null default now()
+);
+create index if not exists page_image_document_idx on page_image (document_id);
+"""
+
+
+def main():
+    ap = argparse.ArgumentParser()
+    ap.add_argument("--manifest", required=True)
+    ap.add_argument("--mark-uploaded", action="store_true",
+                    help="set uploaded=true (run after upload_pages_r2.py)")
+    args = ap.parse_args()
+
+    rows = json.load(io.open(args.manifest, encoding="utf-8"))
+    print("manifest entries: %d" % len(rows))
+
+    con = psycopg.connect(os.environ["NEON_DATABASE_URL"], connect_timeout=30)
+    cur = con.cursor()
+    cur.execute(DDL)
+    con.commit()
+
+    payload = [(r["page_id"], r.get("document_id"), r.get("doc_page"),
+                r["key"], r.get("w"), r.get("h"), r.get("dpi"),
+                r.get("pt_to_px"), r.get("bytes"), bool(args.mark_uploaded))
+               for r in rows]
+
+    cur.executemany("""
+        insert into page_image
+          (page_id, document_id, doc_page, r2_key, width, height, dpi,
+           pt_to_px, bytes, uploaded)
+        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
+        on conflict (page_id) do update set
+          r2_key=excluded.r2_key, width=excluded.width, height=excluded.height,
+          dpi=excluded.dpi, pt_to_px=excluded.pt_to_px, bytes=excluded.bytes,
+          uploaded=page_image.uploaded or excluded.uploaded, ts=now()
+    """, payload)
+    con.commit()
+
+    cur.execute("select count(*), count(*) filter (where uploaded) from page_image")
+    n, up = cur.fetchone()
+    cur.execute("""select count(*) from ocr_reading r
+                   where r.method='mistral-ocr-4-1'
+                     and not exists (select 1 from page_image p
+                                     where p.page_id = r.page_id)""")
+    missing = cur.fetchone()[0]
+    print("page_image rows      : %d  (uploaded=%d)" % (n, up))
+    print("Mistral pages w/o img: %d" % missing)
+    con.close()
+
+
+if __name__ == "__main__":
+    main()


=== DIFF: pipeline/render_page_jpegs.py ===
@@ -0,0 +1,164 @@
+# /// script
+# requires-python = ">=3.10"
+# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24"]
+# ///
+"""Render every page to a 300 DPI JPEG for Cloudflare R2.
+
+    uv run render_page_jpegs.py --dry
+    uv run render_page_jpegs.py                 # resumable; skips what exists
+
+WHY: the review app currently rasterises pages on demand from 3.09 GB of local
+PDFs. That cannot be served to a remote reviewer -- one document is 118 MB, and
+sending a 47-page PDF to display page 4 is the real latency cost, more than
+resolution is. Pre-rendering one JPEG per page turns every page view into a
+single small GET from R2.
+
+NAMED BY page_id. That is the primary key the app already uses to fetch text,
+confidence and corrections, so the image lands in the same lookup with no extra
+mapping table on the read path.
+
+SAME DIMENSIONS: every page is rendered at exactly 300 DPI with no cropping and
+no forced aspect, so the JPEG's pixel box is an exact scalar multiple of the PDF
+page box. That is what lets Mistral's bounding boxes be drawn over the image --
+a box at (x, y) in page coordinates maps to (x * k, y * k) in the JPEG for a
+single k per page. Any crop or letterbox would silently break that overlay.
+"""
+import argparse
+import io
+import json
+import os
+import sys
+from collections import Counter
+
+import fitz
+import psycopg
+
+MISTRAL = "mistral-ocr-4-1"
+BUILD = "recut-v2"
+DEFAULT_RECUT = r"C:\Users\busin\Documents\Document Splitting for Paperless\recut"
+DEFAULT_OUT = r"C:\Users\busin\Documents\Document Splitting for Paperless\pages-r2"
+
+
+def main():
+    ap = argparse.ArgumentParser()
+    ap.add_argument("--dry", action="store_true")
+    ap.add_argument("--dpi", type=int, default=300)
+    ap.add_argument("--quality", type=int, default=82)
+    ap.add_argument("--recut", default=os.environ.get("PAGE_SOURCE") or DEFAULT_RECUT)
+    ap.add_argument("--out", default=DEFAULT_OUT)
+    args = ap.parse_args()
+
+    if not os.path.isdir(args.recut):
+        sys.exit("recut folder not found: %s" % args.recut)
+    os.makedirs(args.out, exist_ok=True)
+
+    con = psycopg.connect(os.environ["NEON_DATABASE_URL"], connect_timeout=30)
+    cur = con.cursor()
+    cur.execute("""
+        select r.page_id,
+               (r.meta->>'document_id')::bigint,
+               (r.meta->>'doc_page')::int,
+               o.name
+        from ocr_reading r
+        join output_file o
+          on o.document_id = (r.meta->>'document_id')::bigint
+         and o.build_version = %s
+        where r.method = %s
+        order by o.name, (r.meta->>'doc_page')::int
+    """, (BUILD, MISTRAL))
+    rows = cur.fetchall()
+    con.close()
+    print("pages to render : %d   dpi=%d q=%d" % (len(rows), args.dpi, args.quality))
+    print("output          : %s" % args.out)
+
+    by_pdf = {}
+    for page_id, did, dp, name in rows:
+        by_pdf.setdefault(name, []).append((page_id, did, dp))
+
+    zoom = args.dpi / 72.0
+    mat = fitz.Matrix(zoom, zoom)
+    stats = Counter()
+    manifest = []
+    dims = Counter()
+
+    for i, (name, items) in enumerate(sorted(by_pdf.items()), 1):
+        path = os.path.join(args.recut, name)
+        if not os.path.exists(path):
+            stats["pdf_missing"] += len(items)
+            continue
+        try:
+            doc = fitz.open(path)
+        except Exception as e:
+            stats["open_failed"] += len(items)
+            print("  OPEN FAILED %s: %s" % (name[:45], str(e)[:60]))
+            continue
+
+        for page_id, did, dp in items:
+            idx = (dp or 1) - 1
+            if idx < 0 or idx >= len(doc):
+                stats["out_of_range"] += 1
+                continue
+            dest = os.path.join(args.out, "%d.jpg" % page_id)
+            if os.path.exists(dest) and os.path.getsize(dest) > 0:
+                stats["skipped_existing"] += 1
+                continue
+            if args.dry:
+                stats["would_render"] += 1
+                continue
+            try:
+                page = doc[idx]
+                pix = page.get_pixmap(matrix=mat, alpha=False)
+                pix.save(dest, jpg_quality=args.quality)
+                stats["rendered"] += 1
+                stats["bytes"] += os.path.getsize(dest)
+                dims["%dx%d" % (pix.width, pix.height)] += 1
+                manifest.append({
+                    "page_id": page_id, "document_id": did, "doc_page": dp,
+                    "key": "pages/%d.jpg" % page_id,
+                    "w": pix.width, "h": pix.height,
+                    "bytes": os.path.getsize(dest),
+                    "dpi": args.dpi,
+                    # scale factor from PDF points to JPEG pixels, for overlays
+                    "pt_to_px": round(pix.width / page.rect.width, 6),
+                    "pdf": name,
+                })
+            except Exception as e:
+                stats["render_failed"] += 1
+                print("  RENDER FAILED %s p%s: %s" % (name[:40], dp, str(e)[:60]))
+        doc.close()
+        if i % 100 == 0:
+            mb = stats["bytes"] / 1048576.0
+            print("  %d/%d PDFs   rendered=%d  %.0f MB"
+                  % (i, len(by_pdf), stats["rendered"], mb))
+
+    if manifest:
+        mf = os.path.join(args.out, "_manifest.json")
+        # append-safe: merge with anything a previous run wrote
+        old = []
+        if os.path.exists(mf):
+            try:
+                old = json.load(io.open(mf, encoding="utf-8"))
+            except Exception:
+                old = []
+        seen = {m["page_id"] for m in manifest}
+        merged = manifest + [m for m in old if m.get("page_id") not in seen]
+        json.dump(merged, io.open(mf, "w", encoding="utf-8"), indent=1)
+        print("\nwrote %s (%d entries)" % (mf, len(merged)))
+
+    print()
+    for k, v in sorted(stats.items()):
+        if k == "bytes":
+            print("  %-18s %.1f MB" % ("total size", v / 1048576.0))
+        else:
+            print("  %-18s %d" % (k, v))
+    if stats["rendered"]:
+        print("  %-18s %.0f KB" % ("mean per page",
+                                   stats["bytes"] / stats["rendered"] / 1024.0))
+    if dims:
+        print("\n  distinct pixel dimensions: %d" % len(dims))
+        for d, n in dims.most_common(6):
+            print("     %-14s %d pages" % (d, n))
+
+
+if __name__ == "__main__":
+    main()


=== DIFF: pipeline/upload_pages_r2.py ===
@@ -0,0 +1,106 @@
+# /// script
+# requires-python = ">=3.10"
+# dependencies = ["boto3>=1.34"]
+# ///
+"""Upload the rendered page JPEGs to Cloudflare R2.
+
+    uv run upload_pages_r2.py --src <pages-r2> --dry
+    uv run upload_pages_r2.py --src <pages-r2>
+
+Needs, in the environment:
+    R2_BUCKET  R2_ENDPOINT  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY
+
+THE BUCKET STAYS PRIVATE. These are probate documents -- bank statements, an
+EIN letter, a creditor's claim against the estate. Nothing here makes the bucket
+public; the app hands the browser a short-lived signed URL instead. If you ever
+do attach a public custom domain to this bucket, that decision should be made
+deliberately and not inherited from a script.
+
+Resumable: an object already present at the same size is skipped, so an
+interrupted run costs only the time to re-list.
+"""
+import argparse, os, sys, time
+
+import boto3
+from botocore.config import Config
+from botocore.exceptions import ClientError
+
+
+def main():
+    ap = argparse.ArgumentParser()
+    ap.add_argument("--src", required=True)
+    ap.add_argument("--prefix", default=os.environ.get("R2_PREFIX", "pages"))
+    ap.add_argument("--dry", action="store_true")
+    args = ap.parse_args()
+
+    for var in ("R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID",
+                "R2_SECRET_ACCESS_KEY"):
+        if not os.environ.get(var):
+            sys.exit("%s is not set" % var)
+
+    bucket = os.environ["R2_BUCKET"]
+    s3 = boto3.client("s3",
+                      endpoint_url=os.environ["R2_ENDPOINT"],
+                      aws_access_key_id=[REDACTED]["R2_ACCESS_KEY_ID"],
+                      aws_secret_access_key=[REDACTED]["R2_SECRET_ACCESS_KEY"],
+                      region_name="auto",
+                      config=Config(signature_version="s3v4",
+                                    retries={"max_attempts": 5, "mode": "standard"}))
+
+    files = sorted(f for f in os.listdir(args.src) if f.lower().endswith(".jpg"))
+    print("local JPEGs : %d" % len(files))
+    print("bucket      : %s   prefix: %s" % (bucket, args.prefix))
+
+    # One listing beats one HEAD per object -- 1,762 round trips becomes ~2.
+    existing = {}
+    token = None
+    while True:
+        kw = {"Bucket": bucket, "Prefix": args.prefix.strip("/") + "/"}
+        if token:
+            kw["ContinuationToken"] = token
+        try:
+            resp = s3.list_objects_v2(**kw)
+        except ClientError as e:
+            sys.exit("cannot list bucket: %s" % e)
+        for o in resp.get("Contents") or []:
+            existing[o["Key"]] = o["Size"]
+        if not resp.get("IsTruncated"):
+            break
+        token = resp.get("NextContinuationToken")
+    print("already in R2: %d" % len(existing))
+
+    up = skip = fail = 0
+    sent = 0
+    t0 = time.time()
+    for i, name in enumerate(files, 1):
+        path = os.path.join(args.src, name)
+        size = os.path.getsize(path)
+        key = "%s/%s" % (args.prefix.strip("/"), name)
+        if existing.get(key) == size:
+            skip += 1
+            continue
+        if args.dry:
+            up += 1
+            continue
+        try:
+            s3.upload_file(path, bucket, key,
+                           ExtraArgs={"ContentType": "image/jpeg",
+                                      "CacheControl": "public, max-age=31536000, immutable"})
+            up += 1
+            sent += size
+        except ClientError as e:
+            fail += 1
+            print("  FAILED %s: %s" % (name, str(e)[:80]))
+        if i % 200 == 0:
+            print("  %d/%d  uploaded=%d skipped=%d  %.0f MB  %.0fs"
+                  % (i, len(files), up, skip, sent / 1048576.0, time.time() - t0))
+
+    print()
+    print("uploaded : %d" % up)
+    print("skipped  : %d" % skip)
+    print("failed   : %d" % fail)
+    print("sent     : %.1f MB in %.0fs" % (sent / 1048576.0, time.time() - t0))
+
+
+if __name__ == "__main__":
+    main()


=== DIFF: review/Dockerfile ===
@@ -0,0 +1,34 @@
+# OCR review app -- deployable image for Coolify.
+#
+# The app holds NO document data. Text, confidence, corrections and verdicts all
+# live in Neon; page images live in R2 and are fetched by the browser directly
+# from a signed URL. So this image stays small and carries nothing sensitive,
+# and the container can be rebuilt or moved without touching the corpus.
+FROM python:3.12-slim
+
+# PyMuPDF ships manylinux wheels, so no build toolchain is needed. It is kept
+# in the image because it powers the local-render fallback when R2 is not
+# configured -- the same file then runs unchanged on Alden's laptop.
+RUN pip install --no-cache-dir \
+        "psycopg[binary]>=3.1" \
+        "pymupdf>=1.24" \
+        "boto3>=1.34"
+
+WORKDIR /app
+COPY ocr_review_app.py /app/
+
+# Coolify's proxy terminates TLS and forwards here, so bind all interfaces.
+ENV HOST=0.0.0.0 \
+    PORT=8778 \
+    NO_BROWSER=1 \
+    PYTHONUNBUFFERED=1
+
+EXPOSE 8778
+
+# Fails the container if the app cannot answer, so Coolify restarts it rather
+# than leaving a dead page in front of a reviewer.
+HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
+  CMD python -c "import urllib.request,sys; \
+sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8778/healthz',timeout=4).status==200 else 1)"
+
+CMD ["python", "ocr_review_app.py"]


=== DIFF: review/docker-compose.yml ===
@@ -0,0 +1,29 @@
+# Coolify reads this directly. Every value comes from the environment, so no
+# secret is ever written into the repo.
+services:
+  ocr-review:
+    build:
+      context: .
+      dockerfile: Dockerfile
+    restart: unless-stopped
+    ports:
+      - "8778:8778"
+    environment:
+      # --- required ---
+      NEON_DATABASE_URL: ${NEON_DATABASE_URL}
+      # "name:password,name:password" -- the name becomes part of the
+      # ocr_reading method, so each reviewer's corrections stay theirs.
+      REVIEW_USERS: ${REVIEW_USERS}
+      # Signs the login cookie. Change it and everyone is logged out.
+      SESSION_SECRET: ${SESSION_SECRET}
+
+      # --- page images from R2 (omit all five to fall back to local render) ---
+      R2_BUCKET: ${R2_BUCKET}
+      R2_ENDPOINT: ${R2_ENDPOINT}
+      R2_ACCESS_KEY_ID: ${R2_ACCESS_KEY_ID}
+      R2_SECRET_ACCESS_KEY: ${R2_SECRET_ACCESS_KEY}
+      R2_PREFIX: ${R2_PREFIX:-pages}
+
+      HOST: 0.0.0.0
+      PORT: 8778
+      NO_BROWSER: "1"


=== DIFF: review/ocr_review_app.py ===
@@ -1,6 +1,6 @@
 # /// script
 # requires-python = ">=3.10"
-# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24"]
+# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24", "boto3>=1.34"]
 # ///
 """Review + correct the low-confidence Mistral OCR before it is embedded.
 
@@ -36,7 +36,7 @@ page's correction in one transaction. ocr_reading has a UNIQUE constraint on
 (page_id, method), so the method carries the reviewer -- 'human-corrected:jeff'
 -- and the database itself enforces one correction per page per reviewer.
 """
-import io, json, os, re, socketserver, sys, threading, webbrowser
+import hashlib, hmac, io, json, os, re, socketserver, sys, threading, webbrowser
 from http.server import SimpleHTTPRequestHandler
 from urllib.parse import parse_qs, unquote, urlparse
 
@@ -45,11 +45,16 @@ import psycopg
 
 BASE = os.path.dirname(os.path.abspath(__file__))
 
-# Page images are the ONE thing Neon cannot serve -- it stores no pixels. The
-# 3.09 GB of scans live outside this repo, so the location is configurable
-# rather than assumed to sit beside the script. Set PAGE_SOURCE to wherever
-# recut/ actually is; the fallback keeps the original single-folder layout
-# working. When pages move to R2 this becomes the only thing that changes.
+# Page images are the ONE thing Neon cannot serve -- it stores no pixels.
+#
+# TWO SOURCES, in priority order:
+#   1. R2  -- pre-rendered 300 DPI JPEGs, one per page_id (render_page_jpegs.py).
+#             This is what a remote reviewer gets. The app never streams the
+#             bytes itself; it signs a short-lived URL and redirects, so the
+#             image travels R2 -> browser directly and the bucket stays private.
+#   2. local recut/ -- rasterise on demand from the source PDFs. This is the
+#             original single-machine behaviour and remains the fallback so the
+#             app still runs on Alden's laptop with nothing configured.
 RECUT = (os.environ.get("PAGE_SOURCE")
          or os.path.join(BASE, "recut")
          or "")
@@ -57,22 +62,54 @@ if not os.path.isdir(RECUT):
     alt = os.path.join(os.path.dirname(BASE), "recut")
     if os.path.isdir(alt):
         RECUT = alt
-PORT = 8778
+
+R2_BUCKET = os.environ.get("R2_BUCKET") or ""
+R2_ENDPOINT = os.environ.get("R2_ENDPOINT") or ""      # https://<acct>.r2.cloudflarestorage.com
+R2_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID") or ""
+R2_SECRET = [REDACTED]("R2_SECRET_ACCESS_KEY") or ""
+R2_PREFIX = os.environ.get("R2_PREFIX", "pages")
+R2_SIGN_TTL = int(os.environ.get("R2_SIGN_TTL") or 3600)
+USE_R2 = bool(R2_BUCKET and R2_ENDPOINT and R2_KEY_ID and R2_SECRET)
+
+HOST = os.environ.get("HOST") or "127.0.0.1"
+PORT = int(os.environ.get("PORT") or 8778)
+
 BAD_WORD = 0.60          # a word below this is "suspect"
 GATE = 2.0               # % of suspect words that puts a document in the queue
 MIN_WORDS = 20           # below this a percentage is noise, not a signal
 MAX_REPEAT = 4           # consecutive identical non-blank rows = a loop
-# No Genius Scan comparison: Mistral is decisively better, so the old reading
-# has no review value. The v2 pages never had one in Neon anyway (measured: 0 of
-# 1,762) -- all 2,327 legacy readings sit on v1-merged files.
 MISTRAL, HUMAN = "mistral-ocr-4-1", "human-corrected"
-RENDER_DPI = 200         # high enough that zooming in reveals more, not blur
+RENDER_DPI = 200         # local fallback render only; R2 JPEGs are 300 DPI
+
+# WHO IS REVIEWING -- now PER REQUEST, not per process.
+#
+# This used to be one process-wide REVIEWER read from the environment, which is
+# correct for one person on one laptop and wrong the moment two people share a
+# deployment: whoever the server was started as would own every correction.
+# The reviewer now comes from the signed session cookie, so Alden and Jeff can
+# be in the app at the same time and each write under their own name.
+#
+# REVIEW_USERS is "name:password,name:password". Names become part of the
+# ocr_reading method, so they are normalised to lowercase here and nowhere else.
+def _parse_users(raw):
+    users = {}
+    for pair in (raw or "").split(","):
+        pair = pair.strip()
+        if not pair or ":" not in pair:
+            continue
+        name, _, pw = pair.partition(":")
+        name = name.strip().lower()
+        if name and pw:
+            users[name] = pw
+    return users
+
+
+USERS = _parse_users(os.environ.get("REVIEW_USERS"))
+# Single-user local use keeps working with no login at all.
+SOLO = (os.environ.get("REVIEWER") or "").strip().lower() if not USERS else ""
+SECRET = (os.environ.get("SESSION_SECRET")
+          or os.environ.get("NEON_DATABASE_URL") or "dev").encode()
 
-# WHO IS REVIEWING. Corrections are stored per reviewer -- meta.by -- so two
-# people working the same corpus never overwrite each other, and so a court
-# record can show who changed what. Set REVIEWER per deployment; the login
-# supplies it once this runs on the server.
-REVIEWER = (os.environ.get("REVIEWER") or "alden").strip().lower()
 
 # ocr_reading carries a UNIQUE constraint on (page_id, method) --
 # ocr_reading_page_id_method_key. A single shared 'human-corrected' method
@@ -80,7 +117,9 @@ REVIEWER = (os.environ.get("REVIEWER") or "alden").strip().lower()
 # reviewer's save dies on a constraint violation. Putting the reviewer in the
 # method turns that constraint into exactly the rule we want: one correction
 # per page PER REVIEWER, enforced by the database rather than by convention.
-MY_METHOD = "%s:%s" % (HUMAN, REVIEWER)
+def method_for(reviewer):
+    return "%s:%s" % (HUMAN, reviewer)
+
 
 URL = os.environ.get("NEON_DATABASE_URL")
 if not URL:
@@ -158,7 +197,7 @@ def bad_rate(conf):
     return score(page_words(conf))
 
 
-def build_queue(cur):
+def build_queue(cur, reviewer):
     """Every flagged document, worst first. One query, scored in Python because
     the scoring rule lives in one place and must match the numbers above."""
     cur.execute("""
@@ -178,8 +217,8 @@ def build_queue(cur):
         # under a reviewer key would otherwise blow up the whole queue.
         rv = review if isinstance(review, dict) else {}
         peers = {w: v.get("verdict") for w, v in rv.items()
-                 if w != REVIEWER and isinstance(v, dict) and v.get("verdict")}
-        me = rv.get(REVIEWER)
+                 if w != reviewer and isinstance(v, dict) and v.get("verdict")}
+        me = rv.get(reviewer)
         mine = me.get("verdict") if isinstance(me, dict) else None
         d = docs.setdefault(did, {"id": did, "key": key, "pdf": name,
                                   "pages": 0, "bad": 0, "words": 0,
@@ -225,7 +264,7 @@ def build_queue(cur):
     return out
 
 
-def load_doc(cur, did):
+def load_doc(cur, did, reviewer):
     """All pages of one document: text, suspect-word spans, prior reading,
     and any correction already saved."""
     cur.execute("""
@@ -244,7 +283,7 @@ def load_doc(cur, did):
         for pid, method, text, blocks, meta, ts in cur.fetchall():
             who = (method.split(":", 1)[1] if ":" in method
                    else ((meta or {}).get("by") or "unknown")).lower()
-            if who == REVIEWER:
+            if who == reviewer:
                 corrected[pid] = text
                 corr_tbl[pid] = (blocks or {}).get("tables")
                 notes[pid] = (meta or {}).get("note") or ""
@@ -298,7 +337,7 @@ def load_doc(cur, did):
     return {"id": did, "pdf": got[0] if got else None, "pages": pages}
 
 
-def save_page(cur, page_id, text, tables, note):
+def save_page(cur, page_id, text, tables, note, reviewer):
     """Replace this page's correction -- text, tables and note in one row.
     DELETE+INSERT because ocr_reading has no unique constraint; an INSERT
     alone would stack another row on every save.
@@ -309,21 +348,22 @@ def save_page(cur, page_id, text, tables, note):
     note alone is enough to write the row -- flagging a problem you have not
     fixed yet must not be lost.
     """
+    my_method = method_for(reviewer)
     cur.execute("delete from ocr_reading where page_id = %s and method = %s",
-                (page_id, MY_METHOD))
+                (page_id, my_method))
     if (text or "").strip() or tables or (note or "").strip():
         cur.execute("""insert into ocr_reading
                        (page_id, method, text, blocks, meta)
                        values (%s, %s, %s, %s::jsonb, %s::jsonb)""",
-                    (page_id, MY_METHOD, text or "",
+                    (page_id, my_method, text or "",
                      json.dumps({"tables": tables or []}, ensure_ascii=False),
                      json.dumps({"source": "ocr_review_app",
-                                 "by": REVIEWER,
+                                 "by": reviewer,
                                  "note": (note or "").strip()},
                                 ensure_ascii=False)))
 
 
-def verdict(cur, did, value):
+def verdict(cur, did, value, reviewer):
     """Stamp the review verdict. document.meta only -- document.state is
     load-bearing for the pipeline and is not touched.
 
@@ -343,10 +383,12 @@ def verdict(cur, did, value):
                        'verdict' - 'approved' ||
                      jsonb_build_object(%s::text,
                        jsonb_build_object('verdict', %s::text)))
-                   where id = %s""", (REVIEWER, value, did))
+                   where id = %s""", (reviewer, value, did))
 
 
 def page_png(pdf_name, doc_page):
+    """Local fallback: rasterise straight from the source PDF. Used when R2 is
+    not configured, i.e. running on the machine that holds recut/."""
     path = os.path.join(RECUT, pdf_name)
     if not pdf_name or ".." in pdf_name or not os.path.isfile(path):
         return None
@@ -359,10 +401,104 @@ def page_png(pdf_name, doc_page):
         doc.close()
 
 
+_s3 = None
+
+
+def r2_url(page_id):
+    """A short-lived signed URL for this page's 300 DPI JPEG.
+
+    We redirect the browser here rather than streaming the bytes through this
+    process. A 47-page document is ~50 MB of JPEG; proxying that through a small
+    Coolify container would make the app the bottleneck for no benefit. Signing
+    keeps the bucket private -- these are probate documents and must not be
+    world-readable."""
+    global _s3
+    if not USE_R2:
+        return None
+    if _s3 is None:
+        import boto3                                   # noqa: PLC0415
+        from botocore.config import Config             # noqa: PLC0415
+        _s3 = boto3.client("s3", endpoint_url=R2_ENDPOINT,
+                           aws_access_key_id=[REDACTED],
+                           aws_secret_access_key=[REDACTED],
+                           region_name="auto",
+                           config=Config(signature_version="s3v4"))
+    key = "%s/%d.jpg" % (R2_PREFIX.strip("/"), int(page_id))
+    return _s3.generate_presigned_url("get_object",
+                                      Params={"Bucket": R2_BUCKET, "Key": key},
+                                      ExpiresIn=R2_SIGN_TTL)
+
+
+# ---------------------------------------------------------------- sessions
+def _sign(value):
+    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()[:32]
+
+
+def make_cookie(reviewer):
+    """name|signature. Stateless, so restarting the container does not log
+    everyone out, and there is no session store to keep."""
+    return "%s|%s" % (reviewer, _sign(reviewer))
+
+
+def cookie_reviewer(header):
+    """Whoever this request is, or None. Never trusts the name without the
+    signature -- otherwise anyone could write corrections as anyone."""
+    if SOLO:
+        return SOLO
+    if not USERS:
+        return "alden"
+    for part in (header or "").split(";"):
+        part = part.strip()
+        if not part.startswith("rev="):
+            continue
+        raw = unquote(part[4:])
+        name, _, sig = raw.partition("|")
+        name = name.strip().lower()
+        if name in USERS and sig and hmac.compare_digest(sig, _sign(name)):
+            return name
+    return None
+
+
+LOGIN_HTML = """<!doctype html><meta charset="utf-8"><title>OCR review</title>
+<meta name=viewport content="width=device-width,initial-scale=1">
+<style>
+body{font:16px/1.5 system-ui,sans-serif;background:#111;color:#eee;
+  display:grid;place-items:center;height:100vh;margin:0}
+form{background:#1c1c1c;padding:28px 30px;border-radius:10px;width:min(92vw,330px);
+  border:1px solid #333}
+h1{font-size:17px;margin:0 0 18px}
+label{display:block;font-size:12px;color:#999;margin:12px 0 4px}
+input{width:100%;padding:9px 10px;font-size:15px;border-radius:6px;
+  border:1px solid #444;background:#111;color:#eee;box-sizing:border-box}
+button{width:100%;margin-top:18px;padding:10px;font-size:15px;border:0;
+  border-radius:6px;background:#2d6cdf;color:#fff;cursor:pointer}
+.err{color:#ff8080;font-size:13px;margin-top:12px}
+</style>
+<form method=post action=/login>
+<h1>OCR review</h1>
+<label>Name</label><input name=user autofocus autocapitalize=off>
+<label>Password</label><input name=pw type=password>
+<button>Sign in</button>
+<!--ERR-->
+</form>"""
+
+
+def login_page(error=""):
+    """Token substitution, NOT %-formatting: the stylesheet contains
+    'width:100%' and a bare % is an invalid format spec, which turned the
+    login page into a 500 for anonymous visitors -- i.e. for everyone."""
+    return LOGIN_HTML.replace(
+        "<!--ERR-->",
+        '<div class="err">%s</div>' % error if error else "")
+
+
 class Handler(SimpleHTTPRequestHandler):
     def log_message(self, *a):
         pass
 
+    def whoami(self):
+        return cookie_reviewer(self.headers.get("Cookie"))
+
     def _send(self, code, body, ctype="application/json; charset=utf-8"):
         b = body if isinstance(body, bytes) else body.encode("utf-8")
         self.send_response(code)
@@ -372,26 +508,64 @@ class Handler(SimpleHTTPRequestHandler):
         self.end_headers()
         self.wfile.write(b)
 
+    def _redirect(self, to, cookie=None):
+        self.send_response(302)
+        self.send_header("Location", to)
+        if cookie:
+            self.send_header("Set-Cookie",
+                             "rev=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
+                             % cookie)
+        self.send_header("Content-Length", "0")
+        self.end_headers()
+
     def do_GET(self):
         u = urlparse(self.path)
         path, q = unquote(u.path), parse_qs(u.query)
         try:
-            if path in ("/", "/index.html"):
-                return self._send(200, HTML, "text/html; charset=utf-8")
             if path == "/favicon.ico":
                 return self._send(204, b"", "image/x-icon")
-            with db() as c, c.cursor() as cur:
-                if path == "/queue":
-                    return self._send(200, json.dumps(build_queue(cur)))
-                if path == "/doc":
-                    return self._send(200, json.dumps(
-                        load_doc(cur, int(q.get("id", ["0"])[0]))))
-            if path == "/page.png":
+            if path == "/healthz":                    # Coolify health check
+                return self._send(200, '{"ok":true}')
+            if path == "/login":
+                return self._send(200, login_page(), "text/html; charset=utf-8")
+
+            who = self.whoami()
+            if not who:
+                if path in ("/", "/index.html"):
+                    return self._send(200, login_page(),
+                                      "text/html; charset=utf-8")
+                return self._send(401, '{"error":"login required"}')
+
+            if path in ("/", "/index.html"):
+                return self._send(200, HTML, "text/html; charset=utf-8")
+            if path == "/whoami":
+                return self._send(200, json.dumps({"reviewer": who}))
+            if path == "/logout":
+                self.send_response(302)
+                self.send_header("Location", "/")
+                self.send_header("Set-Cookie", "rev=; Path=/; Max-Age=0")
+                self.send_header("Content-Length", "0")
+                self.end_headers()
+                return None
+
+            # Page image. R2 when configured -- redirect so the bytes go
+            # straight from R2 to the browser; local render otherwise.
+            if path in ("/page.png", "/page.img"):
+                pid = q.get("id", [""])[0]
+                if USE_R2 and pid:
+                    return self._redirect(r2_url(int(pid)))
                 png = page_png(q.get("pdf", [""])[0],
                                int(q.get("p", ["1"])[0]))
                 if not png:
                     return self._send(404, b"", "image/png")
                 return self._send(200, png, "image/png")
+
+            with db() as c, c.cursor() as cur:
+                if path == "/queue":
+                    return self._send(200, json.dumps(build_queue(cur, who)))
+                if path == "/doc":
+                    return self._send(200, json.dumps(
+                        load_doc(cur, int(q.get("id", ["0"])[0]), who)))
         except Exception as e:                                    # noqa: BLE001
             return self._send(500, json.dumps({"error": "%s: %s"
                                                % (type(e).__name__, e)}))
@@ -400,17 +574,31 @@ class Handler(SimpleHTTPRequestHandler):
     def do_POST(self):
         path = unquote(urlparse(self.path).path)
         n = int(self.headers.get("Content-Length") or 0)
+        body = self.rfile.read(n) or b""
         try:
-            p = json.loads(self.rfile.read(n) or b"{}")
+            if path == "/login":
+                form = parse_qs(body.decode("utf-8", "replace"))
+                name = (form.get("user", [""])[0] or "").strip().lower()
+                pw = form.get("pw", [""])[0] or ""
+                if name in USERS and hmac.compare_digest(USERS[name], pw):
+                    return self._redirect("/", make_cookie(name))
+                return self._send(401, login_page("Wrong name or password."),
+                                  "text/html; charset=utf-8")
+
+            who = self.whoami()
+            if not who:
+                return self._send(401, '{"error":"login required"}')
+
+            p = json.loads(body or b"{}")
             with db() as c, c.cursor() as cur:
                 if path == "/save":
                     save_page(cur, int(p["pageId"]), p.get("text", ""),
-                              p.get("tables") or [], p.get("note", ""))
+                              p.get("tables") or [], p.get("note", ""), who)
                 elif path == "/verdict":
                     v = p.get("verdict")
                     if v not in ("approved", "hold", None):
                         return self._send(400, '{"error":"bad verdict"}')
-                    verdict(cur, int(p["id"]), v)
+                    verdict(cur, int(p["id"]), v, who)
                 else:
                     return self._send(404, '{"error":"not found"}')
                 c.commit()
@@ -683,7 +871,7 @@ function render(){if(!D||!D.pages.length)return;
   $('pn').textContent=`${i+1}/${D.pages.length}`;
   $('fn').textContent=D.pdf||'(no pdf)';
   $('bc').textContent=`${p.bad}/${p.words}`;
-  $('img').src=D.pdf?`/page.png?pdf=${encodeURIComponent(D.pdf)}&p=${p.docPage}`:'';
+  $('img').src=`/page.img?id=${p.pageId}&pdf=${encodeURIComponent(D.pdf||'')}&p=${p.docPage}`;
   $('orig').innerHTML=inlineTables(marks(p.text,p.spans),p);
   markCells($('orig'),p);
   $('ed').value=p.corrected!=null?p.corrected:p.text;
@@ -761,16 +949,25 @@ boot();
 
 if __name__ == "__main__":
     with db() as c, c.cursor() as cur:
-        q = build_queue(cur)
-    print("OCR review  ->  http://127.0.0.1:%d" % PORT)
+        q = build_queue(cur, SOLO or (sorted(USERS) or ["alden"])[0])
+    print("OCR review  ->  http://%s:%d" % (HOST, PORT))
     print("%d documents over %.0f%% suspect words (threshold %.2f)"
           % (len(q), GATE, BAD_WORD))
-    print("corrections write to Neon as method='%s'; Mistral rows untouched"
+    print("corrections write to Neon as method='%s:<reviewer>'; Mistral untouched"
           % HUMAN)
-    threading.Timer(1.0, webbrowser.open,
-                    ("http://127.0.0.1:%d" % PORT,)).start()
-    # 127.0.0.1 only -- this serves probate documents.
+    print("page images : %s" % ("R2 %s/%s (signed, %ds)"
+                                % (R2_BUCKET, R2_PREFIX, R2_SIGN_TTL)
+                                if USE_R2 else "local render from %s" % RECUT))
+    print("auth        : %s" % (", ".join(sorted(USERS)) if USERS
+                                else "OPEN (solo mode as '%s')" % (SOLO or "alden")))
+    # Only pop a browser when a human is sitting at this machine. In a container
+    # there is no browser, and HOST is 0.0.0.0 because Coolify's proxy terminates
+    # TLS in front and forwards here.
+    if HOST.startswith("127.") and os.environ.get("NO_BROWSER") != "1":
+        threading.Timer(1.0, webbrowser.open,
+                        ("http://127.0.0.1:%d" % PORT,)).start()
+
     class S(socketserver.ThreadingTCPServer):
         allow_reuse_address = True
         daemon_threads = True
-    S(("127.0.0.1", PORT), Handler).serve_forever()
+    S((HOST, PORT), Handler).serve_forever()


Investigate per the method in your instructions, then return the findings list.

---

## 🤖 Claude

> 🔧 `Read` — C:\Users\busin\Documents\Document Splitting for Paperless\review\ocr_review_app.py

---

## 🤖 Claude

> 🔧 `Read` — C:\Users\busin\Documents\Paper-Oxidizer\.claude\worktrees\genius-extract-and-app\review\ocr_review_app.py

---

## 🤖 Claude

Looking at this diff, the review app is a Python HTTP server deployed to Coolify with new auth (HMAC-signed session cookies) and an R2 signed-URL redirect for page images. Let me trace the auth flow, cookie handling, and the local-render fallback carefully.

I already have the full source. Key concerns:

1. **`cookie_reviewer` fallback** — when `USERS` is empty (env `REVIEW_USERS` unset) AND `SOLO` is empty, it unconditionally returns `"alden"` for any request, treating anonymous visitors as an authenticated reviewer.
2. **`_redirect` Set-Cookie** — no `Secure` attribute; the app is exposed on `0.0.0.0:8778` in docker-compose behind a TLS proxy but also directly on the host.
3. **`page_png` path check** — `".." in pdf_name` doesn't block absolute paths, so `os.path.join(RECUT, "/etc/passwd")` returns `/etc/passwd`.

Let me quickly check upstream callers/flows to confirm these are exploitable end-to-end.

---

## 🤖 Claude

> 🔧 `Grep` — REVIEW_USERS|REVIEWER

---

## 🤖 Claude

> 🔧 `Grep` — os\.path\.join\(RECUT|page_png|\.\. in

---

## 🤖 Claude

Confirmed. The `cookie_reviewer` fallback at lines 448-449 returns `"alden"` for any request with no valid session when both `USERS` and `SOLO` are empty. The docker-compose passes `REVIEW_USERS: ${REVIEW_USERS}` directly from Coolify's env — if the operator forgets to set it (or a `$REVIEW_USERS=""` shell interpolation happens), the deployed app silently opens to the internet.

---

## 🤖 Claude

> 🔧 `StructuredOutput`
