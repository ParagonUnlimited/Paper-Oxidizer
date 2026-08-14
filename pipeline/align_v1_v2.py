# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf>=1.24", "numpy>=1.26", "pillow>=10", "psycopg[binary]>=3.1"]
# ///
"""Map v1 merged-container pages onto their v2 (reprocessed) equivalents.

    uv run align_v1_v2.py            # read-only: renders, matches, writes JSON + contact sheets
    uv run align_v1_v2.py --write-neon   # additionally records results into Neon meta

ZERO AI TOKENS. Pure local arithmetic on downscaled page images.

Why this exists: the v1 containers carry all the splitting work (which pages form
which document, in what order). The v2 files carry the better images. To re-cut v2
using v1 boundaries we need to know, for every v1 page, which v2 page it is.

Method: render each page to a fixed 64x64 grayscale grid, z-normalise it, and
compare with cosine similarity. Z-normalising per page is what absorbs the tonal
difference from the destructive filters being removed in v2 -- the layout survives,
the brightness/contrast does not matter.

Three gates, each of which can fail loudly:
  A. IDENTITY  -- for the 4 exact-page-count pairs the claim is v1 page i == v2 page i.
                  We test that hypothesis directly rather than globally best-matching,
                  because this corpus is full of near-identical layouts (411 pages of
                  the same statement template) and global matching invites cross-matches.
  B. 013 24->22 -- try every two-deletion hypothesis and take the best. The gate is
                  EXACTLY 2 deletions; if the best alignment wants a different number,
                  we stop and report rather than forcing it.
  C. 077/078 HUNT -- 43 pages with no v2 counterpart, searched against every page of
                  all 930 v2 files. "Not found" is a permitted, meaningful outcome:
                  unfound pages are evidence for the open 941-vs-930 discrepancy.

Thresholds are calibrated from this corpus, not guessed: the genuine-match
distribution comes from the ~700 confirmed same-page pairs in gate A, the impostor
distribution from randomly sampled non-matching pairs.

Signatures are cached to align-cache/ keyed by name+size+pagecount, so re-runs and
threshold tweaking are free.
"""

import argparse
import io
import json
import os
import random
import re
import sys

import fitz
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
V2 = os.path.join(HERE, "genius scan v2 from google drive")
CACHE = os.path.join(HERE, "align-cache")
OUT_JSON = os.path.join(HERE, "v1-v2-alignment.json")
SHEETS = os.path.join(HERE, "alignment-sheets")

GRID = 64  # signature is GRID*GRID = 4096 dims

# v1 containers and their recorded page counts (asserted against the files)
V1_EXPECTED = {
    "013-san-jose-water-24pp.pdf": 24,
    "014-santa-clara-dtac-80pp.pdf": 80,
    "015-we-oneil-35pp.pdf": 35,
    "017-pacific-power-198pp.pdf": 198,
    "018-stcu-411pp.pdf": 411,
    "077-greenwaste-20pp.pdf": 20,
    "078-unlabelled-23pp.pdf": 23,
}

# exact page-count matches from v2-merged-candidates.md
IDENTITY_PAIRS = [
    ("018-stcu-411pp.pdf", "2026-05-11 17-28.pdf"),
    ("017-pacific-power-198pp.pdf", "2026-05-10 23-18.pdf"),
    ("014-santa-clara-dtac-80pp.pdf", "2026-05-09 22-46.pdf"),
    ("015-we-oneil-35pp.pdf", "2026-05-09 23-16.pdf"),
]
# no exact count match; content-confirmed candidate, expected to have lost 2 pages
DELETION_PAIR = ("013-san-jose-water-24pp.pdf", "2026-05-09 20-00.pdf", 2)
HUNT_FILES = ["077-greenwaste-20pp.pdf", "078-unlabelled-23pp.pdf"]


def cache_key(path):
    st = os.stat(path)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path))
    return os.path.join(CACHE, "%s-%d.npy" % (safe, st.st_size))


def signatures(path, label=None):
    """(n_pages, GRID*GRID) float32, z-normalised per page. Cached to disk."""
    ck = cache_key(path)
    if os.path.exists(ck):
        return np.load(ck)
    doc = fitz.open(path)
    rows = []
    for page in doc:
        r = page.rect
        if r.width <= 0 or r.height <= 0:
            rows.append(np.zeros(GRID * GRID, dtype=np.float32))
            continue
        # render straight to GRID x GRID -- never full-res then downscale.
        # Non-uniform scale also normalises away aspect-ratio differences.
        m = fitz.Matrix(GRID / r.width, GRID / r.height)
        pix = page.get_pixmap(matrix=m, colorspace=fitz.csGRAY, alpha=False)
        a = np.frombuffer(pix.samples, dtype=np.uint8).astype(np.float32)
        if a.size != GRID * GRID:  # defensive: rounding can yield +/-1 px
            a = np.resize(a, GRID * GRID)
        a -= a.mean()
        s = a.std()
        rows.append(a / s if s > 1e-6 else a)
    doc.close()
    sig = np.array(rows, dtype=np.float32)
    os.makedirs(CACHE, exist_ok=True)
    np.save(ck, sig)
    if label:
        print("    rendered %-46s %4d pages" % (label, len(sig)))
    return sig


def cos(A, B):
    """Cosine similarity matrix between z-normalised signature sets."""
    An = A / (np.linalg.norm(A, axis=1, keepdims=True) + 1e-9)
    Bn = B / (np.linalg.norm(B, axis=1, keepdims=True) + 1e-9)
    return An @ Bn.T


# --------------------------------------------------------------------------
# TEXT signatures. Both v1 and v2 carry a Genius Scan text layer, and text is
# far more discriminative than a thumbnail here: 411 pages of the same statement
# template are visually near-identical but differ in every account number, date
# and amount. Thumbnails were tried first and the calibration gate rejected them
# (impostor scores reached 0.880 against a genuine floor of 0.111).
# --------------------------------------------------------------------------
MIN_TOKENS = 12  # below this a page is treated as text-poor -> thumbnail fallback


def text_tokens(path, label=None):
    """[set(tokens)] per page, cached. Tokens are lowercased alphanumeric runs."""
    ck = cache_key(path).replace(".npy", ".tok.json")
    if os.path.exists(ck):
        return [set(x) for x in json.load(io.open(ck, encoding="utf-8"))]
    doc = fitz.open(path)
    out = []
    for page in doc:
        toks = re.findall(r"[a-z0-9]+", page.get_text().lower())
        # keep multiplicity out but preserve rare-token power; drop 1-char noise
        out.append(sorted({t for t in toks if len(t) > 1}))
    doc.close()
    os.makedirs(CACHE, exist_ok=True)
    json.dump(out, io.open(ck, "w", encoding="utf-8"))
    if label:
        n = sum(1 for s in out if len(s) >= MIN_TOKENS)
        print("    text %-46s %4d pages (%d with usable text)" % (label, len(out), n))
    return [set(x) for x in out]


def neon_page_text(v1_name, n_pages):
    """Token sets from the vision OCR already stored in Neon.

    077 and 078 carry NO text layer in their PDFs -- Genius Scan never OCR'd
    them, which is why the PDF-text path returns nothing for those 43 pages.
    But we OCR'd them during the original split, so the text exists in
    ocr_reading; use it rather than falling back to weak image matching.
    """
    import psycopg
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        print("    (NEON_DATABASE_URL not set - cannot recover OCR text)")
        return None
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            """select p.page_no, r.text
               from source_page p
               join source_file f on f.id = p.file_id
               join ocr_reading r on r.page_id = p.id
               where f.name = %s and r.method = 'vision_v1'
               order by p.page_no""", (v1_name,)).fetchall()
    out = [set() for _ in range(n_pages)]
    for page_no, text in rows:
        if 1 <= page_no <= n_pages:
            out[page_no - 1] = {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
                                if len(t) > 1}
    got = sum(1 for s in out if len(s) >= MIN_TOKENS)
    print("    recovered vision_v1 OCR from Neon: %d/%d pages with usable text" % (got, n_pages))
    return out


def jaccard_matrix(A, B):
    """|A n B| / |A u B| for every pair of token sets."""
    M = np.zeros((len(A), len(B)), dtype=np.float32)
    for i, a in enumerate(A):
        if not a:
            continue
        la = len(a)
        for j, b in enumerate(B):
            if not b:
                continue
            inter = len(a & b)
            if inter:
                M[i, j] = inter / (la + len(b) - inter)
    return M


def hybrid_matrix(p1, p2, l1=None, l2=None):
    """Text Jaccard where both pages have usable text, thumbnail cosine otherwise.

    Returns (scores, method) where method[i,j] is 't' or 'i'.
    """
    T1, T2 = text_tokens(p1, l1), text_tokens(p2, l2)
    M = jaccard_matrix(T1, T2)
    method = np.full(M.shape, "t", dtype="<U1")
    poor1 = [i for i, s in enumerate(T1) if len(s) < MIN_TOKENS]
    poor2 = [j for j, s in enumerate(T2) if len(s) < MIN_TOKENS]
    if poor1 or poor2:
        I = cos(signatures(p1), signatures(p2))
        for i in poor1:
            M[i, :], method[i, :] = I[i, :], "i"
        for j in poor2:
            M[:, j], method[:, j] = I[:, j], "i"
    return M, method


def page_png(path, page_no, width=210):
    doc = fitz.open(path)
    page = doc[page_no]
    m = fitz.Matrix(width / page.rect.width, width / page.rect.width)
    png = page.get_pixmap(matrix=m, colorspace=fitz.csGRAY, alpha=False).tobytes("png")
    doc.close()
    return png


def contact_sheet(rows, out_path, title):
    """rows: list of (caption, [(label, pngbytes), ...]) -> one side-by-side PNG."""
    from PIL import Image, ImageDraw

    tiles = []
    for caption, items in rows:
        imgs = [(lb, Image.open(io.BytesIO(b))) for lb, b in items]
        h = max(i.height for _, i in imgs) + 34
        w = sum(i.width for _, i in imgs) + 12 * (len(imgs) + 1)
        strip = Image.new("L", (w, h), 255)
        d = ImageDraw.Draw(strip)
        d.text((6, 4), caption, fill=0)
        x = 12
        for lb, im in imgs:
            strip.paste(im, (x, 28))
            d.text((x, 16), lb, fill=0)
            x += im.width + 12
        tiles.append(strip)
    if not tiles:
        return None
    W = max(t.width for t in tiles)
    H = sum(t.height for t in tiles) + 26
    sheet = Image.new("L", (W, H), 255)
    ImageDraw.Draw(sheet).text((6, 6), title, fill=0)
    y = 26
    for t in tiles:
        sheet.paste(t, (0, y))
        y += t.height
    os.makedirs(SHEETS, exist_ok=True)
    sheet.save(out_path)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-neon", action="store_true",
                    help="record results into Neon source_page.meta (default: read-only)")
    ap.add_argument("--grid", type=int, default=GRID)
    args = ap.parse_args()

    result = {"grid": args.grid, "gates": {}}

    # ---------------------------------------------------------------- preflight
    print("=" * 72)
    print("PREFLIGHT - page counts must match what Neon recorded")
    bad = []
    for name, expected in sorted(V1_EXPECTED.items()):
        p = os.path.join(RAW, name)
        if not os.path.exists(p):
            bad.append("%s MISSING" % name)
            continue
        d = fitz.open(p)
        n = d.page_count
        d.close()
        flag = "ok" if n == expected else "*** MISMATCH ***"
        if n != expected:
            bad.append("%s has %d pages, Neon says %d" % (name, n, expected))
        print("  %-34s %4d pages  (expected %4d) %s" % (name, n, expected, flag))
    if bad:
        sys.exit("PREFLIGHT FAILED:\n  " + "\n  ".join(bad))

    # ------------------------------------------------------- gate A: identity
    print()
    print("=" * 72)
    print("GATE A - identity hypothesis: v1 page i == v2 page i")
    genuine = []
    identity = {}
    for v1n, v2n in IDENTITY_PAIRS:
        p1, p2 = os.path.join(RAW, v1n), os.path.join(V2, v2n)
        A = text_tokens(p1, v1n)
        B = text_tokens(p2, v2n)
        if len(A) != len(B):
            sys.exit("count mismatch %s(%d) vs %s(%d)" % (v1n, len(A), v2n, len(B)))
        M, method = hybrid_matrix(p1, p2)
        diag = np.diag(M).copy()
        # for each v1 page, is the same-index v2 page also the BEST match?
        best = M.argmax(axis=1)
        best_is_identity = int((best == np.arange(len(A))).sum())
        # margin: identity score minus the best non-identity score on that row
        off = M.copy()
        np.fill_diagonal(off, -np.inf)
        margin = diag - off.max(axis=1)
        genuine.extend(diag.tolist())
        identity[v1n] = {
            "v2_file": v2n, "pages": int(len(A)),
            "identity_score_min": round(float(diag.min()), 4),
            "identity_score_mean": round(float(diag.mean()), 4),
            "best_match_is_identity": best_is_identity,
            "pages_where_another_page_scored_higher": int(len(A) - best_is_identity),
            "margin_min": round(float(margin.min()), 4),
            "worst_pages": [int(i + 1) for i in np.argsort(diag)[:5]],
            "pages_decided_by_image_fallback": int((np.diag(method) == "i").sum()),
        }
        print("  %-34s -> %-24s %4d pp | identity best on %d/%d | min %.3f mean %.3f"
              % (v1n, v2n, len(A), best_is_identity, len(A), diag.min(), diag.mean()))
    result["gates"]["A_identity"] = identity

    # --------------------------------------------- calibration: impostor scores
    print()
    print("=" * 72)
    print("CALIBRATION - genuine vs impostor score distributions")
    rng = random.Random(20260813)
    impostor = []
    for v1n, v2n in IDENTITY_PAIRS:
        p1, p2 = os.path.join(RAW, v1n), os.path.join(V2, v2n)
        M, _ = hybrid_matrix(p1, p2)
        n = M.shape[0]
        if n < 3:
            continue
        for _ in range(min(400, n * 3)):
            i, j = rng.randrange(n), rng.randrange(n)
            if i != j:
                impostor.append(float(M[i, j]))
    g = np.array(genuine)
    im = np.array(impostor)
    print("  genuine  n=%4d  min %.3f  p01 %.3f  mean %.3f" % (len(g), g.min(), np.percentile(g, 1), g.mean()))
    print("  impostor n=%4d  max %.3f  p99 %.3f  mean %.3f" % (len(im), im.max(), np.percentile(im, 99), im.mean()))
    thr = float((np.percentile(g, 1) + np.percentile(im, 99)) / 2)
    print("  --> threshold (midpoint of genuine p01 and impostor p99): %.3f" % thr)
    separated = float(np.percentile(g, 1)) > float(np.percentile(im, 99))
    print("  --> distributions %s" % ("SEPARATE (clean)" if separated else "*** OVERLAP - treat matches as advisory ***"))
    result["calibration"] = {
        "genuine_n": len(g), "genuine_min": round(float(g.min()), 4),
        "genuine_p01": round(float(np.percentile(g, 1)), 4), "genuine_mean": round(float(g.mean()), 4),
        "impostor_n": len(im), "impostor_max": round(float(im.max()), 4),
        "impostor_p99": round(float(np.percentile(im, 99)), 4), "impostor_mean": round(float(im.mean()), 4),
        "threshold": round(thr, 4), "distributions_separate": separated,
    }

    # ------------------------------------------------- gate B: 013 with deletions
    print()
    print("=" * 72)
    v1n, v2n, expect_del = DELETION_PAIR
    print("GATE B - %s -> %s  (expect exactly %d deletions)" % (v1n, v2n, expect_del))
    M, _ = hybrid_matrix(os.path.join(RAW, v1n), os.path.join(V2, v2n), v1n, v2n)
    n1, n2 = M.shape
    need_del = n1 - n2
    print("  v1 %d pages, v2 %d pages -> %d deletion(s) required" % (n1, n2, need_del))
    from itertools import combinations
    best_score, best_combo = -1e9, None
    for combo in combinations(range(n1), need_del):
        keep = [i for i in range(n1) if i not in combo]
        s = float(M[keep, np.arange(n2)].sum())
        if s > best_score:
            best_score, best_combo = s, combo
    kept = [i for i in range(n1) if i not in best_combo]
    per_page = M[kept, np.arange(n2)]
    deleted_pages = [int(i + 1) for i in best_combo]
    gateB_ok = (need_del == expect_del) and bool((per_page >= thr).all())
    print("  best alignment deletes v1 page(s): %s" % deleted_pages)
    print("  kept-page scores: min %.3f  mean %.3f" % (per_page.min(), per_page.mean()))
    print("  GATE B: %s" % ("PASS" if gateB_ok else "*** REVIEW - see report ***"))
    result["gates"]["B_013_deletions"] = {
        "v1_file": v1n, "v2_file": v2n, "v1_pages": n1, "v2_pages": n2,
        "deletions_required": need_del, "deletions_expected": expect_del,
        "deleted_v1_pages": deleted_pages,
        "map_v1_to_v2": {int(i + 1): int(j + 1) for j, i in enumerate(kept)},
        "score_min": round(float(per_page.min()), 4),
        "score_mean": round(float(per_page.mean()), 4),
        "pass": gateB_ok,
    }

    # ---------------------------------------------------- gate C: 077/078 hunt
    print()
    print("=" * 72)
    print("GATE C - hunt 077/078 pages across ALL v2 files")
    v2_files = sorted(f for f in os.listdir(V2) if f.lower().endswith(".pdf"))
    print("  building v2 corpus text signatures (%d files, cached after first run)..." % len(v2_files))
    corpus, index = [], []
    for k, f in enumerate(v2_files, 1):
        try:
            T = text_tokens(os.path.join(V2, f))
        except Exception as e:
            print("    skip %s (%s)" % (f, str(e)[:50]))
            continue
        corpus.extend(T)
        index.extend((f, p + 1) for p in range(len(T)))
        if k % 150 == 0:
            print("    %d/%d files, %d pages so far" % (k, len(v2_files), len(index)))
    poor = sum(1 for s in corpus if len(s) < MIN_TOKENS)
    print("  v2 corpus: %d pages from %d files (%d text-poor)" % (len(corpus), len(v2_files), poor))

    hunt = {}
    for v1n in HUNT_FILES:
        A = text_tokens(os.path.join(RAW, v1n), v1n)
        if sum(1 for s in A if len(s) >= MIN_TOKENS) == 0:
            print("    %s has NO PDF text layer - recovering OCR from Neon" % v1n)
            recovered = neon_page_text(v1n, len(A))
            if recovered:
                A = recovered
        M = jaccard_matrix(A, corpus)
        rows = []
        for i in range(len(A)):
            order = np.argsort(M[i])[::-1][:3]
            top = [{"file": index[j][0], "page": index[j][1], "score": round(float(M[i, j]), 4)}
                   for j in order]
            margin = top[0]["score"] - top[1]["score"] if len(top) > 1 else 0.0
            rows.append({"v1_page": i + 1, "top": top,
                         "margin": round(float(margin), 4),
                         "found": bool(top[0]["score"] >= thr)})
        found = sum(1 for r in rows if r["found"])
        hunt[v1n] = {"pages": len(A), "found": found, "not_found": len(A) - found, "rows": rows}
        print("  %-30s %2d pages -> %2d located, %2d NOT FOUND"
              % (v1n, len(A), found, len(A) - found))
        for r in rows:
            mark = " " if r["found"] else "?"
            print("      %sp%-3d %-28s p%-3d  score %.3f  margin %.3f"
                  % (mark, r["v1_page"], r["top"][0]["file"][:28], r["top"][0]["page"],
                     r["top"][0]["score"], r["margin"]))
    result["gates"]["C_hunt"] = hunt

    # ------------------------------------------------------------ contact sheets
    print()
    print("=" * 72)
    print("CONTACT SHEETS (for eyeball review)")
    made = []
    # weakest identity pages
    for v1n, v2n in IDENTITY_PAIRS[:2]:
        Mx, _ = hybrid_matrix(os.path.join(RAW, v1n), os.path.join(V2, v2n))
        diag = np.diag(Mx)
        worst = np.argsort(diag)[:3]
        rows = [("v1 p%d <-> v2 p%d   score %.3f" % (i + 1, i + 1, diag[i]),
                 [("v1", page_png(os.path.join(RAW, v1n), int(i))),
                  ("v2", page_png(os.path.join(V2, v2n), int(i)))]) for i in worst]
        out = os.path.join(SHEETS, "identity-weakest-%s.png" % v1n[:3])
        if contact_sheet(rows, out, "%s vs %s - 3 weakest identity pages" % (v1n, v2n)):
            made.append(out)
    # 013 deletions
    rows = [("DELETED from v2: v1 p%d" % p,
             [("v1", page_png(os.path.join(RAW, DELETION_PAIR[0]), p - 1))]) for p in deleted_pages]
    out = os.path.join(SHEETS, "013-deleted-pages.png")
    if contact_sheet(rows, out, "013: pages the alignment says were deleted in v2"):
        made.append(out)
    # hunt samples
    for v1n, h in hunt.items():
        rows = []
        for r in h["rows"][:4]:
            t = r["top"][0]
            rows.append(("v1 p%d -> %s p%d  score %.3f  %s"
                         % (r["v1_page"], t["file"], t["page"], t["score"],
                            "FOUND" if r["found"] else "NOT FOUND"),
                         [("v1", page_png(os.path.join(RAW, v1n), r["v1_page"] - 1)),
                          ("v2 best", page_png(os.path.join(V2, t["file"]), t["page"] - 1))]))
        out = os.path.join(SHEETS, "hunt-%s.png" % v1n[:3])
        if contact_sheet(rows, out, "%s - first 4 pages vs best v2 candidate" % v1n):
            made.append(out)
    for m in made:
        print("  %s" % m)
    result["contact_sheets"] = made

    json.dump(result, io.open(OUT_JSON, "w", encoding="utf-8"), indent=2)
    print()
    print("wrote %s" % OUT_JSON)

    if args.write_neon:
        write_neon(result)
    else:
        print("(read-only run - re-run with --write-neon to record into Neon)")


def write_neon(result):
    import psycopg
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        sys.exit("NEON_DATABASE_URL not set")
    conn = psycopg.connect(url)
    cur = conn.cursor()
    n = 0
    for v1n, info in result["gates"]["A_identity"].items():
        cur.execute("""update source_file set meta = meta ||
            jsonb_build_object('v2_alignment', %s::jsonb) where name = %s""",
            (json.dumps({"v2_file": info["v2_file"], "mapping": "identity",
                         "pages": info["pages"],
                         "identity_score_min": info["identity_score_min"]}), v1n))
        n += cur.rowcount
    b = result["gates"]["B_013_deletions"]
    cur.execute("""update source_file set meta = meta ||
        jsonb_build_object('v2_alignment', %s::jsonb) where name = %s""",
        (json.dumps({"v2_file": b["v2_file"], "mapping": "deletions",
                     "deleted_v1_pages": b["deleted_v1_pages"],
                     "map_v1_to_v2": b["map_v1_to_v2"]}), b["v1_file"]))
    n += cur.rowcount
    for v1n, h in result["gates"]["C_hunt"].items():
        cur.execute("""update source_file set meta = meta ||
            jsonb_build_object('v2_hunt', %s::jsonb) where name = %s""",
            (json.dumps({"found": h["found"], "not_found": h["not_found"],
                         "rows": h["rows"]}), v1n))
        n += cur.rowcount
    conn.commit()
    conn.close()
    print("Neon: %d source_file rows updated with alignment meta" % n)


if __name__ == "__main__":
    main()
