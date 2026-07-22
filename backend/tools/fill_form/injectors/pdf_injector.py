"""PdfInjector — overlays text at each field's rect (PyMuPDF).

Flat digital PDFs and scanned PDFs fill identically once a rect is verified (§6).
Single-line fields use a baseline ``insert_text`` (not ``insert_textbox``, which
silently drops text in short boxes — §9).
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF

from contracts.models import FormSchema

REPO_ROOT = Path(__file__).resolve().parents[4]

_FONT_SIZE = 11
_INK = (0, 0, 0)


class PdfInjector:
    async def inject(self, schema: FormSchema, values: dict, *, out_path: str | Path | None = None, **_) -> dict:
        src = REPO_ROOT / schema.source_ref
        doc = fitz.open(src)

        filled: list[str] = []
        for field in schema.fillable_fields():  # NEVER human_only (§2)
            value = values.get(field.name)
            if value in (None, ""):
                continue
            if not field.rect:
                continue
            page = doc[(field.page or 1) - 1]
            x0, _y0, _x1, y1 = field.rect
            page.insert_text((x0 + 2, y1 - 6), str(value), fontsize=_FONT_SIZE, color=_INK)
            filled.append(field.name)

        out = Path(out_path) if out_path else REPO_ROOT / "sample_forms" / "filled" / f"{schema.form_id}.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        doc.save(out)
        doc.close()

        return {
            "target": "pdf",
            "output_path": str(out),
            "filled_fields": filled,
            "left_blank": [f.name for f in schema.fields if f.fill_policy == "human_only"],
        }
