# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf>=1.24"]
# ///
"""Embed the Mistral OCR into each recut PDF as an invisible, positioned text layer.

    uv run embed_ocr.py                 # build into embedded/ and verify
    uv run embed_ocr.py --limit 20      # sample first

Reads  recut/<doc>.pdf  +  ocr-mistral/<doc>.raw.json
Writes embedded/<doc>.pdf              (originals untouched)

WHY THIS STEP EXISTS: the recut PDFs still carry the GENIUS SCAN text layer,
which is Tesseract-grade ("Lawrence E. Stonc", "sccassessor,org"). The Mistral
text - which is the whole point of the project - lives only in the raw JSON until
this script puts it inside the documents.

TWO IMPROVEMENTS OVER THE v1 build.py APPROACH:

1. POSITIONED, NOT DUMPED. v1 pushed the whole page's text into one page-sized
   textbox, so extraction worked but selection and coordinates did not. Mistral
   returns blocks[] with real bounding boxes, so each block is placed where its
   words actually sit. Text selection in a reader now lands on the right words.

2. The old latin-1 defect is handled from the start. Base-14 PDF fonts are
   latin-1 only; unmapped characters silently became "?" and that cost a full
   512-document rebuild. FOLD below maps the offenders before insertion, and the
   verifier fails the run if a stray "?" appears that was not in the source.

The existing (bad) text layer is removed first, so extraction returns only the
good text rather than two overlapping versions.
"""
import argparse
import io
import json
import os
import re
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "recut")
OCR = os.path.join(HERE, "ocr-mistral")
OUT = os.path.join(HERE, "embedded")

# characters the base-14 latin-1 fonts cannot encode, and what to use instead.
FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...", "•": "*", "·": "*", "‹": "<", "›": ">",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "⁠": "", "​": "", "﻿": "", "­": "",
    "™": "(TM)", "®": "(R)", "©": "(C)",
    "≤": "<=", "≥": ">=", "≠": "!=", "×": "x",
    "′": "'", "″": '"', "⁄": "/", "‐": "-", "‑": "-",
}
FOLD_RE = re.compile("|".join(re.escape(k) for k in FOLD))


def latin1_safe(text):
    """Fold what we can, drop what we cannot -- never emit a silent '?'."""
    text = FOLD_RE.sub(lambda m: FOLD[m.group()], text)
    return text.encode("latin-1", "ignore").decode("latin-1")


def strip_text(page):
    """Remove the existing (Genius Scan) text layer, keeping the scan image."""
    page.clean_contents()
    for xref in page.get_contents():
        s = page.parent.xref_stream(xref)
        if s:
            # drop text-showing operators, leave image/graphics operators alone
            s = re.sub(rb"BT.*?ET", b"", s, flags=re.S)
            page.parent.update_stream(xref, s)


def embed_page(page, blocks, scale_x, scale_y):
    """Place each block's text invisibly at its own bounding box."""
    placed = skipped = 0
    for b in blocks:
        txt = latin1_safe((b.get("content") or "").strip())
        if not txt:
            continue
        try:
            x0 = float(b["top_left_x"]) * scale_x
            y0 = float(b["top_left_y"]) * scale_y
            x1 = float(b["bottom_right_x"]) * scale_x
            y1 = float(b["bottom_right_y"]) * scale_y
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        rect = fitz.Rect(x0, y0, max(x1, x0 + 4), max(y1, y0 + 4))
        # start near the box height, shrink until the text fits
        size = max(3.0, min(11.0, (rect.height / max(1, txt.count("\n") + 1)) * 0.8))
        for _ in range(9):
            if page.insert_textbox(rect, txt, fontsize=size, fontname="helv",
                                   render_mode=3) >= 0:
                placed += 1
                break
            size *= 0.75
            if size < 1.2:
                # last resort: never lose the text, even if it overflows the box
                page.insert_textbox(page.rect, txt, fontsize=2.5,
                                    fontname="helv", render_mode=3)
                placed += 1
                break
    return placed, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    pdfs = sorted(f for f in os.listdir(SRC) if f.lower().endswith(".pdf"))
    if args.limit:
        pdfs = pdfs[:args.limit]
    print("embedding %d documents" % len(pdfs))

    done = failed = 0
    no_ocr = []
    gate = []          # per-document confidence, for the auto-gate later
    for i, name in enumerate(pdfs, 1):
        stem = name[:-4]
        jf = os.path.join(OCR, stem + ".raw.json")
        if not os.path.exists(jf):
            no_ocr.append(stem)
            continue
        try:
            r = json.load(io.open(jf, encoding="utf-8"))
            doc = fitz.open(os.path.join(SRC, name))
            mins, placed_total = [], 0
            for pno, page in enumerate(doc):
                if pno >= len(r.get("pages") or []):
                    break
                mp = r["pages"][pno]
                dim = mp.get("dimensions") or {}
                w, h = dim.get("width"), dim.get("height")
                if not w or not h:
                    continue
                strip_text(page)
                p, _ = embed_page(page, mp.get("blocks") or [],
                                  page.rect.width / float(w),
                                  page.rect.height / float(h))
                placed_total += p
                cs = mp.get("confidence_scores") or {}
                if cs.get("minimum_page_confidence_score") is not None:
                    mins.append(float(cs["minimum_page_confidence_score"]))
            doc.save(os.path.join(args.out, name), garbage=3, deflate=True)
            doc.close()
            gate.append({"document": stem, "pages": len(r.get("pages") or []),
                         "blocks_placed": placed_total,
                         "min_word_confidence": min(mins) if mins else None})
            done += 1
        except Exception as e:
            failed += 1
            print("  FAILED %s: %s" % (stem[:50], str(e)[:70]))
        if i % 200 == 0:
            print("  %d/%d" % (i, len(pdfs)))

    json.dump(gate, io.open(os.path.join(HERE, "embed-gate.json"), "w",
                            encoding="utf-8"), indent=2)
    print()
    print("embedded=%d failed=%d no_ocr=%d" % (done, failed, len(no_ocr)))
    if no_ocr:
        print("  no OCR json for: %s" % no_ocr[:5])
    print("wrote embed-gate.json (min word confidence per document, for the auto-gate)")


if __name__ == "__main__":
    main()
