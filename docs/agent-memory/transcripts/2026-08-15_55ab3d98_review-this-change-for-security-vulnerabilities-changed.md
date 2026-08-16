# Review this change for security vulnerabilities.

- **Session:** `55ab3d98-e153-43f5-a421-e12a898de2eb`
- **Span:** 2026-08-15T04:22:22.775Z → 2026-08-15T04:22:38.784Z
- **Messages:** 1 user · 4 assistant
- **Source:** raw JSONL kept locally in `_session-recovery-backup/` (not in git — contains credentials). This file is the scrubbed conversation: all prose, tool calls as one-line markers, tool results omitted.

---

## 👤 User

Review this change for security vulnerabilities.

Changed files (you may Read these and any other file in the repo):
  - review/e2e_r2_test.py

Unified diff (only + lines are new):

=== DIFF: review/e2e_r2_test.py ===
@@ -0,0 +1,157 @@
+# /// script
+# requires-python = ">=3.10"
+# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24", "boto3>=1.34"]
+# ///
+"""End-to-end proof that a reviewer can actually see a scan.
+
+The smoke test proves the app SIGNS a URL. This proves the signed URL RESOLVES:
+it logs in, asks for a real page, follows the redirect out to R2, and checks the
+bytes that come back are the JPEG that page is supposed to be -- right magic
+number, right size, matching the width/height recorded in Neon.
+
+Everything before this point could be true while a reviewer still saw a broken
+image, because nothing had ever fetched one.
+
+Needs R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY and NEON_DATABASE_URL.
+"""
+import http.client, importlib, json, os, socketserver, struct, sys, threading, time
+import urllib.request
+
+APP = os.path.dirname(os.path.abspath(__file__))
+sys.path.insert(0, APP)
+os.chdir(APP)
+
+os.environ.setdefault("R2_BUCKET", "dobbins-paperless-scans")
+os.environ.setdefault(
+    "R2_ENDPOINT",
+    "https://68cc04bc26e145bfaf919bd02eb787d8.r2.cloudflarestorage.com")
+os.environ["REVIEW_USERS"] = "alden:e2e-pw"
+os.environ["SESSION_SECRET"] = "e2e-secret"
+os.environ["HOST"] = "127.0.0.1"
+os.environ["PORT"] = "8894"
+
+for v in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "NEON_DATABASE_URL"):
+    if not os.environ.get(v):
+        sys.exit("%s is not set" % v)
+
+import ocr_review_app as app
+importlib.reload(app)
+
+ok = fail = 0
+
+
+def check(label, cond, detail=""):
+    global ok, fail
+    if cond:
+        ok += 1
+        print("  PASS  %s" % label)
+    else:
+        fail += 1
+        print("  FAIL  %s   %s" % (label, detail))
+
+
+class S(socketserver.ThreadingTCPServer):
+    allow_reuse_address = True
+    daemon_threads = True
+
+
+srv = S(("127.0.0.1", 8894), app.Handler)
+threading.Thread(target=srv.serve_forever, daemon=True).start()
+time.sleep(0.6)
+
+
+def req(method, path, body=None, cookie=None):
+    c = http.client.HTTPConnection("127.0.0.1", 8894, timeout=30)
+    h = {}
+    if cookie:
+        h["Cookie"] = cookie
+    if body is not None:
+        h["Content-Type"] = "application/x-www-form-urlencoded"
+    c.request(method, path, body, h)
+    r = c.getresponse()
+    return r.status, dict(r.getheaders()), r.read()
+
+
+print("=" * 66)
+print("END-TO-END: login -> signed URL -> real bytes from R2")
+check("R2 mode is on", app.USE_R2 is True, app.USE_R2)
+
+s, h, b = req("POST", "/login", "user=alden&pw=e2e-pw")
+cookie = h.get("Set-Cookie", "").split(";")[0]
+check("login", s == 302 and cookie.startswith("rev=alden|"), s)
+
+s, h, b = req("GET", "/queue", cookie=cookie)
+q = json.loads(b)
+check("queue non-empty", s == 200 and len(q) > 0, len(q))
+
+s, h, b = req("GET", "/doc?id=%d" % q[0]["id"], cookie=cookie)
+doc = json.loads(b)
+pid = doc["pages"][0]["pageId"]
+check("doc has a page", bool(pid), doc.get("id"))
+
+s, h, b = req("GET", "/page.img?id=%d" % pid, cookie=cookie)
+loc = h.get("Location", "")
+check("app 302s to a signed R2 URL",
+      s == 302 and "X-Amz-Signature" in loc and ("pages/%d.jpg" % pid) in loc,
+      "%s %r" % (s, loc[:80]))
+
+# The actual point of this file: does that URL return the image?
+raw = None
+try:
+    with urllib.request.urlopen(loc, timeout=60) as r:
+        raw = r.read()
+        ctype = r.headers.get("Content-Type")
+    check("R2 returns the object", len(raw) > 10000, len(raw or b""))
+    check("bytes are a real JPEG", raw[:2] == b"\xff\xd8" and raw[-2:] == b"\xff\xd9",
+          repr(raw[:4]))
+except Exception as e:                                            # noqa: BLE001
+    check("R2 returns the object", False, str(e)[:140])
+
+
+def jpeg_size(data):
+    i = 2
+    while i < len(data):
+        if data[i] != 0xFF:
+            i += 1
+            continue
+        m = data[i + 1]
+        if m in (0xC0, 0xC1, 0xC2, 0xC3):
+            h, w = struct.unpack(">HH", data[i + 5:i + 9])
+            return w, h
+        seg = struct.unpack(">H", data[i + 2:i + 4])[0]
+        i += 2 + seg
+    return None
+
+
+if raw:
+    import psycopg
+    con = psycopg.connect(os.environ["NEON_DATABASE_URL"], connect_timeout=30)
+    cur = con.cursor()
+    cur.execute("select width, height, bytes, r2_key, uploaded "
+                "from page_image where page_id = %s", (pid,))
+    row = cur.fetchone()
+    con.close()
+    check("Neon has this page's image row", row is not None, row)
+    if row:
+        w, hh, nbytes, key, uploaded = row
+        got = jpeg_size(raw)
+        check("pixel size matches Neon", got == (w, hh), "r2=%s neon=(%s,%s)" % (got, w, hh))
+        check("byte size matches Neon", len(raw) == nbytes, "r2=%d neon=%s" % (len(raw), nbytes))
+        check("row marked uploaded", uploaded is True, uploaded)
+        print("       page_id=%s key=%s %sx%s %.0f KB" % (pid, key, w, hh, len(raw) / 1024))
+
+# an expired/unsigned fetch must not work
+try:
+    bare = loc.split("?")[0]
+    with urllib.request.urlopen(bare, timeout=30) as r:
+        code = r.status
+except urllib.error.HTTPError as e:
+    code = e.code
+except Exception:                                                 # noqa: BLE001
+    code = None
+check("unsigned URL is refused", code in (400, 401, 403), code)
+
+srv.shutdown()
+print()
+print("PASS %d   FAIL %d" % (ok, fail))
+sys.exit(1 if fail else 0)


Investigate per the method in your instructions, then return the findings list.

---

## 🤖 Claude

Looking at this change: it's a single end-to-end test file (`review/e2e_r2_test.py`). Per the review instructions:

> Do NOT report: missing best-practice/hardening with no concrete impact, **test/mock files**, outdated deps, or volumetric DoS

Let me quickly verify this is genuinely test scope and not shipped as an entry point.

---

## 🤖 Claude

> 🔧 `Grep` — e2e_r2_test

---

## 🤖 Claude

Confirmed: the file is not referenced anywhere else — it's a standalone e2e test script (uv-style PEP 723 header, hardcoded test creds `e2e-pw`/`e2e-secret`, binds to 127.0.0.1 only, and `sys.exit`s on completion). Nothing here ships to production or gets imported by other modules.

The nominally "sensitive" patterns present are all intrinsic to test scope and covered by the exclusion:
- Hardcoded `REVIEW_USERS=alden:e2e-pw` and `SESSION_SECRET=[REDACTED]` — test-only, injected at runtime, server bound to localhost.
- `urllib.request.urlopen(loc, ...)` on a redirect Location — but `loc` is the response from the local test app itself, not user input.
- Reading `NEON_DATABASE_URL` and R2 keys from env — standard test wiring, no logging of secrets.

No production sink, entry point, or control path is introduced by this diff.

---

## 🤖 Claude

> 🔧 `StructuredOutput`
