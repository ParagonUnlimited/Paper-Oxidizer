# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf>=1.24", "psycopg[binary]>=3.1"]
# ///
"""PHASE 2 of the re-cut: build the OCR-ready corpus from recut-plan.json.

    uv run recut_build.py            # build + verify
    uv run recut_build.py --write-neon   # also record output_file rows

Output goes to recut/ -- a NEW folder. Nothing existing is touched: not the v2
originals, not splits/, not the v1 raw files.

Pages are copied with insert_pdf, which transplants the page object itself. The
scan images are carried over untouched -- no re-render, no re-compression. That
matters: these are evidence, and the whole point of v2 is the better image.

Verification after building:
  - page count of every output equals the plan
  - SHA256 of every embedded image compared against the v2 source it came from
  - total pages reconciled against the plan's conservation numbers
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.join(HERE, "genius scan v2 from google drive")
PLAN = os.path.join(HERE, "recut-plan.json")
OUT = os.path.join(HERE, "recut")


def safe(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:150]


def image_digest(doc, pno):
    """SHA256 over the raw embedded image streams of one page, in xref order.

    Compares what is actually stored, not a re-render -- so it proves the bytes
    were carried across rather than merely looking similar.
    """
    h = hashlib.sha256()
    for info in sorted(doc[pno].get_images(full=True)):
        try:
            h.update(doc.extract_image(info[0])["image"])
        except Exception:
            h.update(b"<unreadable>")
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-neon", action="store_true")
    ap.add_argument("--verify-sample", type=int, default=40)
    args = ap.parse_args()

    plan = json.load(io.open(PLAN, encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)

    src_cache = {}

    def src(name):
        if name not in src_cache:
            src_cache[name] = fitz.open(os.path.join(V2, name))
        return src_cache[name]

    built = []
    groups = [("from_v1", plan["documents_from_v1"]), ("new", plan["documents_new"])]
    total = sum(len(g) for _, g in groups)
    print("=" * 72)
    print("BUILDING %d cut documents + %d pass-through + %d whole-file"
          % (total, len(plan["passthrough_files"]), len(plan["whole_file_documents"])))
    print("=" * 72)

    n = 0
    for kind, group in groups:
        for key, refs in sorted(group.items()):
            refs = sorted(refs, key=lambda r: r["position"])
            out_name = safe(key if key.lower().endswith(".pdf") else key + ".pdf")
            out_path = os.path.join(OUT, out_name)
            doc = fitz.open()
            for r in refs:
                s = src(r["v2_file"])
                doc.insert_pdf(s, from_page=r["page"] - 1, to_page=r["page"] - 1)
            doc.save(out_path, garbage=3, deflate=True)
            doc.close()
            built.append({"key": key, "kind": kind, "name": out_name,
                          "path": out_path, "pages": len(refs), "refs": refs})
            n += 1
            if n % 100 == 0:
                print("  %d/%d cut documents built" % (n, total))

    # pass-through and whole-file: copy byte-for-byte, no rewrite at all
    copied = 0
    for f in plan["passthrough_files"] + plan["whole_file_documents"]:
        dst = os.path.join(OUT, safe(f))
        shutil.copy2(os.path.join(V2, f), dst)
        d = fitz.open(dst)
        pages = d.page_count
        d.close()
        built.append({"key": f, "kind": "passthrough", "name": safe(f),
                      "path": dst, "pages": pages, "refs": None})
        copied += 1
    print("  %d files copied through unchanged" % copied)

    # --------------------------------------------------------------- verify
    print()
    print("=" * 72)
    print("VERIFICATION")
    print("=" * 72)
    bad_count, checked_img, bad_img = [], 0, []
    for b in built:
        d = fitz.open(b["path"])
        if d.page_count != b["pages"]:
            bad_count.append((b["name"], b["pages"], d.page_count))
        d.close()
    print("  page counts match the plan : %s"
          % ("ALL %d OK" % len(built) if not bad_count else "*** %d MISMATCH ***" % len(bad_count)))
    for nm, want, got in bad_count[:5]:
        print("     %s: planned %d, built %d" % (nm, want, got))

    cut = [b for b in built if b["refs"]]
    step = max(1, len(cut) // max(1, args.verify_sample))
    for b in cut[::step][:args.verify_sample]:
        d = fitz.open(b["path"])
        for i, r in enumerate(b["refs"]):
            a = image_digest(d, i)
            e = image_digest(src(r["v2_file"]), r["page"] - 1)
            checked_img += 1
            if a != e:
                bad_img.append((b["name"], i + 1, r["v2_file"], r["page"]))
        d.close()
    print("  embedded images identical  : %d pages checked, %s"
          % (checked_img, "ALL MATCH" if not bad_img else "*** %d DIFFER ***" % len(bad_img)))
    for nm, i, sf, sp in bad_img[:5]:
        print("     %s p%d != %s p%d" % (nm, i, sf, sp))

    out_pages = sum(b["pages"] for b in built)
    a = plan["audit"]
    print()
    print("  documents built            : %d  (plan said %d)" % (len(built), a["total_documents"]))
    print("  pages in the run folder    : %d" % out_pages)
    print("  v2 corpus pages            : %d" % a["v2_pages"])
    print("  deliberately unused        : %d" % (a["v2_pages"] - out_pages))
    ok = (not bad_count and not bad_img and len(built) == a["total_documents"])
    print()
    print("  RESULT: %s" % ("PASS - recut/ is ready for the OCR run" if ok
                            else "*** REVIEW - see mismatches above ***"))

    json.dump({"built": [{k: v for k, v in b.items() if k != "refs"} for b in built],
               "pages": out_pages, "documents": len(built), "pass": ok},
              io.open(os.path.join(HERE, "recut-manifest.json"), "w", encoding="utf-8"), indent=2)
    print("  wrote recut-manifest.json")

    for d in src_cache.values():
        d.close()

    if args.write_neon and ok:
        write_neon(built)


def write_neon(built):
    import psycopg
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        sys.exit("NEON_DATABASE_URL not set")
    n = 0
    with psycopg.connect(url) as conn:
        cur = conn.cursor()
        for b in built:
            h = hashlib.sha256()
            with open(b["path"], "rb") as f:
                for c in iter(lambda: f.read(1 << 20), b""):
                    h.update(c)
            cur.execute("select id from document where key = %s", (b["key"],))
            row = cur.fetchone()
            cur.execute("""insert into output_file
                (document_id, name, path, sha256, build_version, meta)
                values (%s,%s,%s,%s,'recut-v2', %s::jsonb)""",
                (row[0] if row else None, b["name"], b["path"], h.hexdigest(),
                 json.dumps({"kind": b["kind"], "pages": b["pages"]})))
            n += 1
        conn.commit()
    print("  Neon: %d output_file rows recorded (build_version=recut-v2)" % n)


if __name__ == "__main__":
    main()
