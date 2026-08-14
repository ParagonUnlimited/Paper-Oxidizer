"""Inventory the reprocessed Genius Scan corpus and match it against v1.

    python inventory_v2.py

Reads  genius scan v2 from google drive/   (930 PDFs, downloaded 2026-08-13)
Writes v2-inventory.jsonl   one line per file: name, pages, sha256, bytes,
                            pdf creation date -- written INCREMENTALLY so a
                            crash loses nothing
       v2-merged-candidates.md   the files whose page counts match the seven
                            v1 merged PDFs (24/80/35/198/411/20/23 pages),
                            side by side for eyeball confirmation

Matching the seven matters most: their v1 boundary maps in boundaries/ and
splits/_built.json let us re-cut the improved images WITHOUT re-fingerprinting
791 pages -- but only if the v2 page structure is unchanged, which the page
counts here are the first check on.
"""
import hashlib, io, json, os, sys

import fitz

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "genius scan v2 from google drive")
OUT = os.path.join(BASE, "v2-inventory.jsonl")

V1_MERGED = {  # our v1 label -> page count
    "013-san-jose-water": 24, "014-santa-clara-dtac": 80, "015-we-oneil": 35,
    "017-pacific-power": 198, "018-stcu": 411, "077-greenwaste": 20,
    "078-unlabelled": 23,
}

done = set()
if os.path.exists(OUT):
    for line in io.open(OUT, encoding="utf-8"):
        try:
            done.add(json.loads(line)["name"])
        except Exception:
            pass

files = sorted(f for f in os.listdir(SRC) if f.lower().endswith(".pdf"))
print("files: %d  (already inventoried: %d)" % (len(files), len(done)))

with io.open(OUT, "a", encoding="utf-8") as out:
    for i, name in enumerate(files, 1):
        if name in done:
            continue
        p = os.path.join(SRC, name)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        try:
            d = fitz.open(p)
            pages, meta = len(d), (d.metadata or {})
            d.close()
            err = None
        except Exception as e:                                  # noqa: BLE001
            pages, meta, err = None, {}, str(e)[:120]
        out.write(json.dumps({
            "name": name, "pages": pages, "bytes": os.path.getsize(p),
            "sha256": h.hexdigest(),
            "pdf_created": meta.get("creationDate"),
            "producer": meta.get("producer"), "error": err,
        }, ensure_ascii=False) + "\n")
        out.flush()
        if i % 100 == 0:
            print("  %d/%d" % (i, len(files)))

# ------------------------------------------------- merged-PDF candidates
rows = [json.loads(l) for l in io.open(OUT, encoding="utf-8")]
rows = {r["name"]: r for r in rows}.values()          # last write wins
by_pages = {}
for r in rows:
    by_pages.setdefault(r["pages"], []).append(r["name"])

lines = ["# v2 candidates for the seven v1 merged PDFs\n",
         "| v1 merged (pages) | v2 files with the SAME page count |", "|---|---|"]
for label, n in sorted(V1_MERGED.items(), key=lambda x: -x[1]):
    cands = by_pages.get(n, [])
    lines.append("| %s (%d) | %s |" % (label, n,
                 "; ".join(cands) if cands else "**NONE FOUND**"))
lines.append("\nTotal v2 files: %d · distinct page counts: %d · "
             "files >=20 pages: %d"
             % (len(rows), len(by_pages),
                sum(1 for r in rows if (r["pages"] or 0) >= 20)))
big = sorted((r for r in rows if (r["pages"] or 0) >= 20),
             key=lambda r: -r["pages"])
lines.append("\n## Every v2 file with >=20 pages (merged-PDF suspects)\n")
for r in big:
    lines.append("- %4d pp  %s" % (r["pages"], r["name"]))
io.open(os.path.join(BASE, "v2-merged-candidates.md"), "w",
        encoding="utf-8").write("\n".join(lines))
print("wrote v2-inventory.jsonl (%d rows) and v2-merged-candidates.md"
      % len(rows))
