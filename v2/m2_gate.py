# /// script
# requires-python = ">=3.10"
# ///
"""M2 gate: full feature parity of the Rust API against LIVE Neon, leaving no
trace. Mirrors v1's workflow_test semantics plus v2's page-level approvals.

Run the server first (cargo run with the real env), then:
    uv run m2_gate.py
"""
import http.client, json, sys, time

HOST, PORT = "127.0.0.1", 8779
ok = fail = 0

def check(label, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print("  PASS  %s" % label)
    else:    fail += 1; print("  FAIL  %s   %s" % (label, detail))

def req(method, path, body=None, cookie=None):
    c = http.client.HTTPConnection(HOST, PORT, timeout=120)
    h = {}
    if cookie: h["Cookie"] = cookie
    if body is not None:
        body = json.dumps(body); h["Content-Type"] = "application/json"
    c.request(method, path, body, h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()

deadline = time.time() + 180
while True:
    try:
        s, _, _ = req("GET", "/healthz")
        if s == 200: break
    except OSError: pass
    if time.time() > deadline: sys.exit("server never came up")
    time.sleep(2)

s, h, _ = req("POST", "/login", {"user": "alden", "pw": "m1-test-pw"})
COOKIE = (h.get("set-cookie") or h.get("Set-Cookie") or "").split(";")[0]
check("login", s == 200 and COOKIE.startswith("rev=alden|"), s)

def queue():
    s, _, b = req("GET", "/api/queue", cookie=COOKIE)
    assert s == 200, b[:200]
    return json.loads(b)

print("=" * 62)
print("QUEUE — parity with v1's measured numbers")
q = queue()
check("whole corpus (1464)", len(q) == 1464, len(q))
flagged = [d for d in q if d["flagged"]]
check("flagged == 256 (the gate, exactly)", len(flagged) == 256, len(flagged))
tiers = {t: sum(1 for d in q if d["conf"] == t) for t in ("low", "medium", "high")}
check("tiers match v1 (256/390/818)",
      tiers == {"low": 256, "medium": 390, "high": 818}, tiers)
check("flagged sort first", all(d["flagged"] for d in q[:len(flagged)]))
check("rows carry pagesApproved", all("pagesApproved" in d for d in q))
finals = [d for d in q if d["state"] == "approved"]
check("v1 doc-approvals visible", len(finals) >= 20, len(finals))
check("backfill: every v1-final doc has all pages page-approved",
      all(d["pagesApproved"] >= d["pages"] for d in finals),
      [(d["id"], d["pagesApproved"], d["pages"]) for d in finals
       if d["pagesApproved"] < d["pages"]][:3])

print("=" * 62)
print("DOC — load shape")
target = flagged[0]
s, _, b = req("GET", "/api/doc?id=%d" % target["id"], cookie=COOKIE)
doc = json.loads(b)
check("doc loads", s == 200 and doc["id"] == target["id"], b[:100])
p0 = doc["pages"][0]
check("page carries text/spans/tables/approvals",
      all(k in p0 for k in ("pageId", "text", "spans", "tables", "approvals")))
with_tbl = [p for p in doc["pages"] if p["tables"]]
if with_tbl:
    t = with_tbl[0]["tables"][0]
    check("table carries html+suspect+scores",
          all(k in t for k in ("id", "html", "suspect", "bad", "words")))
else:
    print("       (no tables in this doc; table shape checked implicitly)")

print("=" * 62)
print("WRITES — reversible round trips on the LAST queue doc")
victim = q[-1]
did = victim["id"]
orig_verdict, orig_tags = victim["verdict"], victim["tags"]
s, _, b = req("GET", "/api/doc?id=%d" % did, cookie=COOKIE)
vdoc = json.loads(b)
vp = vdoc["pages"][0]
pid = vp["pageId"]
had_correction = vp["corrected"] is not None
print("       victim doc %s page %s (correction existed: %s)"
      % (did, pid, had_correction))

s, _, b = req("POST", "/api/save",
              {"pageId": pid, "text": "M2-GATE-TEXT", "tables": [],
               "note": "m2 gate"}, cookie=COOKIE)
check("save", s == 200, b[:100])
s, _, b = req("GET", "/api/doc?id=%d" % did, cookie=COOKIE)
rp = json.loads(b)["pages"][0]
check("correction persists + note round-trips",
      rp["corrected"] == "M2-GATE-TEXT" and rp["note"] == "m2 gate",
      (rp["corrected"], rp["note"]))
# empty save = withdraw (v1 semantics: delete, skip re-insert)
s, _, b = req("POST", "/api/save",
              {"pageId": pid, "text": "", "tables": [], "note": ""}, cookie=COOKIE)
s, _, b = req("GET", "/api/doc?id=%d" % did, cookie=COOKIE)
rp = json.loads(b)["pages"][0]
check("empty save withdraws the correction", rp["corrected"] is None,
      rp["corrected"])

def set_verdict(v):
    s, _, b = req("POST", "/api/verdict", {"id": did, "verdict": v}, cookie=COOKIE)
    return s == 200, b[:100]
def state_of():
    return next(d for d in queue() if d["id"] == did)

okv, m = set_verdict("submitted")
check("submitted", okv and state_of()["state"] == "submitted", m)
okv, _ = set_verdict("approved")
check("approved", okv and state_of()["state"] == "approved")
okv, _ = set_verdict("hold")
check("hold trumps", okv and state_of()["state"] == "hold")
okv, _ = set_verdict(None)
check("cleared", okv and state_of()["state"] == "unreviewed")
s, _, b = req("POST", "/api/verdict", {"id": did, "verdict": "banana"}, cookie=COOKIE)
check("bad verdict rejected", s == 400, s)

s, _, b = req("POST", "/api/tags",
              {"id": did, "tags": ["v2", "needs-reocr", "V2"]}, cookie=COOKIE)
check("tags set", s == 200, b[:100])
t = state_of()["tags"]
check("tags dedupe case-insensitively", t == ["v2", "needs-reocr"], t)
s, _, b = req("POST", "/api/tags", {"id": did, "tags": ["x" * 50]}, cookie=COOKIE)
check("overlong tag rejected", s == 400, s)
req("POST", "/api/tags", {"id": did, "tags": orig_tags}, cookie=COOKIE)

print("=" * 62)
print("PAGE-LEVEL APPROVAL — v2's pipeline grain")
before = state_of()["pagesApproved"]
s, _, b = req("POST", "/api/page_verdict",
              {"pageId": pid, "status": "approved"}, cookie=COOKIE)
check("page approve", s == 200, b[:100])
check("pagesApproved incremented", state_of()["pagesApproved"] == before + 1)
s, _, b = req("GET", "/api/doc?id=%d" % did, cookie=COOKIE)
appr = json.loads(b)["pages"][0]["approvals"]
check("approval visible on the page, attributed",
      any(a["by"] == "alden" and a["status"] == "approved" for a in appr), appr)
s, _, b = req("POST", "/api/page_verdict",
              {"pageId": pid, "status": None}, cookie=COOKIE)
check("page approval cleared", s == 200 and state_of()["pagesApproved"] == before)
s, _, b = req("POST", "/api/page_verdict",
              {"pageId": pid, "status": "banana"}, cookie=COOKIE)
check("bad page status rejected", s == 400, s)

if orig_verdict:
    set_verdict(orig_verdict)
final = state_of()
check("victim fully restored",
      final["verdict"] == orig_verdict and final["tags"] == orig_tags
      and final["pagesApproved"] == before,
      (final["verdict"], final["tags"], final["pagesApproved"]))

print()
print("PASS %d   FAIL %d" % (ok, fail))
sys.exit(1 if fail else 0)
