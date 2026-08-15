# /// script
# requires-python = ">=3.10"
# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24", "boto3>=1.34"]
# ///
"""Review + correct the low-confidence Mistral OCR before it is embedded.

    uv run ocr_review_app.py            # then open http://127.0.0.1:8778

NEON IS THE SOURCE OF TRUTH. Every fact this app shows comes from the database:
page text from ocr_reading.text, word confidences from ocr_reading.confidence,
the document->pdf mapping from output_file. Nothing is read from ocr-mistral/.
The single exception is the page IMAGE, because Neon stores no pixels -- those
are rendered on demand from recut/<name>.pdf when a page is actually viewed.

WHY WORD CONFIDENCE DRIVES EVERYTHING. The approved plan gated on MINIMUM word
confidence at 0.90. Measured, that flags 1,330 of 1,464 documents (91%): every
scan has one bad word (a smudge, a logo, a signature) and median doc-minimum is
0.576, while mean AVERAGE page confidence is 0.9799. The statistic that actually
separates good from bad is the PROPORTION of bad words:

    359,189 words, 4,913 below 0.60 = 1.4% overall
    667 documents (46%) have zero bad words
    median 0.27%   p90 4.12%   p99 21.84%
    gate at >2%  ->  245 documents   (>5% -> 117, >10% -> 48)

The same word scores drive the highlighting: each carries a start_index, a
character offset into the page markdown, so suspect words are marked at exact
positions instead of by string matching -- which would mis-mark the second
occurrence of a word that appears twice.

CORRECTIONS ARE ADDITIVE. A save never mutates the Mistral rows; it writes a
separate reading with method='human-corrected'. The Mistral text cost $8.88 and
cannot be reproduced exactly, so it stays intact and any correction can be
withdrawn by deleting one row. Re-saving a page deletes and re-inserts that
page's correction in one transaction. ocr_reading has a UNIQUE constraint on
(page_id, method), so the method carries the reviewer -- 'human-corrected:jeff'
-- and the database itself enforces one correction per page per reviewer.
"""
import hashlib, hmac, io, json, os, re, socketserver, sys, threading, webbrowser
from http.server import SimpleHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

import fitz
import psycopg

BASE = os.path.dirname(os.path.abspath(__file__))

# Page images are the ONE thing Neon cannot serve -- it stores no pixels.
#
# TWO SOURCES, in priority order:
#   1. R2  -- pre-rendered 300 DPI JPEGs, one per page_id (render_page_jpegs.py).
#             This is what a remote reviewer gets. The app never streams the
#             bytes itself; it signs a short-lived URL and redirects, so the
#             image travels R2 -> browser directly and the bucket stays private.
#   2. local recut/ -- rasterise on demand from the source PDFs. This is the
#             original single-machine behaviour and remains the fallback so the
#             app still runs on Alden's laptop with nothing configured.
RECUT = (os.environ.get("PAGE_SOURCE")
         or os.path.join(BASE, "recut")
         or "")
if not os.path.isdir(RECUT):
    alt = os.path.join(os.path.dirname(BASE), "recut")
    if os.path.isdir(alt):
        RECUT = alt

R2_BUCKET = os.environ.get("R2_BUCKET") or ""
R2_ENDPOINT = os.environ.get("R2_ENDPOINT") or ""      # https://<acct>.r2.cloudflarestorage.com
R2_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID") or ""
R2_SECRET = os.environ.get("R2_SECRET_ACCESS_KEY") or ""
# `or` not `get(key, default)`: Coolify passes every variable named in .env,
# including the optional ones left blank, so these arrive as EMPTY STRINGS
# rather than absent. get("R2_PREFIX", "pages") would then return "" and every
# object key would come out as "/1234.jpg" instead of "pages/1234.jpg" -- every
# image 404s, with nothing in the log to say why.
R2_PREFIX = (os.environ.get("R2_PREFIX") or "pages").strip() or "pages"
R2_SIGN_TTL = int(os.environ.get("R2_SIGN_TTL") or 3600)

# R2 is all-or-nothing. Treating a PARTIAL configuration as "R2 off" would send
# the app down the local-render path, and in the container there are no PDFs to
# render -- so every scan would come back 404 and the reviewer would see blank
# panes with nothing in the log to explain why. One unpasted secret should not
# look like a rendering bug.
_R2_VARS = {"R2_BUCKET": R2_BUCKET, "R2_ENDPOINT": R2_ENDPOINT,
            "R2_ACCESS_KEY_ID": R2_KEY_ID, "R2_SECRET_ACCESS_KEY": R2_SECRET}
_r2_missing = sorted(k for k, v in _R2_VARS.items() if not v)
if _r2_missing and len(_r2_missing) != len(_R2_VARS):
    sys.exit("REFUSING TO START: R2 is partly configured. Missing: %s\n"
             "Set all four, or none of them to render locally instead."
             % ", ".join(_r2_missing))
USE_R2 = not _r2_missing

HOST = os.environ.get("HOST") or "127.0.0.1"
PORT = int(os.environ.get("PORT") or 8778)

BAD_WORD = 0.60          # a word below this is "suspect"
GATE = 2.0               # % of suspect words that puts a document in the queue
MIN_WORDS = 20           # below this a percentage is noise, not a signal
MAX_REPEAT = 4           # consecutive identical non-blank rows = a loop
MISTRAL, HUMAN = "mistral-ocr-4-1", "human-corrected"
RENDER_DPI = 200         # local fallback render only; R2 JPEGs are 300 DPI

# WHO IS REVIEWING -- now PER REQUEST, not per process.
#
# This used to be one process-wide REVIEWER read from the environment, which is
# correct for one person on one laptop and wrong the moment two people share a
# deployment: whoever the server was started as would own every correction.
# The reviewer now comes from the signed session cookie, so Alden and Jeff can
# be in the app at the same time and each write under their own name.
#
# REVIEW_USERS is "name:password,name:password". Names become part of the
# ocr_reading method, so they are normalised to lowercase here and nowhere else.
def _parse_users(raw):
    users = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, pw = pair.partition(":")
        name = name.strip().lower()
        if name and pw:
            users[name] = pw
    return users


USERS = _parse_users(os.environ.get("REVIEW_USERS"))
LOOPBACK = HOST in ("127.0.0.1", "localhost", "::1")

# FAIL CLOSED.
#
# The obvious way to keep single-machine use frictionless is "no REVIEW_USERS
# means no login". That is a trapdoor: REVIEW_USERS arriving empty on the server
# -- unset in Coolify, a typo, a missing colon, a secret that failed to inject --
# would not break anything visibly. It would publish 1,464 probate documents,
# including bank statements and a creditor's claim against the estate, to anyone
# who found the URL. A misconfiguration must never widen access.
#
# So: no credentials is only tolerable when nothing outside this machine can
# reach the socket. Bound anywhere else, the process refuses to start. The
# container sets HOST=0.0.0.0, which makes a missing REVIEW_USERS a loud crash
# in the deploy log instead of a silent exposure.
if not USERS and not LOOPBACK:
    sys.exit("REFUSING TO START: REVIEW_USERS is empty and HOST=%s is not "
             "loopback.\nThat combination would serve every document with no "
             "authentication.\nSet REVIEW_USERS='name:password,name:password'."
             % HOST)

# Solo mode: loopback only, and only because the socket is unreachable remotely.
SOLO = ((os.environ.get("REVIEWER") or "alden").strip().lower()
        if not USERS else "")

# The signing key must be its own secret. Falling back to NEON_DATABASE_URL
# would make a cookie-signing key out of a database credential -- one leak, two
# compromises -- and a hardcoded default would let anyone mint a valid cookie.
if USERS and not os.environ.get("SESSION_SECRET"):
    sys.exit("REFUSING TO START: SESSION_SECRET is required when REVIEW_USERS "
             "is set.\nWithout it the login cookie cannot be signed safely.")
SECRET = (os.environ.get("SESSION_SECRET") or "loopback-solo-mode").encode()

# There must be SOME way to show a scan. Without R2 and without a readable
# recut/ folder, the app would start, log nothing unusual, serve the queue --
# and then hand back 404 for every image. A reviewer would be looking at empty
# panes wondering whether the documents were lost.
if not USE_R2 and not os.path.isdir(RECUT):
    sys.exit("REFUSING TO START: no page images available.\n"
             "Either set the four R2_* variables, or point PAGE_SOURCE at a "
             "folder of recut PDFs.\nPAGE_SOURCE currently resolves to: %r"
             % RECUT)


# ocr_reading carries a UNIQUE constraint on (page_id, method) --
# ocr_reading_page_id_method_key. A single shared 'human-corrected' method
# therefore allows exactly ONE correction per page for everyone, and the second
# reviewer's save dies on a constraint violation. Putting the reviewer in the
# method turns that constraint into exactly the rule we want: one correction
# per page PER REVIEWER, enforced by the database rather than by convention.
def method_for(reviewer):
    return "%s:%s" % (HUMAN, reviewer)


URL = os.environ.get("NEON_DATABASE_URL")
if not URL:
    sys.exit("NEON_DATABASE_URL not set")


def db():
    return psycopg.connect(URL)


def score(words):
    return sum(1 for w in words if (w.get("confidence") or 1.0) < BAD_WORD), len(words)


def page_words(conf):
    return (conf or {}).get("word_confidence_scores") or []


TR = re.compile(r"<tr>(.*?)</tr>", re.S)


CELL = re.compile(r"<[^>]*>|&nbsp;|\s")


def repetition(blocks):
    """(longest consecutive run of one row, total duplicated rows).

    A SECOND, INDEPENDENT FAILURE MODE. Word confidence asks "how sure is the
    model about this word"; a repetition loop emits words it is entirely sure
    of, over and over, when it gets stuck on a low-contrast region. The worst
    case here repeats one row 35 times and scores 0.0% suspect -- invisible to
    confidence, and it would write invented line items into a probate account.

    TWO REFINEMENTS, both from real false positives:

    1. BLANK ROWS ARE IGNORED. An invoice with printed ruled lines yields a
       run of identical empty <tr> rows. Measured: 17 of 54 flagged documents
       had NO repeated content at all -- only blank rows from the paper form.

    2. CONSECUTIVE RUNS, not total occurrences. A receipt can legitimately
       list the same item several times, scattered through the order; a
       generation loop emits the same row back to back. Measured across the
       corpus, run>=4 flags 10 documents and still catches every confirmed
       loop (runs of 20, 12, 10, 10), where counting occurrences anywhere
       flagged 54.
    """
    worst = dups = 0
    for t in ((blocks or {}).get("tables") or []):
        rows = [r for r in TR.findall(t.get("content") or "") if CELL.sub("", r)]
        if not rows:
            continue
        run = 1
        prev = None
        counts = {}
        for r in rows:
            counts[r] = counts.get(r, 0) + 1
            run = run + 1 if r == prev else 1
            prev = r
            worst = max(worst, run)
        dups += sum(n - 1 for n in counts.values() if n > 1)
    return worst, dups


def table_words(blocks):
    """Tables are scored SEPARATELY by the API and are invisible in the page
    markdown, which carries only a [tbl-N.html] placeholder. 782 of 1,762 pages
    have one; 79,542 words -- 18% of the corpus -- live in them."""
    out = []
    for t in ((blocks or {}).get("tables") or []):
        out += (t.get("word_confidence_scores") or [])
    return out


def bad_rate(conf):
    return score(page_words(conf))


def build_queue(cur, reviewer):
    """EVERY document, flagged first. One query, scored in Python because the
    scoring rule lives in one place and must match the numbers above.

    This used to return only the gated documents. The list now carries the
    whole corpus so the review UI can filter -- by confidence tier, verdict,
    reviewer, loops, notes -- instead of hiding everything the gate did not
    trip. The gate still decides FLAGGED and the default sort; it no longer
    decides visibility."""
    cur.execute("""
        select d.id, d.key, o.name, r.confidence, r.blocks,
               d.meta->'ocr_review' as review,
               d.meta->'tags' as tags
        from ocr_reading r
        join document d on d.id = (r.meta->>'document_id')::bigint
        left join output_file o on o.document_id = d.id
                        and o.build_version = 'recut-v2'
        where r.method = %s""", (MISTRAL,))
    rows = cur.fetchall()

    # Which documents carry a reviewer note, and how many pages have been
    # edited. Human rows carry only page_id (their meta is {source, by, note}),
    # so the document comes from joining back through the Mistral row.
    cur.execute("""
        select (m.meta->>'document_id')::bigint as did,
               count(*) filter (where coalesce(h.meta->>'note','') <> '') as noted,
               count(*) as edited
        from ocr_reading h
        join ocr_reading m on m.page_id = h.page_id and m.method = %s
        where h.method like %s
        group by 1""", (MISTRAL, HUMAN + ":%"))
    activity = {d: (n, e) for d, n, e in cur.fetchall()}

    docs = {}
    for did, key, name, conf, blocks, review, tags in rows:
        # Every reviewer's verdict travels with the document, so each of you can
        # see what the other decided rather than only your own progress.
        # isinstance guard: two older shapes exist in this table -- a bare
        # {"approved": true} and a flat {"verdict": "..."} -- and a bare value
        # under a reviewer key would otherwise blow up the whole queue.
        rv = review if isinstance(review, dict) else {}
        peers = {w: v.get("verdict") for w, v in rv.items()
                 if w != reviewer and isinstance(v, dict) and v.get("verdict")}
        me = rv.get(reviewer)
        mine = me.get("verdict") if isinstance(me, dict) else None
        d = docs.setdefault(did, {"id": did, "key": key, "pdf": name,
                                  "pages": 0, "bad": 0, "words": 0,
                                  "tbad": 0, "twords": 0,
                                  "maxRep": 0, "dupRows": 0,
                                  "verdict": mine, "peers": peers,
                                  "tags": tags if isinstance(tags, list) else [],
                                  "noted": activity.get(did, (0, 0))[0],
                                  "edited": activity.get(did, (0, 0))[1],
                                  "done": mine in ("approved", "hold")})
        b, t = score(page_words(conf))
        tb, tt = score(table_words(blocks))
        rep, dup = repetition(blocks)
        d["pages"] += 1
        d["bad"] += b
        d["words"] += t
        d["tbad"] += tb
        d["twords"] += tt
        d["maxRep"] = max(d["maxRep"], rep)
        d["dupRows"] += dup
    # A RATE ALONE MISRANKS THE QUEUE. A page where Mistral returned one scored
    # word, and that word is bad, is 100% bad and sorts above every genuinely
    # broken document -- while having a single character to look at. Measured:
    # 6 of the 245 gated documents have under MIN_WORDS scored words.
    # They are not dropped (a silently truncated queue reads as "all reviewed");
    # they are marked thin and sorted last, after the real work.
    # UNION GATE, not either rate alone. Table words are 18% of the corpus and
    # OCR cleaner than prose (0.48% vs 1.37% suspect), so folding them into one
    # ratio DILUTES a document whose prose is bad but whose big table is clean --
    # measured, that silently drops 35 documents out of the queue while their
    # prose errors remain. So: flag if EITHER the prose rate OR the combined
    # rate trips the gate. Nothing escapes review by being averaged away.
    out = []
    for d in docs.values():
        allw, allb = d["words"] + d["twords"], d["bad"] + d["tbad"]
        d["rate"] = round(100.0 * d["bad"] / d["words"], 2) if d["words"] else 0.0
        d["allRate"] = round(100.0 * allb / allw, 2) if allw else 0.0
        d["thin"] = allw < MIN_WORDS
        d["repeats"] = d["maxRep"] >= MAX_REPEAT
        d["flagged"] = d["rate"] > GATE or d["allRate"] > GATE or d["repeats"]
        # Confidence tier, for filtering. "low" is exactly the gate -- one
        # definition of bad, not two. medium/high split the rest at 0.5% so
        # "high" genuinely means near-zero suspect words.
        worst = max(d["rate"], d["allRate"])
        d["conf"] = "low" if d["flagged"] else ("medium" if worst > 0.5 else "high")
        # Effective document state across BOTH reviewers, for the pipeline
        # counts. hold trumps approved -- a document one person approved and
        # another held is NOT safe to embed. Then: any final approval counts,
        # then any submission, else unreviewed.
        verdicts = set(v for v in [d["verdict"], *d["peers"].values()] if v)
        d["state"] = ("hold" if "hold" in verdicts
                      else "approved" if "approved" in verdicts
                      else "submitted" if "submitted" in verdicts
                      else "unreviewed")
        out.append(d)
    # Flagged first (repetition before rate -- a fabricated table is worse than
    # a misread word, and 15 of these are invisible to the confidence gate),
    # then the clean tail sorted by rate so "worst of the good" is on top.
    out.sort(key=lambda x: (not x["flagged"], x["thin"], not x["repeats"],
                            -x["maxRep"], -max(x["rate"], x["allRate"])))
    return out


def load_doc(cur, did, reviewer):
    """All pages of one document: text, suspect-word spans, prior reading,
    and any correction already saved."""
    cur.execute("""
        select r.page_id, r.meta->>'doc_page', r.text, r.confidence, r.blocks
        from ocr_reading r
        where r.method = %s and (r.meta->>'document_id')::bigint = %s
        order by (r.meta->>'doc_page')::int""", (MISTRAL, did))
    rows = cur.fetchall()
    pids = [r[0] for r in rows]
    corrected, corr_tbl, notes, others = {}, {}, {}, {}
    if pids:
        cur.execute("""select page_id, method, text, blocks, meta, ts
                       from ocr_reading
                       where page_id = any(%s) and method like %s
                       order by ts""", (pids, HUMAN + ':%'))
        for pid, method, text, blocks, meta, ts in cur.fetchall():
            who = (method.split(":", 1)[1] if ":" in method
                   else ((meta or {}).get("by") or "unknown")).lower()
            if who == reviewer:
                corrected[pid] = text
                corr_tbl[pid] = (blocks or {}).get("tables")
                notes[pid] = (meta or {}).get("note") or ""
            else:
                others.setdefault(pid, []).append({
                    "by": who, "text": text or "",
                    "note": (meta or {}).get("note") or "",
                    "when": ts.strftime("%Y-%m-%d %H:%M") if ts else ""})

    cur.execute("select name from output_file where document_id = %s "
                "and build_version = 'recut-v2' limit 1", (did,))
    got = cur.fetchone()

    pages = []
    for pid, doc_page, text, conf, blocks in rows:
        words = page_words(conf)
        spans = [{"s": w["start_index"],
                  "e": w["start_index"] + len(w.get("text") or ""),
                  "c": round(w.get("confidence") or 1.0, 3)}
                 for w in words if (w.get("confidence") or 1.0) < BAD_WORD
                 and w.get("start_index") is not None]
        b, t = score(words)

        # Tables: the markdown shows only [tbl-N.html], so hand the browser the
        # real HTML plus the list of suspect strings inside it. Table words are
        # indexed into the table's own content, not the page markdown, so they
        # are matched by value in the rendered cells rather than by offset.
        orig_tbl = ((blocks or {}).get("tables") or [])
        saved_tbl = corr_tbl.get(pid) or []
        by_id = {x.get("id"): x.get("content") for x in saved_tbl}
        tables = []
        for tb in orig_tbl:
            tw = tb.get("word_confidence_scores") or []
            tables.append({
                "id": tb.get("id"),
                "html": tb.get("content") or "",
                "saved": by_id.get(tb.get("id")),
                "suspect": sorted({(w.get("text") or "").strip()
                                   for w in tw
                                   if (w.get("confidence") or 1.0) < BAD_WORD
                                   and (w.get("text") or "").strip()}),
                "bad": score(tw)[0], "words": len(tw)})

        pages.append({"pageId": pid, "docPage": int(doc_page or 1),
                      "text": text or "", "spans": spans,
                      "corrected": corrected.get(pid),
                      "note": notes.get(pid, ""),
                      "others": others.get(pid, []),
                      "tables": tables,
                      "bad": b, "words": t})
    return {"id": did, "pdf": got[0] if got else None, "pages": pages}


def save_page(cur, page_id, text, tables, note, reviewer):
    """Replace this page's correction -- text, tables and note in one row.
    DELETE+INSERT because ocr_reading has no unique constraint; an INSERT
    alone would stack another row on every save.

    The NOTE is why a page was wrong, in your words, and it is the part a
    downstream fix depends on: "this table is a repetition loop, 13 invented
    rows" needs a different remedy from "the merchant name is misread". A
    note alone is enough to write the row -- flagging a problem you have not
    fixed yet must not be lost.
    """
    my_method = method_for(reviewer)
    cur.execute("delete from ocr_reading where page_id = %s and method = %s",
                (page_id, my_method))
    if (text or "").strip() or tables or (note or "").strip():
        cur.execute("""insert into ocr_reading
                       (page_id, method, text, blocks, meta)
                       values (%s, %s, %s, %s::jsonb, %s::jsonb)""",
                    (page_id, my_method, text or "",
                     json.dumps({"tables": tables or []}, ensure_ascii=False),
                     json.dumps({"source": "ocr_review_app",
                                 "by": reviewer,
                                 "note": (note or "").strip()},
                                ensure_ascii=False)))


def verdict(cur, did, value, reviewer):
    """Stamp the review verdict. document.meta only -- document.state is
    load-bearing for the pipeline and is not touched.

    FOUR STATES -- the round trip needs a middle stop:
      submitted -- my edits are done and handed off. Pending application:
                   someone (or the pipeline) applies the corrections and
                   re-uploads the artifact tagged v2/v3. Not yet safe to embed.
      approved  -- FINAL. Reviewed, correct, marked for the next step
                   (embed -> PDF/A -> QC -> Papra ingestion).
      hold      -- reviewed and NOT safe to embed. Unreadable, fabricated
                   table, needs a second OCR pass.
      (absent)  -- not yet reviewed
    The embed step must select on approved, never on 'has been opened' and
    never on submitted -- submitted text has not been checked back yet.
    """
    cur.execute("""update document set meta = coalesce(meta,'{}'::jsonb) ||
                   jsonb_build_object('ocr_review',
                     coalesce(meta->'ocr_review','{}'::jsonb) -
                       'verdict' - 'approved' ||
                     jsonb_build_object(%s::text,
                       jsonb_build_object('verdict', %s::text)))
                   where id = %s""", (reviewer, value, did))


def set_tags(cur, did, tags):
    """Replace the document's tags. Tags are SHARED, not per reviewer -- they
    describe the document (v2, needs-reocr, illegible), not an opinion about
    it, so last write wins and both reviewers see the same set. Deduplicated
    and order-preserved so the UI renders what was sent."""
    seen, clean = set(), []
    for t in tags:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            clean.append(t)
    cur.execute("""update document set meta = coalesce(meta,'{}'::jsonb) ||
                   jsonb_build_object('tags', %s::jsonb)
                   where id = %s""", (json.dumps(clean), did))


def page_png(pdf_name, doc_page):
    """Local fallback: rasterise straight from the source PDF. Used when R2 is
    not configured, i.e. running on the machine that holds recut/."""
    path = os.path.join(RECUT, pdf_name)
    if not pdf_name or ".." in pdf_name or not os.path.isfile(path):
        return None
    doc = fitz.open(path)
    try:
        idx = max(0, min(doc.page_count - 1, doc_page - 1))
        pix = doc[idx].get_pixmap(dpi=RENDER_DPI)
        return pix.tobytes("png")
    finally:
        doc.close()


_s3 = None


def r2_url(page_id):
    """A short-lived signed URL for this page's 300 DPI JPEG.

    We redirect the browser here rather than streaming the bytes through this
    process. A 47-page document is ~50 MB of JPEG; proxying that through a small
    Coolify container would make the app the bottleneck for no benefit. Signing
    keeps the bucket private -- these are probate documents and must not be
    world-readable."""
    global _s3
    if not USE_R2:
        return None
    if _s3 is None:
        import boto3                                   # noqa: PLC0415
        from botocore.config import Config             # noqa: PLC0415
        _s3 = boto3.client("s3", endpoint_url=R2_ENDPOINT,
                           aws_access_key_id=R2_KEY_ID,
                           aws_secret_access_key=R2_SECRET,
                           region_name="auto",
                           config=Config(signature_version="s3v4"))
    key = "%s/%d.jpg" % (R2_PREFIX.strip("/"), int(page_id))
    return _s3.generate_presigned_url("get_object",
                                      Params={"Bucket": R2_BUCKET, "Key": key},
                                      ExpiresIn=R2_SIGN_TTL)


# ---------------------------------------------------------------- sessions
def _sign(value):
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()[:32]


def make_cookie(reviewer):
    """name|signature. Stateless, so restarting the container does not log
    everyone out, and there is no session store to keep."""
    return "%s|%s" % (reviewer, _sign(reviewer))


def cookie_reviewer(header):
    """Whoever this request is, or None. Never trusts the name without the
    signature -- otherwise anyone could write corrections as anyone."""
    # SOLO is only ever set when the listener is loopback-bound (enforced at
    # startup). There is deliberately no other path that returns a reviewer
    # without a verified signature.
    if SOLO:
        return SOLO
    for part in (header or "").split(";"):
        part = part.strip()
        if not part.startswith("rev="):
            continue
        raw = unquote(part[4:])
        name, _, sig = raw.partition("|")
        name = name.strip().lower()
        if name in USERS and sig and hmac.compare_digest(sig, _sign(name)):
            return name
    return None


LOGIN_HTML = """<!doctype html><meta charset="utf-8"><title>OCR review</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{font:16px/1.5 system-ui,sans-serif;background:#111;color:#eee;
  display:grid;place-items:center;height:100vh;margin:0}
form{background:#1c1c1c;padding:28px 30px;border-radius:10px;width:min(92vw,330px);
  border:1px solid #333}
h1{font-size:17px;margin:0 0 18px}
label{display:block;font-size:12px;color:#999;margin:12px 0 4px}
input{width:100%;padding:9px 10px;font-size:15px;border-radius:6px;
  border:1px solid #444;background:#111;color:#eee;box-sizing:border-box}
button{width:100%;margin-top:18px;padding:10px;font-size:15px;border:0;
  border-radius:6px;background:#2d6cdf;color:#fff;cursor:pointer}
.err{color:#ff8080;font-size:13px;margin-top:12px}
</style>
<form method=post action=/login>
<h1>OCR review</h1>
<label>Name</label><input name=user autofocus autocapitalize=off>
<label>Password</label><input name=pw type=password>
<button>Sign in</button>
<!--ERR-->
</form>"""


def login_page(error=""):
    """Token substitution, NOT %-formatting: the stylesheet contains
    'width:100%' and a bare % is an invalid format spec, which turned the
    login page into a 500 for anonymous visitors -- i.e. for everyone."""
    return LOGIN_HTML.replace(
        "<!--ERR-->",
        '<div class="err">%s</div>' % error if error else "")


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def whoami(self):
        return cookie_reviewer(self.headers.get("Cookie"))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, to, cookie=None):
        self.send_response(302)
        self.send_header("Location", to)
        if cookie:
            # Secure off-loopback: Coolify terminates TLS in front, so the
            # cookie must never be allowed onto a plaintext hop. Left off for
            # local http so the laptop case still works.
            self.send_header("Set-Cookie",
                             "rev=%s; Path=/; HttpOnly; SameSite=Lax; "
                             "Max-Age=2592000%s"
                             % (cookie, "" if LOOPBACK else "; Secure"))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        try:
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/healthz":                    # Coolify health check
                return self._send(200, '{"ok":true}')
            if path == "/login":
                return self._send(200, login_page(), "text/html; charset=utf-8")

            who = self.whoami()
            if not who:
                if path in ("/", "/index.html"):
                    return self._send(200, login_page(),
                                      "text/html; charset=utf-8")
                return self._send(401, '{"error":"login required"}')

            if path in ("/", "/index.html"):
                return self._send(200, HTML, "text/html; charset=utf-8")
            if path == "/whoami":
                return self._send(200, json.dumps({"reviewer": who}))
            if path == "/logout":
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", "rev=; Path=/; Max-Age=0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None

            # Page image. R2 when configured -- redirect so the bytes go
            # straight from R2 to the browser; local render otherwise.
            if path in ("/page.png", "/page.img"):
                pid = q.get("id", [""])[0]
                if USE_R2 and pid:
                    return self._redirect(r2_url(int(pid)))
                png = page_png(q.get("pdf", [""])[0],
                               int(q.get("p", ["1"])[0]))
                if not png:
                    return self._send(404, b"", "image/png")
                return self._send(200, png, "image/png")

            with db() as c, c.cursor() as cur:
                if path == "/queue":
                    return self._send(200, json.dumps(build_queue(cur, who)))
                if path == "/doc":
                    return self._send(200, json.dumps(
                        load_doc(cur, int(q.get("id", ["0"])[0]), who)))
        except Exception as e:                                    # noqa: BLE001
            return self._send(500, json.dumps({"error": "%s: %s"
                                               % (type(e).__name__, e)}))
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) or b""
        try:
            if path == "/login":
                form = parse_qs(body.decode("utf-8", "replace"))
                name = (form.get("user", [""])[0] or "").strip().lower()
                pw = form.get("pw", [""])[0] or ""
                if name in USERS and hmac.compare_digest(USERS[name], pw):
                    return self._redirect("/", make_cookie(name))
                return self._send(401, login_page("Wrong name or password."),
                                  "text/html; charset=utf-8")

            who = self.whoami()
            if not who:
                return self._send(401, '{"error":"login required"}')

            p = json.loads(body or b"{}")
            with db() as c, c.cursor() as cur:
                if path == "/save":
                    save_page(cur, int(p["pageId"]), p.get("text", ""),
                              p.get("tables") or [], p.get("note", ""), who)
                elif path == "/verdict":
                    v = p.get("verdict")
                    if v not in ("submitted", "approved", "hold", None):
                        return self._send(400, '{"error":"bad verdict"}')
                    verdict(cur, int(p["id"]), v, who)
                elif path == "/tags":
                    tags = p.get("tags")
                    if (not isinstance(tags, list)
                            or any(not isinstance(t, str) or len(t) > 40
                                   for t in tags)):
                        return self._send(400, '{"error":"bad tags"}')
                    set_tags(cur, int(p["id"]), tags)
                else:
                    return self._send(404, '{"error":"not found"}')
                c.commit()
            return self._send(200, '{"ok":true}')
        except Exception as e:                                    # noqa: BLE001
            return self._send(500, json.dumps({"error": "%s: %s"
                                               % (type(e).__name__, e)}))


HTML = r"""<!doctype html><meta charset="utf-8"><title>OCR review</title>
<style>
/* Linear design tokens -- Design/DESIGN.md. Dark is the native medium: near-
   black canvas, structure from semi-transparent white borders, one accent. */
:root{
  --bg:#08090a;--panel:#0f1011;--lvl3:#191a1b;--hover:#28282c;
  --fg:#f7f8f8;--fg2:#d0d6e0;--dim:#8a8f98;--dim2:#62666d;
  --line:rgba(255,255,255,.05);--line2:rgba(255,255,255,.08);
  --brand:#5e6ad2;--accent:#7170ff;--accent-h:#828fff;
  --ok:#27a644;--ok2:#10b981;--warn:#fbbf24;--bad:#ff6b6b;
  --add:#14532d;--del:#5b1d1d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);height:100vh;display:flex;
font:13px/1.5 "Inter Variable",Inter,"SF Pro Display",-apple-system,system-ui,
"Segoe UI",Roboto,sans-serif;font-feature-settings:"cv01","ss03";
font-weight:400}
#side{width:300px;flex:0 0 300px;border-right:1px solid var(--line2);
display:flex;flex-direction:column;min-height:0;background:var(--panel)}
#counts{padding:10px 12px;border-bottom:1px solid var(--line);font-size:12px;
font-weight:510;display:flex;gap:10px;flex-wrap:wrap}
#counts b{font-weight:590}
#counts .cr{color:var(--bad)}#counts .cs{color:var(--accent-h)}
#counts .cf{color:var(--ok2)}#counts .ch{color:var(--warn)}
#filters{padding:7px 10px;border-bottom:1px solid var(--line);display:flex;
gap:4px;flex-wrap:wrap}
.fc{font-size:11px;font-weight:510;padding:2px 8px;border-radius:9999px;
border:1px solid var(--line2);background:rgba(255,255,255,.02);
color:var(--dim);cursor:pointer;user-select:none}
.fc:hover{background:var(--hover);color:var(--fg2)}
.fc.on{background:var(--brand);border-color:var(--brand);color:#fff}
#search{margin:7px 10px;padding:5px 9px;background:rgba(255,255,255,.02);
border:1px solid var(--line2);border-radius:6px;color:var(--fg);
font:12px/1.4 inherit;outline:0}
#search:focus{border-color:var(--accent)}
#search::placeholder{color:var(--dim2)}
#list{flex:1;overflow:auto;min-height:0}
.q{padding:7px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.q:hover{background:var(--hover)}
.q.on{background:var(--lvl3);border-left:3px solid var(--accent)}
.q .k{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
font-weight:510;color:var(--fg2)}
.q .m{color:var(--dim);font-size:11px}
.q.done .k{color:var(--ok2)}
.q.held .k{color:var(--warn)}
.q.subm .k{color:var(--accent-h)}
.pk{color:var(--accent-h);font-size:10px}
.tag{display:inline-block;font-size:10px;font-weight:510;padding:0 6px;
border-radius:9999px;background:rgba(94,106,210,.18);color:var(--accent-h);
border:1px solid rgba(113,112,255,.3);margin-left:4px}
#notewrap{flex:0 0 auto;border-top:1px solid var(--line2);background:var(--panel);
display:flex;flex-direction:column;max-height:44vh}
.nh{padding:5px 10px;font-size:11px;font-weight:510;color:var(--dim);
background:var(--lvl3);border-bottom:1px solid var(--line)}
#note{height:84px;min-height:84px;padding:8px 10px;flex:0 0 auto;
font:12px/1.5 inherit;background:var(--panel);color:var(--fg);border:0;
outline:0;resize:vertical}
#note::placeholder{color:var(--dim2)}
#others{flex:0 1 auto;overflow:auto;border-top:1px solid var(--line)}
.ob{padding:6px 10px;border-bottom:1px solid var(--line)}
.ob h4{margin:0 0 3px;font-size:11px;color:var(--accent-h);font-weight:590}
.ob .on{white-space:pre-wrap;font-size:12px;color:var(--fg2)}
.ob .oc{color:var(--dim);font-size:11px;margin-top:3px}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#bar{padding:8px 12px;border-bottom:1px solid var(--line2);display:flex;gap:8px;
align-items:center;background:var(--panel);flex-wrap:wrap}
button{background:rgba(255,255,255,.02);color:#e2e4e7;font-weight:510;
border:1px solid rgb(36,40,44);border-radius:6px;padding:5px 11px;cursor:pointer;
font-family:inherit;font-feature-settings:inherit}
button:hover{background:var(--hover)}
button.p{background:var(--brand);border-color:var(--brand);color:#fff}
button.p:hover{background:var(--accent)}
button.f{background:rgba(16,185,129,.14);border-color:rgba(16,185,129,.4);
color:#6ee7b7}
button.f:hover{background:rgba(16,185,129,.25)}
button.h{background:rgba(251,191,36,.1);border-color:rgba(251,191,36,.35);
color:var(--warn)}
button.h:hover{background:rgba(251,191,36,.2)}
#tags{display:flex;gap:4px;align-items:center;flex-wrap:wrap}
#tags .tag{cursor:pointer;margin-left:0}
#tags .tag:hover{background:rgba(255,107,107,.18);color:#ffd7d7;
border-color:rgba(255,107,107,.4)}
#tagsel{background:rgba(255,255,255,.02);color:var(--dim);font:11px/1.4 inherit;
border:1px solid var(--line2);border-radius:6px;padding:3px 6px;outline:0}
#panes{flex:1;display:flex;min-height:0}
.pane{flex:1;display:flex;flex-direction:column;border-right:1px solid var(--line2);
min-width:0}.pane:last-child{border-right:0}
.ph{padding:5px 10px;font-size:11px;font-weight:510;color:var(--dim);
background:var(--panel);border-bottom:1px solid var(--line);display:flex;
justify-content:space-between}
.pb{flex:1;overflow:auto;padding:10px}
#imgwrap{position:relative;overflow:hidden;height:100%;background:#010102;
cursor:grab}
#imgwrap.drag{cursor:grabbing}
#img{display:block;background:#fff;transform-origin:0 0;
image-rendering:-webkit-optimize-contrast}
#zbar{position:absolute;right:8px;top:8px;z-index:5;display:flex;gap:4px;
background:rgba(15,16,17,.85);border:1px solid var(--line2);border-radius:6px;
padding:3px}
#zbar button{padding:2px 8px;font-size:12px;line-height:1.4}
#zl{align-self:center;color:var(--dim);font-size:11px;padding:0 4px;min-width:38px;
text-align:center}
pre,textarea.mono,#ed{margin:0;font:12px/1.55 "Berkeley Mono",ui-monospace,
"SF Mono",Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
#ed{width:100%;height:auto;min-height:24vh;background:transparent;
color:var(--fg);border:0;outline:0;resize:vertical}
.tw{margin:8px 0;border:1px solid var(--line2);border-radius:6px;
padding:6px;background:var(--lvl3)}
.tl{font-size:10px;color:var(--dim);margin-bottom:4px}
.th{margin:14px 0 4px;font-size:11px;color:var(--dim);border-top:1px solid var(--line);
padding-top:8px;display:flex;justify-content:space-between}
table{border-collapse:collapse;width:100%;
font:11px/1.4 "Berkeley Mono",ui-monospace,Consolas,monospace}
td,th{border:1px solid var(--line2);padding:3px 5px;vertical-align:top}
td:focus{outline:2px solid var(--accent);background:var(--lvl3)}
mark{background:rgba(255,107,107,.28);color:#ffd7d7;border-bottom:1px solid var(--bad);
border-radius:2px}
ins{background:var(--add);color:#bbf7d0;text-decoration:none}
del{background:var(--del);color:#fecaca}
#st{margin-left:auto;color:var(--dim)}
.hide{display:none !important}
button.t{padding:5px 9px;font-size:11px}
</style>
<div id=side>
  <div id=counts>loading…</div>
  <div id=filters></div>
  <input id=search placeholder="search documents…" spellcheck=false>
  <div id=list>loading…</div>
  <div id=notewrap>
    <div class=nh id=noteh>note — what is wrong with this page?</div>
    <textarea id=note spellcheck=true placeholder="e.g. table is a repetition loop, ~13 invented rows · merchant name misread · handwriting unreadable, do not embed a guess"></textarea>
    <div id=others></div>
  </div>
</div>
<div id=main>
  <div id=bar>
    <button class=t onclick="tog('side')" title="[ key">☰ list</button>
    <button class=t onclick="tog('p-diff')" title="] key">diff</button>
    <button onclick="pg(-1)">◀ page</button><b id=pn>—</b><button onclick="pg(1)">page ▶</button>
    <button onclick="save()">Save page</button>
    <button class=p onclick="setVerdict('submitted')" id=bok
      title="Saves this page first, then marks the document SUBMITTED — edits done, pending application and re-upload (v2/v3)">Submit ▶</button>
    <button class=f onclick="setVerdict('approved')" id=bfin
      title="Saves first, then marks the document FINAL — correct, and marked for the next step: embed, PDF/A, QC, Papra">✔ Approve Final</button>
    <button class=h onclick="setVerdict('hold')"
      title="Saves this page first, then marks the document DO NOT EMBED">⏸ Hold</button>
    <span id=tags></span>
    <select id=tagsel onchange="addTag(this.value)">
      <option value="">+ tag</option>
      <option>v2</option><option>v3</option>
      <option>needs-reocr</option><option>illegible</option>
      <option>reading-order</option><option>repetition</option>
      <option>handwriting</option>
      <option value="__custom">custom…</option>
    </select>
    <span id=st></span>
  </div>
  <div id=panes>
    <div class=pane><div class=ph><span>scan</span><span id=fn></span></div>
      <div class=pb style=padding:0>
        <div id=imgwrap>
          <div id=zbar>
            <button onclick="zoom(-1)">−</button><span id=zl>fit</span>
            <button onclick="zoom(1)">+</button><button onclick="zfit()">fit</button>
          </div>
          <img id=img>
        </div>
      </div></div>
    <div class=pane><div class=ph><span>Mistral — suspect words marked</span>
      <span id=bc></span></div><div class=pb><pre id=orig></pre></div></div>
    <div class=pane><div class=ph><span>your correction (editable)</span>
      <span id=tc></span></div>
      <div class=pb><textarea id=ed spellcheck=false></textarea>
        <div id=tbl></div></div>
    </div>
    <div class=pane id=p-diff><div class=ph><span>diff</span>
      <select id=dm onchange=render()>
        <option value=edit>your edits vs Mistral</option>
        <option value=other>other reviewer vs Mistral</option></select></div>
      <div class=pb><pre id=df></pre></div></div>
  </div>
</div>
<script>
let Q=[],D=null,i=0,dirty=false;
const $=x=>document.getElementById(x);
// The Submit button relabels itself when there are pending edits, so it is
// visible on the button -- not just inferable from the code -- that submitting
// saves your work rather than discarding it.
function mark(){dirty=true;$('st').textContent='unsaved';
  $('bok').textContent='Save + Submit ▶';}
function unmark(){dirty=false;$('bok').textContent='Submit ▶';}
const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

// ---- filters + counts ------------------------------------------------------
// The list carries the WHOLE corpus; the chips decide what is visible. The
// filter is a predicate over the document row, so adding one is one line.
const FILTERS={
  all:      [ 'All',        d=>true ],
  flagged:  [ 'Flagged',    d=>d.flagged ],
  loops:    [ '⟳ Loops', d=>d.repeats ],
  low:      [ 'Low',        d=>d.conf==='low' ],
  medium:   [ 'Med',        d=>d.conf==='medium' ],
  high:     [ 'High',       d=>d.conf==='high' ],
  unrev:    [ 'Unreviewed', d=>d.state==='unreviewed' ],
  submitted:[ 'Submitted',  d=>d.state==='submitted' ],
  final:    [ 'Final ✓', d=>d.state==='approved' ],
  held:     [ 'Held',       d=>d.state==='hold' ],
  mine:     [ 'Mine',       d=>!!d.verdict ],
  noted:    [ '✎ Noted', d=>d.noted>0 ],
};
let FILTER='flagged', SEARCH='';
function visible(){const f=FILTERS[FILTER][1], s=SEARCH.toLowerCase();
  return Q.map((d,n)=>[d,n]).filter(([d])=>f(d)&&
    (!s||(d.key||'').toLowerCase().includes(s)||
     (d.tags||[]).some(t=>t.toLowerCase().includes(s))));}
function drawFilters(){$('filters').innerHTML=Object.entries(FILTERS).map(
  ([k,[label]])=>{const n=Q.filter(FILTERS[k][1]).length;
    return `<span class="fc ${k===FILTER?'on':''}" onclick="setFilter('${k}')"
      title="${n} document(s)">${label}</span>`;}).join('');}
function setFilter(k){FILTER=k;drawFilters();drawList();}
$('search').addEventListener('input',e=>{SEARCH=e.target.value;drawList();});

// The pipeline readout. "to review" is the gate's queue; submitted is waiting
// on the apply+reupload round trip; final is marked for embed -> PDF/A -> QC
// -> Papra. These are EFFECTIVE states across both reviewers (hold trumps).
function drawCounts(){
  const c={r:0,s:0,f:0,h:0};
  for(const d of Q){
    if(d.state==='approved')c.f++;
    else if(d.state==='hold')c.h++;
    else if(d.state==='submitted')c.s++;
    else if(d.flagged)c.r++;}
  $('counts').innerHTML=
    `<span class=cr><b>${c.r}</b> to review</span>`+
    `<span class=cs><b>${c.s}</b> submitted</span>`+
    `<span class=cf><b>${c.f}</b> final</span>`+
    `<span class=ch><b>${c.h}</b> held</span>`;}

// Recompute a document's effective state after a verdict changes, using the
// same rule the server applies: hold trumps approved trumps submitted.
function effState(d){
  const vs=new Set([d.verdict,...Object.values(d.peers||{})].filter(Boolean));
  return vs.has('hold')?'hold':vs.has('approved')?'approved'
        :vs.has('submitted')?'submitted':'unreviewed';}

async function boot(){Q=await(await fetch('/queue')).json();
  drawCounts();drawFilters();drawList();
  const first=Q.findIndex(d=>d.flagged&&d.state==='unreviewed');
  if(Q.length)openDoc(first>=0?first:0);}
// ---- scan zoom / pan -------------------------------------------------------
// The first version of this tool served the PDF itself and let the browser's
// PDF viewer handle zoom. This one renders a page image server-side, which is
// faster and works the same over R2 later -- but it means zoom has to be
// implemented rather than inherited. Wheel to zoom at the cursor, drag to pan,
// double-click to fit. Scale persists across pages so comparing two pages at
// the same magnification does not mean re-zooming every time.
let Z=0,ox=0,oy=0,fitS=1,drag=null;
function apply(){const s=Z?Z:fitS;
  $('img').style.transform=`translate(${ox}px,${oy}px) scale(${s})`;
  $('zl').textContent=Z?Math.round(s*100)+'%':'fit';}
function zfit(){Z=0;ox=oy=0;
  const w=$('imgwrap'),im=$('img');
  if(im.naturalWidth){fitS=Math.min(w.clientWidth/im.naturalWidth,
                                    w.clientHeight/im.naturalHeight);
    ox=(w.clientWidth-im.naturalWidth*fitS)/2;oy=0;}
  apply();}
function setZ(ns,cx,cy){const w=$('imgwrap').getBoundingClientRect();
  const px=(cx-w.left-ox)/(Z||fitS), py=(cy-w.top-oy)/(Z||fitS);
  Z=Math.max(0.1,Math.min(8,ns));
  ox=cx-w.left-px*Z; oy=cy-w.top-py*Z; apply();}
function zoom(d){const w=$('imgwrap').getBoundingClientRect();
  setZ((Z||fitS)*(d>0?1.25:0.8), w.left+w.width/2, w.top+w.height/2);}
$('imgwrap').addEventListener('wheel',e=>{e.preventDefault();
  setZ((Z||fitS)*(e.deltaY<0?1.15:0.87), e.clientX, e.clientY);},{passive:false});
$('imgwrap').addEventListener('mousedown',e=>{drag={x:e.clientX-ox,y:e.clientY-oy};
  $('imgwrap').classList.add('drag');});
addEventListener('mousemove',e=>{if(!drag)return;
  ox=e.clientX-drag.x;oy=e.clientY-drag.y;apply();});
addEventListener('mouseup',()=>{drag=null;$('imgwrap').classList.remove('drag');});
$('imgwrap').addEventListener('dblclick',zfit);
$('img').addEventListener('load',()=>{if(!Z)zfit();else apply();});
addEventListener('resize',()=>{if(!Z)zfit();});

const VICON={approved:'✓ ',hold:'⏸ ',submitted:'↑ '};
function drawList(){const rows=visible();
  $('list').innerHTML=rows.map(([d,n])=>
  `<div class="q ${n==cur?'on':''} ${d.state==='approved'?'done':''} ${
     d.state==='hold'?'held':''} ${d.state==='submitted'?'subm':''}"
     onclick="openDoc(${n})">
   <span class=k>${VICON[d.verdict]||''}${
     d.repeats?'⟳ ':''}${d.thin?'· ':''}${esc(d.key)}${
     Object.keys(d.peers||{}).length?` <span class=pk>${
       Object.entries(d.peers).map(([w,v])=>
         `${esc(w)}:${VICON[v]?VICON[v].trim():'?'}`).join(' ')}</span>`:''}${
     (d.tags||[]).map(t=>`<span class=tag>${esc(t)}</span>`).join('')}</span>
   <span class=m>${d.repeats?`<b style=color:var(--warn)>${d.maxRep} identical rows in a row</b> · `:''}${d.rate}% text${
     d.twords?` · ${d.allRate}% w/tables`:''} · ${
     d.bad+d.tbad}/${d.words+d.twords} words · ${d.pages}p${
     d.noted?' · ✎':''}${
     d.thin?' · thin, rate unreliable':''}</span></div>`
  ).join('')||'<div class=q style=color:var(--dim2)>nothing matches this filter</div>';}
let cur=0;
// NOT named open(): inline onclick handlers resolve identifiers against the
// document object before window, so open() reached document.open(), which
// blanks the page and starts a new stream -- the white screen. Calls from
// normal scope (boot) hit the intended function, which is why only clicking broke.
async function openDoc(n){if(dirty&&!confirm('Discard unsaved edits?'))return;
  cur=n;i=0;D=await(await fetch('/doc?id='+Q[n].id)).json();
  drawList();drawTags();render();}

function marks(t,sp){if(!sp.length)return esc(t);
  sp=sp.slice().sort((a,b)=>a.s-b.s);let o='',p=0;
  for(const s of sp){if(s.s<p)continue;o+=esc(t.slice(p,s.s))+
    '<mark title="confidence '+s.c+'">'+esc(t.slice(s.s,s.e))+'</mark>';p=s.e;}
  return o+esc(t.slice(p));}

// The markdown carries only "[tbl-0.html](tbl-0.html)" where a table belongs --
// most of a receipt's content can sit behind that one placeholder. Swap each
// one for the real table so the read pane shows the whole page, in order,
// instead of a dead link.
function inlineTables(html,p){
  (p.tables||[]).forEach((t,n)=>{
    const id=t.id||('tbl-'+n+'.html');
    const q=id.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    html=html.replace(new RegExp('\\['+q+'\\]\\('+q+'\\)','g'),
      '<div class=tw><div class=tl>'+id+' — '+t.bad+'/'+t.words+
      ' suspect · edit it in the next pane</div>'+
      (t.saved!=null?t.saved:t.html)+'</div>');
  });
  return html;}

// Mark suspect values in the read pane's tables. Table words are indexed into
// the table's own content, not the page markdown, so they match by cell value
// rather than by character offset.
function markCells(root,p){
  const bad=new Set();(p.tables||[]).forEach(t=>t.suspect.forEach(s=>bad.add(s)));
  root.querySelectorAll('td,th').forEach(c=>{const v=c.textContent.trim();
    if(v&&bad.has(v))c.innerHTML='<mark>'+esc(v)+'</mark>';});}

// word-level LCS: short enough to be obviously correct, and a page of OCR is
// never large enough for the O(n*m) table to matter.
function diff(a,b){const A=a.split(/(\s+)/),B=b.split(/(\s+)/);
  const m=A.length,n=B.length,L=Array.from({length:m+1},()=>new Int32Array(n+1));
  for(let x=m-1;x>=0;x--)for(let y=n-1;y>=0;y--)
    L[x][y]=A[x]===B[y]?L[x+1][y+1]+1:Math.max(L[x+1][y],L[x][y+1]);
  let x=0,y=0,o='';
  while(x<m&&y<n){if(A[x]===B[y]){o+=esc(A[x]);x++;y++;}
    else if(L[x+1][y]>=L[x][y+1]){o+='<del>'+esc(A[x])+'</del>';x++;}
    else{o+='<ins>'+esc(B[y])+'</ins>';y++;}}
  return o+'<del>'+esc(A.slice(x).join(''))+'</del>'
          +'<ins>'+esc(B.slice(y).join(''))+'</ins>';}

// Tables live outside the markdown -- it carries only [tbl-N.html] -- so they
// get their own editors. Cells are contenteditable rather than raw-HTML
// textareas: the errors are inside cell VALUES ("MAINTAINED - LAP 1-A1"), and
// making someone hand-edit <td> tags to fix a word invites broken markup.
function drawTables(p){
  const host=$('tbl');host.innerHTML='';
  let tb=0,tw=0;
  (p.tables||[]).forEach((t,n)=>{
    tb+=t.bad;tw+=t.words;
    const h=document.createElement('div');
    h.className='th';
    h.innerHTML=`<span>table ${t.id||n} — editable</span>
                 <span>${t.bad}/${t.words} suspect</span>`;
    const box=document.createElement('div');
    box.innerHTML=t.saved!=null?t.saved:t.html;
    box.dataset.id=t.id||('tbl-'+n);
    box.querySelectorAll('td,th').forEach(c=>{
      c.setAttribute('contenteditable','true');
      const v=c.textContent.trim();
      if(v&&t.suspect.includes(v))c.innerHTML='<mark>'+esc(v)+'</mark>';
    });
    box.addEventListener('input',mark);
    host.appendChild(h);host.appendChild(box);
  });
  $('tc').textContent=(p.tables||[]).length
    ? `${p.tables.length} table(s) · ${tb}/${tw} suspect` : '';}

// Strip the highlight wrappers before storing, so a save never persists markup
// the review tool added for display.
function readTables(p){
  return Array.from($('tbl').children)
    .filter(el=>el.dataset&&el.dataset.id)
    .map(el=>{const c=el.cloneNode(true);
      c.querySelectorAll('mark').forEach(m=>m.replaceWith(m.textContent));
      c.querySelectorAll('[contenteditable]').forEach(x=>x.removeAttribute('contenteditable'));
      return {id:el.dataset.id,format:'html',content:c.innerHTML};});}

function render(){if(!D||!D.pages.length)return;
  const p=D.pages[i];
  $('pn').textContent=`${i+1}/${D.pages.length}`;
  $('fn').textContent=D.pdf||'(no pdf)';
  $('bc').textContent=`${p.bad}/${p.words}`;
  $('img').src=`/page.img?id=${p.pageId}&pdf=${encodeURIComponent(D.pdf||'')}&p=${p.docPage}`;
  $('orig').innerHTML=inlineTables(marks(p.text,p.spans),p);
  markCells($('orig'),p);
  $('ed').value=p.corrected!=null?p.corrected:p.text;
  const dmv=$('dm').value;
  $('df').innerHTML = dmv==='other'
    ? ((p.others||[]).length?diff(p.text,p.others[0].text)
       :'<i style=color:#8b93a7>no other reviewer on this page</i>')
    : diff(p.text,$('ed').value);
  drawTables(p);
  $('note').value=p.note||'';
  $('noteh').textContent=`note — page ${i+1} of ${D.pages.length}: what is wrong?`;
  // What the other reviewer did on THIS page -- their note, and whether their
  // text differs from Mistral's. Read-only: their row is theirs, and saving
  // never touches it.
  $('others').innerHTML=(p.others||[]).map(o=>{
    const changed=o.text&&o.text!==p.text;
    return `<div class=ob><h4>${esc(o.by)} — ${o.when}</h4>`+
      (o.note?`<div class=on>${esc(o.note)}</div>`:
              '<div class=on style=color:#8b93a7>(no note)</div>')+
      `<div class=oc>${changed?'edited the text — '+
        `<a href="#" onclick="showOther(${p.others.indexOf(o)});return false"
         style=color:#7dd3fc>see their diff</a>`
        :'no text change'}</div></div>`;}).join('')
    ||'<div class=ob style=color:#5b6478>no other reviewer on this page</div>';
  unmark();
  $('st').textContent=p.corrected!=null
    ?('saved'+(p.note?' · noted':'')) : '';}

// Collapse the list and the diff to give the scan and the editor real width.
// Preference sticks, because a review of 278 documents is many sittings.
function tog(id){const e=$(id);e.classList.toggle('hide');
  try{localStorage.setItem('h_'+id,e.classList.contains('hide')?'1':'')}catch(_){}}
['side','p-diff'].forEach(id=>{try{
  if(localStorage.getItem('h_'+id))$(id).classList.add('hide')}catch(_){}});

$('ed').addEventListener('input',()=>{mark();
  $('df').innerHTML=$('dm').value==='prior'?$('df').innerHTML
    :diff(D.pages[i].text,$('ed').value);});

function pg(d){if(dirty&&!confirm('Discard unsaved edits?'))return;
  i=Math.max(0,Math.min(D.pages.length-1,i+d));render();}

async function save(){const p=D.pages[i],t=$('ed').value,tb=readTables(p),
  nt=$('note').value;
  const r=await fetch('/save',{method:'POST',body:JSON.stringify(
    {pageId:p.pageId,text:t,tables:tb,note:nt})});
  const j=await r.json();
  if(j.error){$('st').textContent='SAVE FAILED: '+j.error;return;}
  p.corrected=t;p.note=nt;
  (p.tables||[]).forEach((x,n)=>{if(tb[n])x.saved=tb[n].content;});
  unmark();
  $('st').textContent='saved '+new Date().toLocaleTimeString();}

// ALWAYS saves first. A verdict must never discard the edits or the note that
// justify it -- that ambiguity is the whole reason there are four states.
async function setVerdict(v){
  if(dirty)await save();
  if(dirty)return;                    // save failed; the error is on screen
  const r=await fetch('/verdict',{method:'POST',
    body:JSON.stringify({id:D.id,verdict:v})});
  const j=await r.json();
  if(j.error){$('st').textContent='FAILED: '+j.error;return;}
  Q[cur].verdict=v;Q[cur].state=effState(Q[cur]);
  drawCounts();drawFilters();drawList();
  // Next unhandled document WITHIN the current filter, so working a filtered
  // slice (say, loops only) walks that slice rather than jumping out of it.
  const nx=visible().find(([d,n])=>n!==cur&&d.state==='unreviewed');
  if(nx)openDoc(nx[1]);else $('st').textContent='no unreviewed left in this filter';}

// ---- tags ------------------------------------------------------------------
// Shared, document-level. Click a chip to remove it; the select adds one.
// v2/v3 mark the re-upload round; the rest describe what is wrong.
function drawTags(){const d=Q[cur];if(!d)return;
  $('tags').innerHTML=(d.tags||[]).map(t=>
    `<span class=tag onclick="rmTag('${esc(t).replace(/'/g,"\\'")}')"
      title="click to remove">${esc(t)} ×</span>`).join('');}
async function pushTags(tags){
  const r=await fetch('/tags',{method:'POST',
    body:JSON.stringify({id:D.id,tags})});
  const j=await r.json();
  if(j.error){$('st').textContent='TAGS FAILED: '+j.error;return false;}
  Q[cur].tags=tags;drawTags();drawList();return true;}
function addTag(v){$('tagsel').value='';if(!v)return;
  if(v==='__custom'){v=(prompt('tag:')||'').trim();if(!v)return;}
  const t=[...(Q[cur].tags||[])];
  if(!t.some(x=>x.toLowerCase()===v.toLowerCase()))t.push(v);
  pushTags(t);}
function rmTag(v){pushTags((Q[cur].tags||[]).filter(t=>t!==v));}

$('note').addEventListener('input',mark);

document.addEventListener('keydown',e=>{if(e.target.tagName==='TEXTAREA'){
  if(e.key==='s'&&(e.ctrlKey||e.metaKey)){e.preventDefault();save();}return;}
  if(e.key==='ArrowRight')pg(1);if(e.key==='ArrowLeft')pg(-1);
  if(e.key==='[')tog('side');if(e.key===']')tog('p-diff');});
boot();
</script>"""


if __name__ == "__main__":
    with db() as c, c.cursor() as cur:
        q = build_queue(cur, SOLO or (sorted(USERS) or ["alden"])[0])
    print("OCR review  ->  http://%s:%d" % (HOST, PORT))
    flagged = sum(1 for d in q if d.get("flagged"))
    print("%d documents (%d flagged over %.0f%% suspect words, threshold %.2f)"
          % (len(q), flagged, GATE, BAD_WORD))
    print("corrections write to Neon as method='%s:<reviewer>'; Mistral untouched"
          % HUMAN)
    print("page images : %s" % ("R2 %s/%s (signed, %ds)"
                                % (R2_BUCKET, R2_PREFIX, R2_SIGN_TTL)
                                if USE_R2 else "local render from %s" % RECUT))
    print("auth        : %s" % (", ".join(sorted(USERS)) if USERS
                                else "OPEN (solo mode as '%s')" % (SOLO or "alden")))
    # Only pop a browser when a human is sitting at this machine. In a container
    # there is no browser, and HOST is 0.0.0.0 because Coolify's proxy terminates
    # TLS in front and forwards here.
    if HOST.startswith("127.") and os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.0, webbrowser.open,
                        ("http://127.0.0.1:%d" % PORT,)).start()

    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    S((HOST, PORT), Handler).serve_forever()
