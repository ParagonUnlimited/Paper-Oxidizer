# SPDX-License-Identifier: MIT
"""OCRmyPDF v17 plugin: inject pre-computed Mistral OCR results (Spike A).

This plugin performs NO OCR. It loads a Mistral OCR ``*.raw.json`` file
(path supplied via the ``MISTRAL_OCR_JSON`` environment variable, which is
inherited by OCRmyPDF's spawned worker processes), scales the Mistral block
bounding boxes from Mistral pixel space into the coordinate space of the
rasterized page image that OCRmyPDF hands the engine, and returns the result
as an ``OcrElement`` tree through OCRmyPDF v17's modern structured OCR API:

    OcrEngine.supports_generate_ocr() -> True
    OcrEngine.generate_ocr(input_file, options, page_number)
        -> tuple[OcrElement, str]

Hook surface used (all verified against installed ocrmypdf 17.10.0 source):

* ``initialize(plugin_manager)`` -- blocks the builtin
  ``ocrmypdf.builtin_plugins.tesseract_ocr`` plugin so its unconditional
  ``check_options`` hook does not fail on machines without a tesseract
  binary (this is the pattern documented in ocrmypdf/pluginspec.py).
* ``register_options()`` -- re-registers the ``TesseractOptions`` model under
  the ``tesseract`` namespace because core code
  (ocrmypdf/_validation_coordinator.py) unconditionally reads
  ``options.tesseract.*``; importing the class does not require the binary.
* ``get_ocr_engine(options)`` -- firstresult hook. ``--plugin`` modules are
  registered after builtins and pluggy calls hooks LIFO, so this plugin wins.

Coordinate flow (verified in ocrmypdf source): the engine's bboxes are in
pixels of the OCR input image, top-left origin. The fpdf2 renderer converts
px -> pt with ``value * 72.0 / dpi`` where, for the direct ``generate_ocr``
path, dpi is taken from ``pdfinfo[pageno].dpi`` (ocrmypdf/_graft.py). The
rasterized OCR image is produced at that same resolution, so scaling Mistral
coordinates by (image_size / mistral_dimensions) lands text correctly.
"""

from __future__ import annotations

import json
import os
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

from ocrmypdf import hookimpl
from ocrmypdf.hocrtransform import BoundingBox, OcrClass, OcrElement
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence

ENV_VAR = 'MISTRAL_OCR_JSON'
SKIP_BLOCK_TYPES = {'image'}
CONFIDENCE = 0.95


# --------------------------------------------------------------------------
# Mistral JSON -> rows of text
# --------------------------------------------------------------------------


class _TableFlattener(HTMLParser):
    """Flatten simple <table><tr><td>...</td></tr></table> HTML to rows."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.rows.append([])
        elif tag in ('td', 'th'):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._cell is not None:
            text = ' '.join(''.join(self._cell).split())
            if self.rows:
                self.rows[-1].append(text)
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def flatten_table_html(html: str) -> list[str]:
    """Strip table tags; one output line per <tr>, cells joined by spaces."""
    parser = _TableFlattener()
    parser.feed(html)
    lines = []
    for row in parser.rows:
        line = ' '.join(cell for cell in row if cell).strip()
        if line:
            lines.append(line)
    return lines


def block_rows(block: dict) -> list[str]:
    """Text rows of one Mistral block (tables flattened, newlines split)."""
    content = block.get('content') or ''
    if block.get('type') == 'table':
        return flatten_table_html(content)
    rows = [' '.join(r.split()) for r in content.splitlines()]
    return [r for r in rows if r]


# --------------------------------------------------------------------------
# Rows -> OcrElement tree
# --------------------------------------------------------------------------


def _word_elements(
    row_text: str, left: float, top: float, right: float, bottom: float
) -> list[OcrElement]:
    """Divide a row bbox into word bboxes proportional to character counts.

    Mistral blocks are line/paragraph level; word-level boxes are synthesized
    by splitting the row width across words (spike-acceptable accuracy).
    """
    words = row_text.split()
    if not words:
        return []
    total_chars = sum(len(w) for w in words) + max(len(words) - 1, 0)
    width_per_char = (right - left) / max(total_chars, 1)
    elements = []
    cursor = left
    for word in words:
        w_width = max(len(word) * width_per_char, 0.5)
        w_right = min(cursor + w_width, right)
        w_right = max(w_right, cursor + 0.1)
        elements.append(
            OcrElement(
                ocr_class=OcrClass.WORD,
                bbox=BoundingBox(cursor, top, w_right, bottom),
                text=word,
                confidence=CONFIDENCE,
            )
        )
        cursor = w_right + width_per_char  # advance past inter-word space
    return elements


def build_page_tree(
    mistral_page: dict,
    img_w: float,
    img_h: float,
    dpi: float,
    page_number: int,
) -> tuple[OcrElement, str]:
    """Build the OcrElement PAGE tree and the plain text sidecar content."""
    dims = mistral_page['dimensions']
    sx = img_w / float(dims['width'])
    sy = img_h / float(dims['height'])

    lines: list[OcrElement] = []
    text_parts: list[str] = []
    for block in mistral_page.get('blocks', []):
        if block.get('type') in SKIP_BLOCK_TYPES:
            continue
        rows = block_rows(block)
        if not rows:
            continue
        left = max(0.0, min(block['top_left_x'] * sx, img_w - 1.0))
        top = max(0.0, min(block['top_left_y'] * sy, img_h - 1.0))
        right = max(left + 0.5, min(block['bottom_right_x'] * sx, img_w))
        bottom = max(top + 0.5, min(block['bottom_right_y'] * sy, img_h))
        row_h = (bottom - top) / len(rows)
        for i, row_text in enumerate(rows):
            r_top = top + i * row_h
            r_bottom = r_top + row_h
            words = _word_elements(row_text, left, r_top, right, r_bottom)
            if not words:
                continue
            lines.append(
                OcrElement(
                    ocr_class=OcrClass.LINE,
                    bbox=BoundingBox(left, r_top, right, r_bottom),
                    children=words,
                    confidence=CONFIDENCE,
                )
            )
            text_parts.append(row_text)

    page = OcrElement(
        ocr_class=OcrClass.PAGE,
        bbox=BoundingBox(left=0, top=0, right=img_w, bottom=img_h),
        dpi=float(dpi),
        page_number=page_number,
        children=lines,
    )
    return page, '\n'.join(text_parts)


def _load_mistral_page(page_number: int) -> dict:
    json_path = os.environ.get(ENV_VAR)
    if not json_path:
        raise RuntimeError(
            f'{ENV_VAR} environment variable must point at a Mistral raw.json'
        )
    data = json.loads(Path(json_path).read_text(encoding='utf-8'))
    for page in data['pages']:
        if page.get('index', 0) == page_number:
            return page
    raise RuntimeError(f'No page with index {page_number} in {json_path}')


# --------------------------------------------------------------------------
# OcrEngine implementation
# --------------------------------------------------------------------------


class MistralJsonOcrEngine(OcrEngine):
    """OCR 'engine' that replays pre-computed Mistral OCR results."""

    @staticmethod
    def version() -> str:
        return '0.1.0-spike'

    @staticmethod
    def creator_tag(options) -> str:
        return 'Paper-Oxidizer Mistral OCR replay 0.1.0-spike'

    def __str__(self) -> str:
        return 'Mistral OCR JSON replay (Paper-Oxidizer spike)'

    @staticmethod
    def languages(options) -> set[str]:
        # Accept whatever was requested; language checks are irrelevant here.
        requested = set(getattr(options, 'languages', None) or [])
        return requested | {'eng'}

    @staticmethod
    def get_orientation(input_file: Path, options) -> OrientationConfidence:
        return OrientationConfidence(angle=0, confidence=0.0)

    @staticmethod
    def get_deskew(input_file: Path, options) -> float:
        return 0.0

    @staticmethod
    def supports_generate_ocr() -> bool:
        return True

    @staticmethod
    def generate_ocr(
        input_file: Path, options, page_number: int = 0
    ) -> tuple[OcrElement, str]:
        with Image.open(input_file) as img:
            img_w, img_h = img.size
            dpi_info = img.info.get('dpi', (72, 72))
            dpi = dpi_info[0] if isinstance(dpi_info, tuple) else dpi_info
        mistral_page = _load_mistral_page(page_number)
        return build_page_tree(
            mistral_page, float(img_w), float(img_h), float(dpi), page_number
        )

    @staticmethod
    def generate_hocr(
        input_file: Path, output_hocr: Path, output_text: Path, options
    ) -> None:
        """Fallback surface: synthesize hOCR XML from the same block data.

        Never called by the v17 pipeline while supports_generate_ocr() is
        True; kept as the documented alternative for engines that cannot
        return OcrElement trees (hocrtransform's parser does the rest).
        """
        page_number = 0  # hOCR path has no page context; spike is 1 page
        tree, text = MistralJsonOcrEngine.generate_ocr(
            input_file, options, page_number
        )
        assert tree.bbox is not None

        def esc(s: str) -> str:
            return (
                s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            )

        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            '<title>Mistral OCR replay</title>\n'
            "<meta name='ocr-system' content='mistral-replay'/>\n"
            '</head>\n<body>\n'
            f"<div class='ocr_page' title='bbox 0 0 "
            f"{tree.bbox.width:.0f} {tree.bbox.height:.0f}'>\n"
        ]
        for line in tree.lines:
            b = line.bbox
            parts.append(
                f"<span class='ocr_line' title='bbox {b.left:.0f} {b.top:.0f} "
                f"{b.right:.0f} {b.bottom:.0f}'>"
            )
            for word in line.words:
                wb = word.bbox
                parts.append(
                    f"<span class='ocrx_word' title='bbox {wb.left:.0f} "
                    f"{wb.top:.0f} {wb.right:.0f} {wb.bottom:.0f}'>"
                    f"{esc(word.text)}</span> "
                )
            parts.append('</span>\n')
        parts.append('</div>\n</body>\n</html>\n')
        output_hocr.write_text(''.join(parts), encoding='utf-8')
        output_text.write_text(text, encoding='utf-8')

    @staticmethod
    def generate_pdf(
        input_file: Path, output_pdf: Path, output_text: Path, options
    ) -> None:
        raise NotImplementedError(
            'MistralJsonOcrEngine does not support the sandwich renderer; '
            'use the default fpdf2 renderer.'
        )


# --------------------------------------------------------------------------
# Hook implementations
# --------------------------------------------------------------------------


@hookimpl
def initialize(plugin_manager):
    """Block the builtin tesseract plugin (its check_options requires the
    tesseract binary even when another engine is selected)."""
    plugin_manager.set_blocked('ocrmypdf.builtin_plugins.tesseract_ocr')


@hookimpl
def register_options():
    """Keep the 'tesseract' options namespace alive.

    ocrmypdf/_validation_coordinator.py reads options.tesseract.* even when
    tesseract is not the OCR engine; blocking the builtin plugin removes its
    register_options, so re-register the same (import-safe) model here.
    """
    from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOptions

    return {'tesseract': TesseractOptions}


@hookimpl
def get_ocr_engine(options):
    return MistralJsonOcrEngine()
