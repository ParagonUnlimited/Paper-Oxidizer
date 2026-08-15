---
name: papra-api-integration-facts
description: "Verified Papra API facts the delivery pipeline depends on — upload semantics, enrichment calls, key scoping, dedup 409, size cap"
metadata: 
  node_type: memory
  type: project
  originSessionId: b76fad19-299e-44e3-9a86-3befe45b4077
  modified: 2026-08-15T21:42:49.257Z
---

Verified 2026-08-15 against Papra `@papra/app@26.6.1` source + docs (research
agent, sourced). The pipeline delivers via the **API, not the ingestion
folder** — upload returns the document object (with id) synchronously; the
folder path calls the identical usecase but loses the id.

- Upload: `POST /api/organizations/{org}/documents`, multipart field `file`,
  **file only** — no metadata at upload. Enrich immediately after:
  `PATCH .../documents/{id}` `{name, documentDate, notes}` (NEVER patch
  `content` — it feeds FTS5 and async extraction writes it), then one
  `POST .../documents/{id}/tags {tagId}` per tag, one
  `PUT .../documents/{id}/custom-properties/{cpd_id} {value}` per property.
- Auth: `Authorization: Bearer ppapi_...`. **Keys are hardcoded
  allOrganizations:true and never expire** (org-scoping fields commented out in
  source) → create the pipeline key under a dedicated service user that belongs
  ONLY to the target org.
- **Duplicate = HTTP 409 with NO document id**, and there is no sha256 search —
  Neon must own the `sha256 → papra_document_id` mapping (artifact table).
- Size cap: `DOCUMENT_STORAGE_MAX_UPLOAD_SIZE` default **25 MiB**; corpus tails
  exceed it (one policy is 118 MB) → raise the env on the instance. API timeout
  `SERVER_API_ROUTES_TIMEOUT_MS=20000` may also need raising for big uploads.
- Webhooks: 5 events, Standard-Webhooks signing (`webhook-signature: v1,<b64>`
  over `id.timestamp.rawBody`); SSRF guard blocks private-IP listeners unless
  `WEBHOOK_URL_ALLOWED_HOSTNAMES` allowlists ours. Needed only for
  reconciliation — the id arrives synchronously.
- Session-only (no API): webhook CRUD, tagging-rule CRUD, api-key management,
  org settings (incl. toggling ai_auto_tagging). Do these in the UI once.
- Tagging rules match `name`/`content` only, applied ASYNC after content
  extraction; LLM auto-tagging (if on) adds tags on top of the pipeline's.
- UI reflects API changes on tab focus (TanStack Query), not push.
- Papra has NO vector/embedding story (confirmed) — the Turso layer duplicates
  nothing.

Related: [[v2-rust-integration-facts]]
