# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf>=1.24", "numpy>=1.26"]
# ///
"""Did 013's 'deleted' pages 23/24 really vanish, or were they re-split elsewhere?

The alignment only compared 013 against its one candidate file, so 'deleted' there
means 'not in THAT file'. 077/078 taught us pages get re-split into other documents.
This searches the whole v2 corpus for them, same as the 077/078 hunt.
"""
import io
import json
import os
import re

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
V2 = os.path.join(HERE, "genius scan v2 from google drive")
CACHE = os.path.join(HERE, "align-cache")
MIN_TOKENS = 12


def toks_from_cache(path):
    st = os.stat(path)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path))
    ck = os.path.join(CACHE, "%s-%d.tok.json" % (safe, st.st_size))
    if os.path.exists(ck):
        return [set(x) for x in json.load(io.open(ck, encoding="utf-8"))]
    doc = fitz.open(path)
    out = [sorted({t for t in re.findall(r"[a-z0-9]+", p.get_text().lower()) if len(t) > 1})
           for p in doc]
    doc.close()
    json.dump(out, io.open(ck, "w", encoding="utf-8"))
    return [set(x) for x in out]


def jac(a, b):
    if not a or not b:
        return 0.0
    i = len(a & b)
    return i / (len(a) + len(b) - i) if i else 0.0


corpus, index = [], []
files = sorted(f for f in os.listdir(V2) if f.lower().endswith(".pdf"))
for f in files:
    T = toks_from_cache(os.path.join(V2, f))
    corpus.extend(T)
    index.extend((f, p + 1) for p in range(len(T)))
print("v2 corpus: %d pages from %d files" % (len(corpus), len(files)))

targets = [("013-san-jose-water-24pp.pdf", 23), ("013-san-jose-water-24pp.pdf", 24)]
A = toks_from_cache(os.path.join(RAW, "013-san-jose-water-24pp.pdf"))
for name, pno in targets:
    q = A[pno - 1]
    print()
    print("=== %s page %d  (%d distinct words) ===" % (name, pno, len(q)))
    scores = [(jac(q, c), i) for i, c in enumerate(corpus)]
    scores.sort(reverse=True)
    for s, i in scores[:5]:
        print("   %.3f  %-30s p%d" % (s, index[i][0], index[i][1]))
    print("   verdict: %s" % ("LIKELY FOUND elsewhere in v2" if scores[0][0] >= 0.55
                              else "no convincing match anywhere in v2 -> genuinely absent"))
