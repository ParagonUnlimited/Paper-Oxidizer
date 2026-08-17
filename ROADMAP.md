# Roadmap

This file is a pointer. The actual plan and live state live in
[`PLAN-V2.md`](./PLAN-V2.md) and [`STATE.md`](./STATE.md) — read those for
the full picture. Anything in this file that disagrees with them is wrong by
definition; update this one, not them.

## Done

- Repo set up and the v1 review app shipped behind Coolify
- v2 review app (Rust + TypeScript) live on `ocr.dobbinscodex.cloud`
- Adjust worker — note/tag → remedy per page (re-OCR, block-geometry rebuild,
  same-issuer fanout)
- Loop 1 end-to-end smoke passes 29/29 gate criteria

## Next

- Loop 2 — build runner + sidecar pipeline (Spike A proven 12/12; the runner
  is the implementation step)
- Turso fork — vector + graph mirror (designed, scheduled at M4)
- Dehydrate and publish a non-private mirror if a sibling project wants
  to reuse the review app
