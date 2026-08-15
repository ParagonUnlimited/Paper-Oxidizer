# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "ocrmypdf>=17,<18",
#   "pymupdf>=1.24",
# ]
# ///
"""Spike A driver: prove the OCRmyPDF v17 plugin path end to end.

Runs the real ocrmypdf CLI (python -m ocrmypdf) with mistral_plugin.py in
--redo-ocr --output-type pdfa mode against the scanned San Jose Water bill,
then verifies the output with PyMuPDF. If the redo route fails to remove the
bad Genius Scan text layer, falls back to the production route: strip the
text layer with PyMuPDF, then ocrmypdf --skip-text.

Usage:  uv run spike.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pymupdf as fitz

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / 'out'
PLUGIN = HERE / 'mistral_plugin.py'

INPUT_PDF = Path(
    r'C:\Users\busin\Documents\Document Splitting for Paperless\recut'
    r'\0000__San Jose Water bill 8364120000-7 2021-06-25.pdf'
)
MISTRAL_JSON = Path(
    r'C:\Users\busin\Documents\Document Splitting for Paperless\ocr-mistral'
    r'\0000__San Jose Water bill 8364120000-7 2021-06-25.raw.json'
)

MUST_CONTAIN = ['san jose water company', 'previous', '1441', '1459']
MUST_NOT_CONTAIN = ['san jos0', 'campay']

# "JUN" header block in Mistral 794x1018 space (from the raw.json)
JUN_BLOCK = dict(left=14, top=26, right=108, bottom=71)
MISTRAL_W, MISTRAL_H = 794.0, 1018.0
POSITION_TOLERANCE = 0.03  # 3% of the page dimension

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f' -- {detail}' if detail else ''))
    return ok


def norm(text: str) -> str:
    return ' '.join(text.split()).lower()


def image_inventory(pdf_path: Path) -> list[dict]:
    """(width, height, byte size) of every image on page 1."""
    inventory = []
    with fitz.open(pdf_path) as doc:
        page = doc[0]
        for entry in page.get_images(full=True):
            xref = entry[0]
            info = doc.extract_image(xref)
            inventory.append(
                dict(
                    xref=xref,
                    width=info['width'],
                    height=info['height'],
                    bytes=len(info['image']),
                    ext=info['ext'],
                )
            )
    return inventory


def run_ocrmypdf(args: list[str]) -> tuple[int, str, float]:
    cmd = [sys.executable, '-m', 'ocrmypdf', *args]
    env = dict(os.environ)
    env['MISTRAL_OCR_JSON'] = str(MISTRAL_JSON)
    print(f"\n$ ocrmypdf {' '.join(args)}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        encoding='utf-8',
        errors='replace',
        env=env,
    )
    wall = time.perf_counter() - t0
    print(f"  exit={proc.returncode}  wall={wall:.1f}s")
    if proc.stdout.strip():
        print('  --- stdout ---')
        print('  ' + proc.stdout.strip().replace('\n', '\n  '))
    if proc.stderr.strip():
        print('  --- stderr ---')
        print('  ' + proc.stderr.strip().replace('\n', '\n  '))
    return proc.returncode, proc.stdout + proc.stderr, wall


def strip_text_layer(src: Path, dst: Path) -> None:
    """Production-fallback text strip: redact all text, keep images/lineart."""
    with fitz.open(src) as doc:
        for page in doc:
            page.add_redact_annot(page.rect)
            try:
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                )
            except (AttributeError, TypeError):
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        doc.save(dst)


def verify_output(out_pdf: Path, in_inventory: list[dict], label: str) -> bool:
    print(f'\n=== Verifying {label}: {out_pdf.name} ===')
    all_ok = True
    with fitz.open(out_pdf) as doc:
        page = doc[0]
        text = page.get_text()
        ntext = norm(text)

        for needle in MUST_CONTAIN:
            all_ok &= check(f'text contains {needle!r}', needle in ntext)
        for needle in MUST_NOT_CONTAIN:
            all_ok &= check(f'text does NOT contain {needle!r}', needle not in ntext)

        # (b) words carry positions inside the page
        words = page.get_text('words')
        rect = page.rect
        pad = 2.0
        inside = [
            w
            for w in words
            if w[0] >= rect.x0 - pad
            and w[1] >= rect.y0 - pad
            and w[2] <= rect.x1 + pad
            and w[3] <= rect.y1 + pad
        ]
        all_ok &= check(
            'words have positions inside page',
            len(words) > 50 and len(inside) == len(words),
            f'{len(inside)}/{len(words)} words inside {rect}',
        )

        # Proportional position spot-check on the "JUN" block: proves the
        # whole Mistral px -> image px -> pt -> graft coordinate chain.
        expected_x0 = JUN_BLOCK['left'] / MISTRAL_W * rect.width
        expected_y0 = JUN_BLOCK['top'] / MISTRAL_H * rect.height
        jun_words = [w for w in words if w[4].strip().lower() == 'jun']
        best = None
        for w in jun_words:
            dx = abs(w[0] - expected_x0) / rect.width
            dy = abs(w[1] - expected_y0) / rect.height
            dev = max(dx, dy)
            if best is None or dev < best[0]:
                best = (dev, w)
        if best is None:
            all_ok &= check('JUN position spot-check', False, 'no JUN word found')
        else:
            dev, w = best
            all_ok &= check(
                'JUN position spot-check',
                dev <= POSITION_TOLERANCE,
                f'expected ({expected_x0:.1f},{expected_y0:.1f}) '
                f'got ({w[0]:.1f},{w[1]:.1f}) max-dev={dev * 100:.2f}% of page',
            )

        # (c) PDF/A declaration in XMP
        xmp = doc.get_xml_metadata()
        pdfa = 'pdfaid:part' in xmp
        detail = ''
        if pdfa:
            import re

            m = re.search(
                r'pdfaid:part(?:>|=")\s*(\d+)', xmp
            )
            m2 = re.search(r'pdfaid:conformance(?:>|=")\s*(\w+)', xmp)
            detail = (
                f"part={m.group(1) if m else '?'} "
                f"conformance={m2.group(1) if m2 else '?'}"
            )
        all_ok &= check('XMP declares pdfaid:part', pdfa, detail)

        # (d) page image not rasterized/replaced: same count + pixel dims
        out_inventory = image_inventory(out_pdf)
        same_count = len(out_inventory) == len(in_inventory)
        same_dims = same_count and all(
            (i['width'], i['height']) == (o['width'], o['height'])
            for i, o in zip(in_inventory, out_inventory)
        )
        in_dims = [(i['width'], i['height'], i['bytes'], i['ext']) for i in in_inventory]
        out_dims = [(o['width'], o['height'], o['bytes'], o['ext']) for o in out_inventory]
        all_ok &= check(
            'embedded images preserved (count + pixel dims)',
            same_dims,
            f'in={in_dims} out={out_dims}',
        )
    return all_ok


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    print(f'python {sys.version.split()[0]}')
    import ocrmypdf

    print(f'ocrmypdf {ocrmypdf.__version__}')

    # --- Pre-check: input really carries the bad Genius Scan layer ---
    print('\n=== Input pre-check ===')
    with fitz.open(INPUT_PDF) as doc:
        in_text = norm(doc[0].get_text())
    check("input has bad layer marker 'san jos0'", 'san jos0' in in_text)
    check("input has bad layer marker 'campay'", 'campay' in in_text)
    in_inventory = image_inventory(INPUT_PDF)
    print(f'  input images: {in_inventory}')

    # --- Primary route: --redo-ocr ---
    out_redo = OUT_DIR / 'out-redo.pdf'
    rc, _log, wall_redo = run_ocrmypdf(
        [
            '--redo-ocr',
            '--output-type',
            'pdfa',
            '--plugin',
            str(PLUGIN),
            str(INPUT_PDF),
            str(out_redo),
        ]
    )
    redo_ok = False
    if rc == 0 and out_redo.exists():
        redo_ok = verify_output(out_redo, in_inventory, 'redo-ocr route')
    else:
        check('redo-ocr run exited 0', False, f'exit={rc}')

    # --- Fallback route (production plan): pymupdf strip + --skip-text ---
    fallback_ok = None
    wall_fallback = None
    if not redo_ok:
        print('\n=== redo-ocr route failed; trying strip + --skip-text ===')
        stripped = OUT_DIR / 'stripped.pdf'
        strip_text_layer(INPUT_PDF, stripped)
        out_skip = OUT_DIR / 'out-skiptext.pdf'
        rc2, _log2, wall_fallback = run_ocrmypdf(
            [
                '--skip-text',
                '--output-type',
                'pdfa',
                '--plugin',
                str(PLUGIN),
                str(stripped),
                str(out_skip),
            ]
        )
        if rc2 == 0 and out_skip.exists():
            fallback_ok = verify_output(
                out_skip, image_inventory(stripped), 'strip + skip-text route'
            )
        else:
            check('skip-text run exited 0', False, f'exit={rc2}')
            fallback_ok = False

    # --- Summary ---
    print('\n=== SUMMARY ===')
    fails = [r for r in results if not r[1]]
    print(f'checks: {len(results) - len(fails)} passed, {len(fails)} failed')
    print(f'redo-ocr route: {"PROVEN" if redo_ok else "FAILED"} '
          f'(wall {wall_redo:.1f}s)')
    if fallback_ok is not None:
        print(f'strip+skip-text route: {"PROVEN" if fallback_ok else "FAILED"}'
              + (f' (wall {wall_fallback:.1f}s)' if wall_fallback else ''))
    return 0 if (redo_ok or fallback_ok) else 1


if __name__ == '__main__':
    sys.exit(main())
