# /// script
# requires-python = ">=3.10"
# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24", "boto3>=1.34"]
# ///
"""The submit/final/tags round trip, against live Neon, leaving no trace.

Uses the LAST document in the queue (least likely to be actively reviewed),
walks it unreviewed -> submitted -> approved -> hold -> cleared, sets and
clears tags, and asserts the queue reflects each step. Every write is undone.
"""
import http.client, importlib, json, os, socketserver, sys, threading, time

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)
os.chdir(APP)
os.environ["REVIEW_USERS"] = "alden:wf-pw"
os.environ["SESSION_SECRET"] = "wf-secret"
os.environ["HOST"] = "127.0.0.1"

import ocr_review_app as app
importlib.reload(app)

ok = fail = 0
def check(label, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print("  PASS  %s" % label)
    else:    fail += 1; print("  FAIL  %s   %s" % (label, detail))

class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
srv = S(("127.0.0.1", 8896), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)

def req(method, path, body=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", 8896, timeout=60)
    h = {"Cookie": cookie} if cookie else {}
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body, h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()

s, h, _ = req("POST", "/login", "user=alden&pw=wf-pw")
COOKIE = h.get("Set-Cookie", "").split(";")[0]
check("login", s == 302, s)

def queue():
    s, _, b = req("GET", "/queue", cookie=COOKIE)
    assert s == 200, b[:200]
    return json.loads(b)

q = queue()
check("queue returns the WHOLE corpus (1464)", len(q) == 1464, len(q))
check("rows carry conf tier", all(d.get("conf") in ("low","medium","high") for d in q))
check("rows carry effective state", all("state" in d for d in q))
check("rows carry tags list", all(isinstance(d.get("tags"), list) for d in q))
flagged = [d for d in q if d["flagged"]]
check("flagged subset = 256 (the old gate)", len(flagged) == 256, len(flagged))
check("flagged sort first", all(d["flagged"] for d in q[:len(flagged)]))
tiers = {t: sum(1 for d in q if d["conf"] == t) for t in ("low","medium","high")}
print("       tiers: %s" % tiers)

victim = q[-1]
did = victim["id"]
orig_verdict = victim["verdict"]
orig_tags = victim["tags"]
print("       victim doc %s (%s) verdict=%r tags=%r"
      % (did, victim["key"][:40], orig_verdict, orig_tags))

def set_verdict(v):
    s, _, b = req("POST", "/verdict",
                  json.dumps({"id": did, "verdict": v}), cookie=COOKIE)
    return s == 200, b[:120]

def state_of():
    return next(d for d in queue() if d["id"] == did)

okv, msg = set_verdict("submitted")
check("verdict submitted accepted", okv, msg)
check("state becomes submitted", state_of()["state"] == "submitted")
okv, _ = set_verdict("approved")
check("state becomes approved (final)", okv and state_of()["state"] == "approved")
okv, _ = set_verdict("hold")
check("hold trumps: state becomes hold", okv and state_of()["state"] == "hold")
okv, _ = set_verdict(None)
check("cleared back to unreviewed", okv and state_of()["state"] == "unreviewed")
s, _, b = req("POST", "/verdict",
              json.dumps({"id": did, "verdict": "banana"}), cookie=COOKIE)
check("bad verdict rejected", s == 400, s)

s, _, b = req("POST", "/tags",
              json.dumps({"id": did, "tags": ["v2", "needs-reocr", "V2"]}),
              cookie=COOKIE)
check("tags set", s == 200, b[:120])
t = state_of()["tags"]
check("tags dedupe case-insensitively", t == ["v2", "needs-reocr"], t)
s, _, b = req("POST", "/tags", json.dumps({"id": did, "tags": ["x" * 50]}),
              cookie=COOKIE)
check("overlong tag rejected", s == 400, s)
s, _, b = req("POST", "/tags", json.dumps({"id": did, "tags": orig_tags}),
              cookie=COOKIE)
check("tags restored", s == 200 and state_of()["tags"] == orig_tags)

if orig_verdict:                                  # restore if it had one
    set_verdict(orig_verdict)
final = state_of()
check("victim fully restored", final["verdict"] == orig_verdict
      and final["tags"] == orig_tags,
      "verdict=%r tags=%r" % (final["verdict"], final["tags"]))

srv.shutdown()
print()
print("PASS %d   FAIL %d" % (ok, fail))
sys.exit(1 if fail else 0)
