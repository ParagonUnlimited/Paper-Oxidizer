# Review this change for security vulnerabilities.

- **Session:** `b9e42505-5826-40f8-ba79-da81d32f731d`
- **Span:** 2026-08-15T08:43:19.403Z → 2026-08-15T08:43:40.903Z
- **Messages:** 1 user · 3 assistant
- **Source:** raw JSONL kept locally in `_session-recovery-backup/` (not in git — contains credentials). This file is the scrubbed conversation: all prose, tool calls as one-line markers, tool results omitted.

---

## 👤 User

Review this change for security vulnerabilities.

Changed files (you may Read these and any other file in the repo):
  - docker-compose.yml

Unified diff (only + lines are new):

=== DIFF: docker-compose.yml ===
@@ -62,6 +62,20 @@ services:
       HOST: 0.0.0.0
       PORT: "8778"
       NO_BROWSER: "1"
+      # REQUIRED TO REACH NEON THROUGH CLOUDFLARE GATEWAY.
+      #
+      # Postgres defaults to NEGOTIATED TLS: it opens a plaintext TCP connection,
+      # sends an SSLRequest, then upgrades. That first packet carries no SNI, so
+      # the Gateway rule "ALLOW NET - Server egress core" -- which matches on
+      # net.sni.domains against the cfzt-server-egress-core list -- cannot see
+      # which host is being reached, and "Block All Network Traffic not auth by
+      # email" drops it. The symptom is "server closed the connection
+      # unexpectedly" against every Neon IP, which reads like Neon being down.
+      #
+      # direct = TLS first, WITH SNI, so the allowlist matches. The Neon hosts
+      # (both direct and pooler) are already in that list. Paperless on this same
+      # VPS needs the identical setting.
+      PGSSLNEGOTIATION: direct
     # Coolify writes the variables you set in its UI to a .env beside the
     # generated compose and loads them from there. Declaring it explicitly costs
     # nothing if Coolify already does it, and removes the doubt if it does not.


Investigate per the method in your instructions, then return the findings list.

---

## 🤖 Claude

> 🔧 `Read` — docker-compose.yml

---

## 🤖 Claude

I've read the full docker-compose.yml. The diff adds a single environment variable `PGSSLNEGOTIATION: direct` to the ocr-review service. This setting instructs libpq to perform a TLS-first handshake (sending ClientHello with SNI immediately) instead of the default negotiated flow (plaintext TCP → SSLRequest → upgrade).

Analysis:
- **Entry points / sinks touched**: none. No new HTTP handler, no new subprocess, no new file/path/URL sink. The added variable is consumed by libpq inside the container.
- **Data flow**: `PGSSLNEGOTIATION=direct` is a hardcoded literal, not attacker-influenceable.
- **Security posture**: this is a hardening change, not a regression. `direct` mode eliminates the plaintext SSLRequest packet and forces SNI-bearing TLS ClientHello. It does not disable certificate verification (that's governed by `PGSSLMODE`/`sslmode` in the connection string, which is unchanged). No downgrade path is introduced — if the server doesn't support direct SSL, libpq fails closed rather than falling back to plaintext.
- **Sibling checks**: no auth/permission/allowlist logic in this file; the credentials remain in Coolify UI (unchanged convention). No parser/validator differentials introduced. No CI/CD trigger changes. No IaC omitted args (this is compose, not Terraform, and the omitted knobs are unrelated to the change). No logging/observability sinks.
- **No new principal, no new grant, no new sink argument, no cap defeat, no state drift.**

---

## 🤖 Claude

> 🔧 `StructuredOutput`
