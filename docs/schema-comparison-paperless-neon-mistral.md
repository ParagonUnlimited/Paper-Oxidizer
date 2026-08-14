# Schema comparison — Paperless (Django) · Neon pipeline · Mistral Document AI

**Sources, all verified 2026-08-13:** Paperless from the version-pinned v3.0.3
`src/documents/models.py` (the exact code running on the VPS); Neon from the DDL
created and loaded this week (live row counts shown); Mistral from the installed
SDK (`mistralai` 2.8.0) response models. Cross-database capability checked live
against the Neon project: `postgres_fdw 1.2` and `dblink 1.2` are available.

**Plain-language frame:** three systems, three jobs. Paperless's schema is built
for *humans filing and finding documents* (one row per document, one text blob,
tags, permissions). The Neon schema is built for *knowing everything about every
page* (pages are first-class, documents are groupings of pages, every machine
reading is kept). Mistral's output is *what one read of one file produces* — it
isn't a database at all until we store it, which the Neon schema absorbs.

---

## 1. Paperless-ngx v3.0.3 (Django models → PostgreSQL tables)

### Core filing tables

| Table (model) | Fields | Plain meaning |
|---|---|---|
| **Document** | `title` (128) · `content` (text) · `content_length` (generated) · `mime_type` · `checksum` (64, indexed) · `archive_checksum` · `page_count` · `created` (**Date**, indexed) · `modified` · `added` · `filename` / `archive_filename` (unique paths) · `original_filename` · `archive_serial_number` (unique int) · FKs: `correspondent`, `document_type`, `storage_path`, `owner` · M2M: `tags` · **versioning: `root_document` (self-FK) · `version_index` · `version_label`** · soft-delete | one row per document; ONE text blob; ONE date; knows its file by checksum |
| **Correspondent** | `name` · `match` · `matching_algorithm` · `is_insensitive` · `owner` | who sent it + auto-assign rule |
| **Tag** | same matching fields + `color` · `is_inbox_tag` · tree hierarchy | labels, nestable, auto-assignable |
| **DocumentType** | matching fields only | what kind of paper |
| **StoragePath** | matching fields + `path` template | where the file lives on disk |

### Flexible metadata

| Table | Fields | Notes |
|---|---|---|
| **CustomField** | `name` · `data_type` · `extra_data` | data types (exact): `STRING, URL, DATE, BOOL, INT, FLOAT, MONETARY, DOCUMENTLINK, SELECT, LONG_TEXT` |
| **CustomFieldInstance** | FK document + field · one `value_*` column **per type** (`value_text` 128, `value_date`, `value_monetary` + generated amount, `value_long_text`, …) | one row per field per document — typed slots, not free JSON |

### Human/ops layer (no counterpart in the other two schemas)

`Note` (comments per doc) · `ShareLink` / `ShareLinkBundle` (expiring public links)
· `SavedView` + filter rules · `UiSettings` · `PaperlessTask` (job tracking) ·
`Workflow` / `WorkflowTrigger` / `WorkflowAction` (+email/webhook actions —
including **webhooks**, relevant to n8n) · audit log · users/groups/permissions
on nearly everything (`owner`, view/change users+groups) · trash with 30-day
delay (soft-delete).

**What Paperless's schema cannot express:** anything about an individual page
(only `page_count`); more than one date per document; more than one reading of
the text; confidence; geometry; arbitrary structured facts (custom fields are
typed slots you predefine, max one value each per document).

---

## 2. Neon pipeline schema (ours, live, loaded)

| Table | Rows now | Fields | Plain meaning |
|---|---|---|---|
| **source_file** | 937 | `sha256` (unique) · `name` · `path` · `bytes` · `pages` · `origin` (v1-merged / v2-genius-scan) · `doc_identity` · `pdf_created` · `producer` · `meta` jsonb | every physical file that ever arrived, including both versions of the same paper |
| **source_page** | 2,618 | FK file · `page_no` · `status` (assigned/discarded/unresolved) · `reason` · `meta` jsonb (**fingerprint**, review decisions) | every page of every file, with its fate and identity |
| **document** | 512 | `key` (unique) · `label` · `issuer` · `account` · `doc_date` · `doc_kind` · `bucket` · `confidence` · `state` · `reordered` · `notes` · `meta` jsonb (missing-evidence, annotation later) | a logical document, independent of which file its pages sit in |
| **document_page** | 722 | FK document + page · `position` · `order_evidence` | which pages form which document, in what order — the assembly knowledge |
| **ocr_reading** | 2,327 | FK page · `method` · `text` · `blocks` jsonb · `confidence` jsonb · `model` · `ts` | every machine reading of every page, kept side by side (genius_scan_v1, tesseract_v1, vision_v1, soon mistral-ocr-4-1) |
| **output_file** | 581 | FK document · `name` · `path` · `sha256` · `build_version` · `embedded_reading` | every PDF we built, and which text is stamped inside it |
| **wanted** | 96 | `label` · `missing` jsonb · `evidence` · FK document (nullable) | documents known to be incomplete — the gaps register |

**Design rules:** pages and documents are separate things joined by membership
(regrouping = row updates, never rebuilds); every table carries `meta` jsonb so
no fact is ever discarded for lack of a column; readings accumulate rather than
replace.

---

## 3. What Mistral Document AI output needs stored — and where it lands

One `ocr.process` call returns (SDK-verified):

| Mistral output | Neon home | Status |
|---|---|---|
| raw response (everything, verbatim) | disk file (`ocr-mistral/<stem>.raw.json`); path noted in `meta` | designed |
| per-page `markdown` | `ocr_reading.text` (method `mistral-ocr-4-1`) | column exists |
| per-page `header` / `footer` / `blocks[]` (13 typed, with coords) / `tables[]` (**html** + per-table word confidences) / `images[]` (+ per-image annotation JSON) / `hyperlinks[]` / `dimensions` | `ocr_reading.blocks` jsonb — one structured object holds all of it | column exists |
| per-word confidence + page min/avg | `ocr_reading.confidence` jsonb | column exists |
| `document_annotation` — the fact sheet, **29 document-level fields as of 2026-08-13**: issuer, addressee, people, **addresses with 11 roles**, account numbers, case numbers, 29-type dates, **amounts with `amount_numeric`/`amount_role`/sign**, **`line_items[]`** (10 subfields incl. merchant, reference id, running balance, card last-4), **`payment_instruments[]`** (14 methods, check no., memo line, name on instrument), **`account_holder_names[]`**, **`transaction_parties`**, balance chain, meter readings, pagination, completeness, duplicates, handwriting, summary, tags, confidence | known documents → `document.meta.annotation`; v2-only files → **new `document` rows** with `state='mistral_proposed'` until reviewed | designed |
| model id, pages processed | `ocr_reading.model` + run manifest | exists |

**Genuine schema gaps: none** — re-confirmed after the 2026-08-13 accounting
upgrade (23 → 29 annotation fields). The jsonb columns absorb every output shape,
so **no Neon DDL change was needed**; the new accounting structures land inside
`document.meta.annotation` exactly like the rest.
The only *state machine* addition needed is new `document.state` values
(`mistral_proposed` → `approved`) for documents the fact sheet discovers in
v2-only files.

---

## 4. Side-by-side: the same concept in all three

| Concept | Paperless | Neon pipeline | Mistral output |
|---|---|---|---|
| A document | one `Document` row | `document` + memberships | one `documents[]` entry in the fact sheet |
| The file | `filename` + `checksum` | `source_file`/`output_file` + `sha256` | transient `file_id` on their servers |
| The text | **one** `content` blob | per page × per method, full history | `pages[].markdown` |
| Dates | **one** `created` (Date) | `doc_date` (sort key) + `dates[]` with 29 meanings | `dates[]` typed, labeled |
| Sender | `correspondent` FK + auto-match rules | `issuer` + canonical bucket | `issuer {name, dept, address, phone, web}` |
| Kind | `document_type` FK | `doc_kind` | 23-value enum + free text |
| Tags | M2M + matching rules | `suggested_tags` in meta | `suggested_tags[]` |
| Arbitrary facts | CustomField typed slots (predefined, one value each) | `meta` jsonb, label+value arrays | `account_numbers[]`, `amounts[]`, `line_items[]`… self-labeling |
| Pages | `page_count` integer only | first-class rows with fate + fingerprint | `pages[]` with geometry |
| Versions of a doc | **`root_document`/`version_index`/`version_label`** | `source_file.origin` + `doc_identity` | — |
| Confidence | — | per word, per page, per reading | per word (source of it) |
| Geometry | — | `blocks` jsonb | blocks/tables/images coords (source) |
| Permissions, sharing, notes | rich (owner, groups, links, notes) | — | — |
| Automation | Workflows (incl. **webhook actions**) | — (n8n external) | — |
| Audit / trash | audit log, soft-delete + delay | timestamps only | — |

## 5. The diff, distilled

**Only Paperless has:** the human layer — permissions, share links, notes,
saved views, workflows, matching rules, audit, trash. And native versioning,
which maps cleanly onto our v1→v2 story (a corrected re-upload can be a
*version* of the same Paperless document rather than a new one).

**Only Neon has:** page-level truth, reading history, fingerprints, assembly
knowledge (which pages form which document and in what order), page fates, and
the gaps register. None of this fits Paperless's shape at all.

**Only Mistral produces (and Neon captures):** word confidence, geometry, typed
dates/amounts/line-items. Transient unless stored — which is the whole reason
`ocr_reading` exists.

**Collision points and their mapping rules:**
- *Dates*: their one `created` ← our `doc_date`. The other 28 meanings live in
  Neon; `due_by` optionally becomes a Paperless DATE custom field.
- *Text*: their one `content` ← flattened final text (tables as readable rows).
  `PAPERLESS_OCR_MODE=auto` must stay, or Paperless overwrites it.
- *Custom fields*: map only what earns a slot — `sha256` (STRING, the join key),
  `due_by` (DATE), headline amount (MONETARY). Everything else stays in Neon.

## 6. If Paperless's database moves onto Neon (Level 2 — now viable)

Latency accepted (plan upgraded). Facts: one Neon project can hold the
pipeline DB and Paperless's DB as **separate databases** — Django owns its own
schema untouched, so the two schemas never collide. `postgres_fdw`/`dblink`
(verified available) then allow single-SQL joins across both — e.g., "every
Paperless document whose pages carry a low-confidence word," answered in one
query. Migration path is Paperless's documented exporter/importer or
dump/restore, pointing `PAPERLESS_DBHOST` at the Neon endpoint. Sequencing
recommendation stands: **after** the OCR mission completes — don't relocate the
vault while loading it.

## 7. Convergence sketch (the Rust-tool endgame)

A unified schema is visibly the Neon core (files/pages/documents/readings/
outputs/wanted) plus the four Paperless capabilities worth keeping: owners and
permissions, share links, notes, and a workflow/trigger table. Nothing in the
two schemas is contradictory — one is the brain, the other is a reading room;
a future tool bolts the reading-room tables onto the brain.
