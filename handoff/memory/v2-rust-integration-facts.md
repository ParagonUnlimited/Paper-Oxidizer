---
name: v2-rust-integration-facts
description: "Non-obvious integration facts the v2 Rust stack depends on — TLS, channel binding, R2 checksums, toolchain"
metadata: 
  node_type: memory
  type: project
  originSessionId: b76fad19-299e-44e3-9a86-3befe45b4077
  modified: 2026-08-15T16:30:02.340Z
---

Paper-Oxidizer v2 (Rust, `v2/` in the repo) depends on four integration facts
that cost real debugging time on 2026-08-15 and are invisible from the code's
surface:

1. **Neon through Cloudflare Gateway needs direct TLS with SNI.** tokio-postgres
   `SslNegotiation::Direct` + rustls with `alpn_protocols = [b"postgresql"]`.
   sqlx cannot do this (PR #3879 unmerged) — do not "upgrade" to sqlx without
   rechecking.
2. **Neon's POOLER completes SCRAM without channel binding.** The shared
   connection string says `channel_binding=require`; tokio-postgres enforces it
   strictly and fails with "server did not use channel binding". v2 overrides to
   `ChannelBinding::Prefer` in `db.rs`. libpq (v1/Python) tolerated it.
3. **rustls needs an explicit CryptoProvider install** (`ring`) because the AWS
   SDK brings aws-lc-rs into the graph — otherwise a panic at first TLS use.
4. **aws-sdk-s3 against R2** needs `force_path_style(true)` and request/response
   checksum behaviour forced to `WhenRequired`, or R2 rejects uploads.

Toolchain: the laptop's default Rust host is windows-gnu with no dlltool; `v2/`
carries a rustup **directory override to stable-msvc** (machine-local rustup
state, not in git). Docker builds are Linux and unaffected.

Related: [[check-supermemory-before-researching]]
