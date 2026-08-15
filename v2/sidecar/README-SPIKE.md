# Spike A — OCRmyPDF v17 custom-engine plugin (PROVEN)

**Objective:** prove that OCRmyPDF v17's plugin API can take our corrected OCR
text + Mistral block bounding boxes and produce a searchable PDF/A from a
scanned PDF, replacing the page's existing bad text layer — without touching
the page image.

**Result: PROVEN on the primary route (`--redo-ocr`), first attempt.
12/12 verification checks passed. Wall time 6.8 s for the one-page bill.**

## Files

| File | Purpose |
|---|---|
| `mistral_plugin.py` | OCRmyPDF plugin: replays Mistral raw.json as an `OcrEngine` (no real OCR) |
| `spike.py` | uv-runnable driver (PEP 723 inline deps): runs the real CLI, verifies with PyMuPDF |
| `out/out-redo.pdf` | The proven output: searchable PDF/A-2b, original image untouched |

Run with: `uv run spike.py` (from this directory).
The plugin finds the Mistral JSON via the `MISTRAL_OCR_JSON` env var
(inherited by ocrmypdf's spawned worker processes).

## Exact API surface that worked (ocrmypdf 17.10.0)

All read from the installed package source, not docs-from-memory:

1. **`OcrEngine.generate_ocr(input_file, options, page_number) -> tuple[OcrElement, str]`**
   plus **`supports_generate_ocr() -> True`** (`ocrmypdf/pluginspec.py`).
   v17 has a modern structured API — **no synthetic hOCR needed**. You return
   an `OcrElement` tree (`ocrmypdf.hocrtransform`: `OcrElement`, `OcrClass`,
   `BoundingBox`) rooted at `OcrClass.PAGE`, plus the plain-text sidecar
   string. `ocrmypdf/builtin_plugins/null_ocr.py` is the reference
   implementation. (`generate_hocr()` is only the fallback surface for
   engines that return False; we implement a synthetic-hOCR version anyway
   for documentation.)
2. **`@hookimpl get_ocr_engine(options)`** — firstresult hook. `--plugin`
   modules register after builtins and pluggy calls LIFO, so our engine wins
   without needing `--ocr-engine` (whose CLI choices are locked to
   `auto|tesseract|none`).
3. **`@hookimpl initialize(plugin_manager)`** →
   `plugin_manager.set_blocked('ocrmypdf.builtin_plugins.tesseract_ocr')`.
   Required: the builtin tesseract plugin's `check_options` unconditionally
   runs `check_external_program('tesseract', ...)` and raises
   `MissingDependencyError` on machines without the binary — even when
   another engine is selected. Blocking is the pattern documented in
   `pluginspec.py`.
4. **`@hookimpl register_options()`** → `{'tesseract': TesseractOptions}`
   (imported from the blocked builtin module — import is binary-free).
   Required because core `ocrmypdf/_validation_coordinator.py` reads
   `options.tesseract.pagesegmode` etc. unconditionally; blocking the plugin
   would otherwise leave the namespace unregistered → `AttributeError`.

### Coordinate chain (verified in source + empirically)

- Engine bboxes are **pixels of the OCR input image, top-left origin**.
  Scale Mistral coords by `image_size / mistral_dimensions` (this page:
  Mistral space 794×1018 @ 96 dpi; ocrmypdf rasterized the page at 3308×4240,
  ~400 dpi — observed in the `-k` kept work folder, `000001_ocr.png`).
- The fpdf2 renderer converts px→pt as `px * 72 / dpi`, where for the
  direct `generate_ocr` path **dpi comes from `pdfinfo[pageno].dpi`, not from
  `OcrElement.dpi`** (`ocrmypdf/_graft.py`, `Fpdf2ParsedPage`). The OCR image
  is rasterized at that same resolution, so the chain closes. Empirical
  spot-check: the "JUN" header landed within **0.81%** of its proportional
  expected position.
- `--redo-ocr` strips the old **invisible** text at graft time
  (`ocrmypdf/_graft.py` — "Strip old invisible text if redo mode is
  enabled") and masks only *visible* text regions out of the OCR image
  (irrelevant to us — we never look at the image).

### Tree shape the fpdf2 renderer wants

`PAGE` → (optionally `ocr_par`) → line elements (`ocr_line` et al.) → 
`ocrx_word` children with `text` + `bbox`. We emit one line per **text row**
of a Mistral block (table HTML flattened one line per `<tr>`, cell text
joined by spaces; multi-row blocks split evenly over the block height), and
word-split each row proportionally to character counts. Beware the
renderer's aspect-ratio suppression: tall-narrow "lines" (height > width)
whose text wouldn't plausibly fit are silently dropped (`renderer.py::
_check_aspect_ratio_plausible`, suppresses at ratio < 0.1) — per-row
splitting keeps every line wide and safe. `image`-type blocks are skipped;
the `markdown` field is ignored (blocks[] carry the real content, tables as
HTML where markdown has `[tbl-N.html]` placeholders).

## Verification results (12/12 PASS)

Command actually run (via `python -m ocrmypdf` inside the uv env):

```
ocrmypdf --redo-ocr --output-type pdfa --plugin mistral_plugin.py in.pdf out-redo.pdf
```

| Check | Result |
|---|---|
| Input pre-check: bad layer markers `San Jos0` / `Campay` present | PASS (both) |
| Output text contains `san jose water company` | PASS (the uppercase text block; also as 3 header blocks) |
| Output text contains `previous`, `1441`, `1459` (table content) | PASS |
| Output text free of `san jos0` / `campay` | PASS — bad layer fully replaced |
| Words carry positions | PASS — 427/427 words inside page rect |
| "JUN" proportional position spot-check | PASS — max deviation 0.81% of page |
| XMP declares PDF/A | PASS — `pdfaid:part=2`, `conformance=B` (ocrmypdf also logged "Output file is a PDF/A-2b (as expected)") |
| Page image untouched | PASS — same single JPEG, 2047×2624, **identical byte size 2,040,764** in and out (no rasterization, no recompression) |

Wall time: **6.8 s** (single page, cold ocrmypdf worker spawn included).

`--redo-ocr` combined with the plugin **without any conflict** — the
`--force-ocr` diagnostic and the strip+`--skip-text` fallback were not
needed. The fallback (PyMuPDF full-page redaction keeping images/line-art +
`--skip-text`) is implemented in `spike.py::strip_text_layer` and wired to
run automatically if the redo route ever fails.

## Surprises / notes that affect sidecar design

1. **No tesseract needed, but its plugin must be blocked *and* its options
   namespace re-registered** (items 3–4 above). This pair is mandatory
   boilerplate for any tesseract-less container image.
2. **Ghostscript is a hard dependency** for `--output-type pdfa`
   (`generate_pdfa` hook is implemented by the builtin ghostscript plugin).
   Also note: the builtin ghostscript plugin **refuses gs 10.0.0–10.02.0 in
   redo/skip modes** (known text-corruption bug) — container must ship
   ≥ 10.02.1. This spike used gs **10.07.1**.
3. **DPI subtlety:** for the direct tree path the renderer's px→pt dpi comes
   from pdfinfo, not from the tree — always express bboxes in the pixel
   space of the image ocrmypdf hands `generate_ocr()`; never assume the
   Mistral dpi.
4. **`--redo-ocr` incompatibilities:** rejects `--deskew`, `--clean-final`,
   `--remove-background` (core validation). Fine — we don't preprocess.
5. Harmless `[WinError 2]` log lines during postprocessing = ocrmypdf
   probing optional optimizers (jbig2, pngquant) that aren't installed;
   optimization still ran (ratio 1.00). Ship them in the container if we
   want smaller files.
6. **The one-worker pickle dance matters on Windows/spawn:** the plugin is
   re-imported by file path in worker processes and options must be
   picklable — we pass the JSON path via env var (`MISTRAL_OCR_JSON`), which
   is inherited by workers. In the Linux sidecar container this is equally
   safe. (A custom CLI arg would flow through `options.extra_attrs`; not
   verified across process spawn, hence env var for now.)
7. **Word boxes are synthesized** (even char-proportional split across each
   row). Good enough for search/highlight; if we ever need exact word
   geometry, Mistral would have to give word-level boxes.

## Environment used

- Windows 11, `uv 0.12.3`, Python 3.14.7 (uv-managed script env)
- `ocrmypdf 17.10.0`, `pymupdf 1.26.x`, `pikepdf` (transitive)
- Ghostscript **10.07.1** — installed during the spike via `scoop install
  ghostscript` (user-scoped; environment change on this machine worth
  knowing about). Production runs gs inside the sidecar container.
- Java absent on this machine → **veraPDF validation skipped locally**;
  veraPDF runs in the sidecar container in production. Local PDF/A evidence:
  XMP `pdfaid` assertions + Ghostscript's own conformance log line.
