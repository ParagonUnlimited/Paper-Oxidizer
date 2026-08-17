# Security

This repository handles scanned legal documents and the credentials needed
to read them. It is hosted as a **private** GitHub repository on the
Paragon Unlimited org; treat the review-app URL the same way.

---

## Reporting a vulnerability

Please **do not** file a public GitHub issue for security-relevant findings.
Email the maintainer directly at `businessleadership@paragonunlimited.pro`
with the details below. We will acknowledge within two business days.

> **Note on PGP.** Today we do not publish a GPG key for the security alias
> — the maintainer is reachable at the address above, which is on the same
> trusted path as every other project channel. v1 of this policy keeps that
> trust on the channel itself. If/when a GPG key is added, this file will
> gain an `### PGP key` section with the fingerprint and signing policy.

What to include:

- The affected path or URL (`/page_verdict`, the Coolify deploy, R2 keys,
  the `recut/` mount, etc.)
- Reproduction steps — local-only if the issue is exposed; theoretical if
  not.
- What you saw and what you expected.
- Whether the issue is currently reachable (public deploy URL, internal
  Coolify bind, local loopback only).

Please do not exploit the finding beyond what is needed to demonstrate it.
Do not exfiltrate document content, reviewer credentials, or R2 keys.

---

## Supported versions

| Version | Supported |
|---|---|
| latest | yes |
| previous | yes, security fixes only |
| older | no |

We ship security patches into the latest release first, then backport where
practical.

---

## Threat model — what is in scope

- A reviewer who has a valid cookie trying to read documents they should
  not see (multi-tenant scope, rejected in `review/app.py` fail-closed)
- An unauthenticated browser hitting the review app
  (`REVIEW_USERS` is mandatory on non-loopback bind)
- A misconfigured Coolify env that downgrades the app silently
  (every named env var is fail-closed at startup, by name)
- Any path that lets the review app serve a working queue with no images
  (`R2_*` or local `recut/` is required)
- A page-image URL signature that can be forged (`hmac = "0.12"` with
  `SESSION_SECRET` as the key; forge-tested in `review/smoke_test.py`)

Out of scope: the source PDFs themselves (they are unencrypted by design —
they were scanned and the user owns them). If you believe a scanned document
itself is sensitive, that is a question for the document owner, not this
repo.

---

## Acknowledgements

We name reporters in `CHANGELOG.md` after the fix ships, unless asked to
stay anonymous.
