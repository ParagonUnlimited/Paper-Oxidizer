# Review this change for security vulnerabilities.

- **Session:** `8e1d141d-ed7b-45b8-aabe-d7f20e7db067`
- **Span:** 2026-08-14T23:03:20.759Z → 2026-08-14T23:05:28.327Z
- **Messages:** 1 user · 4 assistant
- **Source:** raw JSONL kept locally in `_session-recovery-backup/` (not in git — contains credentials). This file is the scrubbed conversation: all prose, tool calls as one-line markers, tool results omitted.

---

## 👤 User

Review this change for security vulnerabilities.

Changed files (you may Read these and any other file in the repo):
  - review/ocr_review_app.py
  - review/smoke_test.py

Unified diff (only + lines are new):

=== DIFF: review/ocr_review_app.py ===
@@ -105,10 +105,38 @@ def _parse_users(raw):
 
 
 USERS = _parse_users(os.environ.get("REVIEW_USERS"))
-# Single-user local use keeps working with no login at all.
-SOLO = (os.environ.get("REVIEWER") or "").strip().lower() if not USERS else ""
-SECRET = (os.environ.get("SESSION_SECRET")
-          or os.environ.get("NEON_DATABASE_URL") or "dev").encode()
+LOOPBACK = HOST in ("127.0.0.1", "localhost", "::1")
+
+# FAIL CLOSED.
+#
+# The obvious way to keep single-machine use frictionless is "no REVIEW_USERS
+# means no login". That is a trapdoor: REVIEW_USERS arriving empty on the server
+# -- unset in Coolify, a typo, a missing colon, a secret that failed to inject --
+# would not break anything visibly. It would publish 1,464 probate documents,
+# including bank statements and a creditor's claim against the estate, to anyone
+# who found the URL. A misconfiguration must never widen access.
+#
+# So: no credentials is only tolerable when nothing outside this machine can
+# reach the socket. Bound anywhere else, the process refuses to start. The
+# container sets HOST=0.0.0.0, which makes a missing REVIEW_USERS a loud crash
+# in the deploy log instead of a silent exposure.
+if not USERS and not LOOPBACK:
+    sys.exit("REFUSING TO START: REVIEW_USERS is empty and HOST=%s is not "
+             "loopback.\nThat combination would serve every document with no "
+             "authentication.\nSet REVIEW_USERS='name:password,name:password'."
+             % HOST)
+
+# Solo mode: loopback only, and only because the socket is unreachable remotely.
+SOLO = ((os.environ.get("REVIEWER") or "alden").strip().lower()
+        if not USERS else "")
+
+# The signing key must be its own secret. Falling back to NEON_DATABASE_URL
+# would make a cookie-signing key out of a database credential -- one leak, two
+# compromises -- and a hardcoded default would let anyone mint a valid cookie.
+if USERS and not os.environ.get("SESSION_SECRET"):
+    sys.exit("REFUSING TO START: SESSION_SECRET is required when REVIEW_USERS "
+             "is set.\nWithout it the login cookie cannot be signed safely.")
+SECRET = (os.environ.get("SESSION_SECRET") or "loopback-solo-mode").encode()
 
 
 # ocr_reading carries a UNIQUE constraint on (page_id, method) --
@@ -443,10 +471,11 @@ def make_cookie(reviewer):
 def cookie_reviewer(header):
     """Whoever this request is, or None. Never trusts the name without the
     signature -- otherwise anyone could write corrections as anyone."""
+    # SOLO is only ever set when the listener is loopback-bound (enforced at
+    # startup). There is deliberately no other path that returns a reviewer
+    # without a verified signature.
     if SOLO:
         return SOLO
-    if not USERS:
-        return "alden"
     for part in (header or "").split(";"):
         part = part.strip()
         if not part.startswith("rev="):
@@ -512,9 +541,13 @@ class Handler(SimpleHTTPRequestHandler):
         self.send_response(302)
         self.send_header("Location", to)
         if cookie:
+            # Secure off-loopback: Coolify terminates TLS in front, so the
+            # cookie must never be allowed onto a plaintext hop. Left off for
+            # local http so the laptop case still works.
             self.send_header("Set-Cookie",
-                             "rev=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
-                             % cookie)
+                             "rev=%s; Path=/; HttpOnly; SameSite=Lax; "
+                             "Max-Age=2592000%s"
+                             % (cookie, "" if LOOPBACK else "; Secure"))
         self.send_header("Content-Length", "0")
         self.end_headers()
 


=== DIFF: review/smoke_test.py ===
@@ -0,0 +1,163 @@
+# /// script
+# requires-python = ">=3.10"
+# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24", "boto3>=1.34"]
+# ///
+"""Boot the review app in-process and exercise it over real HTTP.
+
+Two configurations, because the change that could break things is the one that
+turns a single-user script into a shared deployment:
+  A. solo   -- no REVIEW_USERS. Must behave exactly as before: no login.
+  B. shared -- REVIEW_USERS set. Must refuse anonymous access, accept a correct
+               password, reject a wrong one, and scope data to the logged-in user.
+"""
+import http.client, importlib, os, sys, threading, time, urllib.parse
+
+APP = r"C:\Users\busin\Documents\Paper-Oxidizer\.claude\worktrees\genius-extract-and-app\review"
+sys.path.insert(0, APP)
+os.chdir(APP)
+
+
+def boot(port, env):
+    for k in ("REVIEW_USERS", "REVIEWER", "SESSION_SECRET",
+              "R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
+        os.environ.pop(k, None)
+    os.environ.update(env)
+    os.environ["PORT"] = str(port)
+    os.environ["HOST"] = "127.0.0.1"
+    import ocr_review_app as app
+    importlib.reload(app)
+    import socketserver
+
+    class S(socketserver.ThreadingTCPServer):
+        allow_reuse_address = True
+        daemon_threads = True
+    srv = S(("127.0.0.1", port), app.Handler)
+    threading.Thread(target=srv.serve_forever, daemon=True).start()
+    time.sleep(0.6)
+    return srv, app
+
+
+def req(port, method, path, body=None, cookie=None, follow=False):
+    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
+    h = {}
+    if cookie:
+        h["Cookie"] = cookie
+    if body is not None:
+        h["Content-Type"] = "application/x-www-form-urlencoded"
+    c.request(method, path, body, h)
+    r = c.getresponse()
+    data = r.read()
+    return r.status, dict(r.getheaders()), data
+
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
+# ---------------------------------------------------------------- A: solo
+print("=" * 66)
+print("A. SOLO MODE (no REVIEW_USERS) -- must work with no login")
+srv, app = boot(8891, {"REVIEWER": "alden"})
+s, h, b = req(8891, "GET", "/healthz")
+check("/healthz 200", s == 200, s)
+s, h, b = req(8891, "GET", "/")
+check("/ serves the app, not a login form", s == 200 and b"OCR review" in b and b"Sign in" not in b, s)
+s, h, b = req(8891, "GET", "/queue")
+import json as _j
+q = _j.loads(b) if s == 200 else []
+check("/queue 200 and non-empty", s == 200 and len(q) > 0, "%s len=%d" % (s, len(q)))
+if q:
+    did = q[0]["id"]
+    s, h, b = req(8891, "GET", "/doc?id=%d" % did)
+    d = _j.loads(b)
+    check("/doc returns pages with pageId", s == 200 and d["pages"] and "pageId" in d["pages"][0], s)
+    pid = d["pages"][0]["pageId"]
+    s, h, b = req(8891, "GET", "/page.img?id=%d&pdf=%s&p=%d"
+                  % (pid, urllib.parse.quote(d["pdf"] or ""), d["pages"][0]["docPage"]))
+    check("/page.img renders locally (PNG)", s == 200 and b[:4] == b"\x89PNG", "%s %r" % (s, b[:8]))
+    print("       queue=%d docs, first doc %d has %d pages" % (len(q), did, len(d["pages"])))
+srv.shutdown()
+
+# ---------------------------------------------------------------- B: shared
+print()
+print("=" * 66)
+print("B. SHARED MODE (REVIEW_USERS set) -- login required, per-user scoping")
+srv, app = boot(8892, {"REVIEW_USERS": "alden:pw-a,jeff:pw-j",
+                       "SESSION_SECRET": "[REDACTED]"})
+s, h, b = req(8892, "GET", "/queue")
+check("anonymous /queue is refused", s == 401, s)
+s, h, b = req(8892, "GET", "/")
+check("anonymous / shows login form", s == 200 and b"Sign in" in b, s)
+
+s, h, b = req(8892, "POST", "/login", "user=jeff&pw=wrong")
+check("wrong password rejected", s == 401 and b"Wrong name" in b, s)
+
+s, h, b = req(8892, "POST", "/login", "user=jeff&pw=pw-j")
+setc = h.get("Set-Cookie", "")
+check("correct password sets a cookie + redirects", s == 302 and setc.startswith("rev=jeff|"), "%s %r" % (s, setc[:40]))
+cookie = setc.split(";")[0]
+
+s, h, b = req(8892, "GET", "/whoami", cookie=cookie)
+check("/whoami reports jeff", s == 200 and _j.loads(b)["reviewer"] == "jeff", b[:60])
+
+s, h, b = req(8892, "GET", "/queue", cookie=cookie)
+check("authenticated /queue 200", s == 200 and len(_j.loads(b)) > 0, s)
+
+# forged cookie must not be accepted
+s, h, b = req(8892, "GET", "/whoami", cookie="rev=alden|deadbeef")
+check("forged signature rejected", s == 401, s)
+
+# a name not in REVIEW_USERS must not be accepted even with a valid-looking sig
+s, h, b = req(8892, "GET", "/whoami", cookie="rev=mallory|" + app._sign("mallory"))
+check("unknown user rejected even with valid signature", s == 401, s)
+srv.shutdown()
+
+# ------------------------------------------------------- C: fail-closed
+# The previous version of this file asserted that "no REVIEW_USERS -> no login"
+# was CORRECT. That is the bug: an env var failing to arrive in Coolify would
+# have published the whole corpus with no authentication, silently. These tests
+# assert the process refuses to start instead.
+print()
+print("=" * 66)
+print("C. FAIL-CLOSED STARTUP -- misconfiguration must not widen access")
+import subprocess
+
+BASE_ENV = dict(os.environ)
+BASE_ENV.pop("REVIEW_USERS", None)
+BASE_ENV.pop("SESSION_SECRET", None)
+BASE_ENV.pop("REVIEWER", None)
+
+
+def run(env_extra, label, expect_exit, expect_text):
+    env = dict(BASE_ENV)
+    env.update(env_extra)
+    env["PORT"] = "8899"
+    p = subprocess.run([sys.executable, "ocr_review_app.py"],
+                       cwd=APP, env=env, capture_output=True, timeout=90)
+    out = (p.stdout + p.stderr).decode("utf-8", "replace")
+    check(label, p.returncode == expect_exit and expect_text.lower() in out.lower(),
+          "exit=%s out=%r" % (p.returncode, out[-160:]))
+
+
+run({"HOST": "0.0.0.0"}, "public bind + no REVIEW_USERS refuses to start",
+    1, "REFUSING TO START")
+run({"HOST": "0.0.0.0", "REVIEW_USERS": "alden"},   # malformed: no colon
+    "public bind + malformed REVIEW_USERS refuses to start",
+    1, "REFUSING TO START")
+run({"HOST": "0.0.0.0", "REVIEW_USERS": "alden:pw"},   # no SESSION_SECRET
+    "REVIEW_USERS without SESSION_SECRET refuses to start",
+    1, "SESSION_SECRET")
+
+print()
+print("=" * 66)
+print("PASS %d   FAIL %d" % (ok, fail))
+sys.exit(1 if fail else 0)


Investigate per the method in your instructions, then return the findings list.

---

## 🤖 Claude

> 🔧 `Read` — C:\Users\busin\Documents\Paper-Oxidizer\.claude\worktrees\genius-extract-and-app\review\ocr_review_app.py

---

## 🤖 Claude

> 🔧 `Grep` — REVIEWER|SOLO|USERS|LOOPBACK|HOST

---

## 🤖 Claude

> 🔧 `Glob` — **/Dockerfile*

---

## 🤖 Claude

> 🔧 `Read` — C:\Users\busin\Documents\Paper-Oxidizer\.claude\worktrees\genius-extract-and-app\review\Dockerfile
