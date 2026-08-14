# Mistral OCR — complete parameter reference

Source: the installed SDK (`mistralai` 2.8.0, the exact version the runner uses),
cross-checked against docs.mistral.ai and the live `/v1/models` endpoint.
Extracted 2026-08-13. One note up front: **parameters are endpoint-level, not
model-level** — `mistral-ocr-4-1` exposes exactly the same surface as every
other OCR model id. Nothing below is 4-1-specific.

## Models (live on this account's key)

`mistral-ocr-2512` · `mistral-ocr-3` · `mistral-ocr-3-0` · `mistral-ocr-4` ·
`mistral-ocr-4-0` · `mistral-ocr-4-1` · `mistral-ocr-latest`

Pricing (model card, OCR 4): $4/1k pages · $2/1k via Batch API · $5/1k annotated.

## Request — every parameter, every option

| # | Parameter | Type | Every possible value | Default | What it does |
|---|---|---|---|---|---|
| 1 | `model` | str | one of the 7 ids above | — required | which OCR model runs |
| 2 | `document` | union | **3 chunk types**: `{type:"document_url", document_url, document_name?}` — public URL, `data:application/pdf;base64,...` URI, or signed upload URL · `{type:"image_url", image_url}` — single image · `{type:"file", file_id}` — a file uploaded via `client.files.upload(purpose="ocr")`, by id | — required | the input |
| 3 | `pages` | list[int] \| str | omit = all pages · list `[0,1,2]` · range string `"0,2-4"` — **0-indexed** | all | which pages to process |
| 4 | `include_image_base64` | bool | `true` / `false` / unset | unset | return extracted images' data in the response (bbox annotation works WITHOUT this — verified live 2026-08-13) |
| 5 | `image_limit` | int | any int / unset | unset | max images extracted per document |
| 6 | `image_min_size` | int | any int / unset | unset | minimum height AND width (px) for an image region to be extracted — noise filter |
| 7 | `bbox_annotation_format` | ResponseFormat | unset / json_schema wrapper (below) | unset | structured extraction run per extracted image |
| 8 | `document_annotation_format` | ResponseFormat | unset / json_schema wrapper | unset | structured extraction over the whole document (sees full OCR markdown + first 8 image bboxes) |
| 9 | `document_annotation_prompt` | str | unset / any string | unset | guidance for #8; requires #8 to be set |
| 10 | `table_format` | enum | **3 states**: unset = tables inline in markdown · `"markdown"` · `"html"` = standalone table objects | unset | table rendering |
| 11 | `extract_header` | bool | `true` / `false` | None | move page header out of markdown into its own field |
| 12 | `extract_footer` | bool | `true` / `false` | None | same for footer |
| 13 | `include_blocks` | bool | `true` / `false` | **True** | paragraph-level bounding boxes for all content blocks |
| 14 | `confidence_scores_granularity` | enum | **3 states**: unset = **no scores** · `"page"` · `"word"` | unset | confidence reporting; must be set explicitly to get any |

## ResponseFormat wrapper (parameters 7 and 8)

```json
{"type": "json_schema",
 "json_schema": {"name": "...", "schema_definition": {...},
                 "description": "...", "strict": true}}
```

- `type` options: `"text"` · `"json_object"` · `"json_schema"` — **only
  `json_schema` is valid for the two annotation fields** (SDK docstring).
- `json_schema.name` str (required) · `schema_definition` dict (required;
  serializes to `schema` on the wire) · `description` str? · `strict` bool.
- The schema itself must be the strict structured-output dialect: every object
  `additionalProperties:false`, every property in `required`, optionality via
  `["type","null"]` unions.

## Response — everything that comes back

| Object | Fields |
|---|---|
| top level | `pages[]` · `model` · `usage_info {pages_processed, doc_size_bytes}` · `document_annotation` (JSON **string** of the doc schema output) |
| each page | `index` · `markdown` · `images[]` · `dimensions {dpi, height, width}` · `tables[]` · `hyperlinks[]` · `header` · `footer` · `confidence_scores` · `blocks[]` |
| confidence_scores | `average_page_confidence_score` · `minimum_page_confidence_score` · `word_confidence_scores[] {text, confidence, start_index}` (start_index ties each word to its markdown offset) |
| each image | `id` · `top_left_x/y` · `bottom_right_x/y` · `image_base64?` · `image_annotation` (JSON string of the image schema output, per image) |
| each table | `id` · `content` · `format` (`"markdown"` \| `"html"`) · own `word_confidence_scores[]` |

## Block taxonomy — all 13 types

Every block: `top_left_x, top_left_y, bottom_right_x, bottom_right_y, content, type`.

`text` · `title` · `header` · `footer` · `caption` · `aside_text` · `list` ·
`table` (+`table_id` → links to the standalone table object) · `image`
(+`image_id` → links to the image object) · `code` · `equation` · `references`
· `signature`

## Known-unverified items

- Whether `/v1/batch` accepts the two annotation formats (and whether the $2/1k
  batch rate applies to annotated pages). Resolve before the bulk run; the
  synchronous path is fully verified.
- Max page count / file size per request: not stated in the SDK; the 411-page
  upload path will test it.

## Our approved settings (for the record)

`table_format="html"` (raw HTML kept in Neon; a plain-text flattening is derived
for the PDF text layer and Paperless) · `extract_header/footer=true` (footer text always
stored — batch codes live there) · `include_blocks=true` ·
`confidence_scores_granularity="word"` · `include_image_base64=false` · both
annotation formats on (schemas in `mistral-annotation-schema.json` /
`mistral-image-annotation-schema.json`, prompt in
`mistral-annotation-prompts.md`) · `image_limit`/`image_min_size` unset for
run 1 · model pinned per benchmark, never `-latest`.
