# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf>=1.24"]
# ///
"""Prove the re-cut is lossless: EVERY cut page, not a sample.

For each page of each cut document, compare against the v2 source page:
  - SHA256 of every embedded image stream (the actual stored bytes)
  - pixel dimensions and colourspace
  - compression filter (JPEG stays JPEG; nothing re-encoded)

Also byte-compares the pass-through files against their originals.
"""
import hashlib
import io
import json
import os

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.join(HERE, "genius scan v2 from google drive")
PLAN = json.load(io.open(os.path.join(HERE, "recut-plan.json"), encoding="utf-8"))
OUT = os.path.join(HERE, "recut")


def page_images(doc, pno):
    """[(sha256, width, height, colorspace, filter)] for every image on the page."""
    out = []
    for info in sorted(doc[pno].get_images(full=True)):
        try:
            d = doc.extract_image(info[0])
            out.append((hashlib.sha256(d["image"]).hexdigest(),
                        d.get("width"), d.get("height"),
                        d.get("colorspace"), d.get("ext")))
        except Exception as e:
            out.append(("<err:%s>" % str(e)[:20], None, None, None, None))
    return out


def safe(name):
    import re
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:150]


src_cache = {}


def src(name):
    if name not in src_cache:
        src_cache[name] = fitz.open(os.path.join(V2, name))
    return src_cache[name]


pages_checked = 0
imgs_checked = 0
mismatches = []
no_image = []

groups = list(PLAN["documents_from_v1"].items()) + list(PLAN["documents_new"].items())
print("checking every page of %d cut documents ..." % len(groups))
for k, (key, refs) in enumerate(sorted(groups), 1):
    name = safe(key if key.lower().endswith(".pdf") else key + ".pdf")
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        mismatches.append((name, 0, "OUTPUT MISSING"))
        continue
    d = fitz.open(path)
    refs = sorted(refs, key=lambda r: r["position"])
    for i, r in enumerate(refs):
        got = page_images(d, i)
        want = page_images(src(r["v2_file"]), r["page"] - 1)
        pages_checked += 1
        imgs_checked += len(want)
        if not want:
            no_image.append((name, i + 1))
        if got != want:
            mismatches.append((name, i + 1,
                               "got %s want %s" % (got[:1], want[:1])))
    d.close()
    if k % 150 == 0:
        print("  %d/%d documents, %d pages checked" % (k, len(groups), pages_checked))

print()
print("=" * 70)
print("CUT PAGES")
print("=" * 70)
print("  pages compared against their v2 source : %d" % pages_checked)
print("  embedded image streams compared        : %d" % imgs_checked)
print("  pages with no embedded image           : %d" % len(no_image))
print("  MISMATCHES                             : %d" % len(mismatches))
for m in mismatches[:8]:
    print("     %s p%s  %s" % m)

print()
print("=" * 70)
print("PASS-THROUGH FILES (should be byte-identical copies)")
print("=" * 70)
bad_bytes = 0
checked = 0
for f in PLAN["passthrough_files"] + PLAN["whole_file_documents"]:
    a = os.path.join(V2, f)
    b = os.path.join(OUT, safe(f))
    if not os.path.exists(b):
        bad_bytes += 1
        continue
    ha = hashlib.sha256(open(a, "rb").read()).hexdigest()
    hb = hashlib.sha256(open(b, "rb").read()).hexdigest()
    checked += 1
    if ha != hb:
        bad_bytes += 1
        if bad_bytes <= 5:
            print("     DIFFERS: %s" % f)
print("  files compared : %d" % checked)
print("  byte-identical : %s" % ("ALL" if bad_bytes == 0 else "*** %d DIFFER ***" % bad_bytes))

print()
ok = not mismatches and bad_bytes == 0
print("VERDICT: %s" % ("LOSSLESS - every scan image carried across unchanged"
                       if ok else "*** QUALITY LOSS DETECTED - see above ***"))
for d in src_cache.values():
    d.close()
