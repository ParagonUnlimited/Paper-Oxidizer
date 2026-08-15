# /// script
# requires-python = ">=3.10"
# ///
"""M1 gate: the Rust server, against LIVE Neon and LIVE R2, must behave exactly
like v1 on the security surface and actually serve a real page image.

Waits for the server to come up (first cargo run compiles the binary), then:
  - /healthz open
  - anonymous /api/queue and /page.img refused (401)
  - wrong password refused, right password sets rev= cookie
  - /whoami round trip
  - /api/queue returns the corpus counts from Neon over DIRECT TLS
  - /page.img 302s to a signed R2 URL and THAT URL returns a real JPEG
  - forged cookie signature refused
"""
import http.client, json, sys, time, urllib.request

HOST, PORT = "127.0.0.1", 8779
ok = fail = 0

def check(label, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print("  PASS  %s" % label)
    else:    fail += 1; print("  FAIL  %s   %s" % (label, detail))

def req(method, path, body=None, cookie=None):
    c = http.client.HTTPConnection(HOST, PORT, timeout=30)
    h = {}
    if cookie: h["Cookie"] = cookie
    if body is not None:
        body = json.dumps(body); h["Content-Type"] = "application/json"
    c.request(method, path, body, h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()

# wait up to 180s for first-compile + startup
deadline = time.time() + 180
while True:
    try:
        s, _, _ = req("GET", "/healthz")
        if s == 200: break
    except OSError:
        pass
    if time.time() > deadline:
        sys.exit("server never came up")
    time.sleep(2)
print("server is up")

check("healthz open", True)
s, _, b = req("GET", "/api/queue")
check("anonymous queue refused", s == 401, s)
s, _, b = req("GET", "/page.img?id=796")
check("anonymous image refused", s == 401, s)
s, _, b = req("POST", "/login", {"user": "alden", "pw": "wrong"})
check("wrong password refused", s == 401, s)
s, h, b = req("POST", "/login", {"user": "alden", "pw": "m1-test-pw"})
setc = h.get("set-cookie") or h.get("Set-Cookie") or ""
check("login sets rev= cookie", s == 200 and setc.startswith("rev=alden|"),
      "%s %r" % (s, setc[:40]))
cookie = setc.split(";")[0]
s, _, b = req("GET", "/whoami", cookie=cookie)
check("whoami", s == 200 and json.loads(b)["reviewer"] == "alden", b[:60])
s, _, b = req("GET", "/api/queue", cookie=cookie)
q = json.loads(b)
check("queue reads Neon over direct TLS", s == 200 and q.get("total") == 1464, b[:100])
s, h, b = req("GET", "/page.img?id=796", cookie=cookie)
loc = h.get("location") or h.get("Location") or ""
check("image 302s to signed R2 URL",
      s in (302, 307) and "X-Amz-Signature" in loc and "/pages/796.jpg" in loc,
      "%s %r" % (s, loc[:80]))
raw = b""
try:
    with urllib.request.urlopen(loc, timeout=60) as r:
        raw = r.read()
except Exception as e:                                            # noqa: BLE001
    pass
check("R2 returns a real JPEG", raw[:2] == b"\xff\xd8" and len(raw) > 10000,
      "%d bytes %r" % (len(raw), raw[:4]))
s, _, b = req("GET", "/whoami", cookie="rev=alden|deadbeef")
check("forged signature refused", s == 401, s)
s, _, b = req("GET", "/")
check("static shell served", s == 200 and b"OCR review" in b, s)

print()
print("PASS %d   FAIL %d" % (ok, fail))
sys.exit(1 if fail else 0)
