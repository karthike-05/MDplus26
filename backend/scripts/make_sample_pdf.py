"""Generate a flat (non-fillable) PDF fixture from its schema.

The schema in `contracts/schemas/` is the source of truth for field geometry (§5c);
this script *derives* the blank form from it, so the PDF and schema can never drift.
Labels are derived from each field's `name`. Output goes to the schema's `source_ref`.

    python -m backend.scripts.make_sample_pdf                 # transport_intake (default)
    python -m backend.scripts.make_sample_pdf food_assistance # any form_id
    python -m backend.scripts.make_sample_pdf --all           # every schema
"""

from __future__ import annotations

import sys
from pathlib import Path

import fitz  # PyMuPDF

from contracts.models import FormSchema

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "contracts" / "schemas"

PAGE_W, PAGE_H = 612, 792  # US Letter, points

INK = (0.1, 0.12, 0.15)
LINE = (0.55, 0.58, 0.62)
HUMAN = (0.42, 0.27, 0.76)  # signature/consent rows stand out

TITLES = {
    "transport_intake": "Non-Emergency Medical Transport — Intake",
    "food_assistance": "Food Assistance — Referral & Enrollment",
}


def _label(name: str) -> str:
    return name.replace("_", " ").title()


def _load_schemas() -> dict[str, FormSchema]:
    out: dict[str, FormSchema] = {}
    for path in SCHEMA_DIR.glob("*.json"):
        s = FormSchema.model_validate_json(path.read_text())
        out[s.form_id] = s
    return out


def build(schema: FormSchema) -> Path:
    doc = fitz.open()
    pages: dict[int, fitz.Page] = {}

    def page_for(n: int) -> fitz.Page:
        if n not in pages:
            pages[n] = doc.new_page(width=PAGE_W, height=PAGE_H)
        return pages[n]

    p1 = page_for(1)
    p1.insert_text((50, 60), TITLES.get(schema.form_id, _label(schema.form_id)), fontsize=16, color=INK)
    p1.insert_text((50, 80), "Synthetic fixture — no real PHI", fontsize=9, color=LINE)

    for f in schema.fields:
        page = page_for(f.page or 1)
        x0, y0, x1, y1 = f.rect
        is_human = f.fill_policy == "human_only"
        color = HUMAN if is_human else LINE
        page.insert_text((50, y1 - 6), _label(f.name) + ("*" if f.required else ""), fontsize=10, color=INK)
        if is_human:
            page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=color, width=0.8)
        else:
            page.draw_line(fitz.Point(x0, y1), fitz.Point(x1, y1), color=color, width=0.7)

    out_path = ROOT / schema.source_ref
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    doc.close()
    return out_path


def main(argv: list[str]) -> None:
    schemas = _load_schemas()
    if argv and argv[0] == "--all":
        targets = list(schemas.values())
    else:
        form_id = argv[0] if argv else "transport_intake"
        if form_id not in schemas:
            raise SystemExit(f"no schema for form_id '{form_id}'. have: {sorted(schemas)}")
        targets = [schemas[form_id]]

    for schema in targets:
        path = build(schema)
        print(f"wrote {path.relative_to(ROOT)}  ({len(schema.fields)} fields)")


if __name__ == "__main__":
    main(sys.argv[1:])
