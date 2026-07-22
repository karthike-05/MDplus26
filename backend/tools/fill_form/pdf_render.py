"""PDF render helpers for the review UI (CLAUDE.md §6, §9).

The split-screen review screen needs, per form:
  - the rendered page as a PNG  -> ``render_page_png``
  - the page size in PDF points -> ``get_page_size``
so it can position `rect` overlay boxes as percentages (display-size independent).

Also the backbone of the visual coordinate-authoring loop (§9): fill -> render ->
eyeball -> nudge rect.
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF


def get_page_size(pdf_path: str | Path, page: int = 1) -> dict[str, float]:
    """Page ``{width, height}`` in PDF points (1-indexed page)."""
    with fitz.open(pdf_path) as doc:
        rect = doc[page - 1].rect
        return {"width": rect.width, "height": rect.height}


def render_page_png(
    pdf_path: str | Path,
    page: int = 1,
    zoom: float = 2.0,
    out_path: str | Path | None = None,
) -> bytes:
    """Render a page to PNG. Writes to ``out_path`` if given; always returns bytes.

    ``zoom`` is the render scale (2.0 ≈ 144 dpi) — crisp enough for the review UI
    without bloating the payload. Overlay math is scale-independent (percentages),
    so ``zoom`` never has to match anything on the frontend.
    """
    with fitz.open(pdf_path) as doc:
        pix = doc[page - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        data = pix.tobytes("png")
    if out_path is not None:
        Path(out_path).write_bytes(data)
    return data
