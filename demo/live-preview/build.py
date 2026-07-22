"""Self-contained renderer for the offline live preview.

Standalone on purpose (no repo imports) so the demo runs from anywhere, even if a
sandbox blocks the project package path. Mirrors backend/scripts/make_sample_pdf.py
+ pdf_render.py. Writes page.png next to this script.

    python3 build.py
"""

import os

import fitz  # PyMuPDF

HERE = os.path.dirname(os.path.abspath(__file__))

PAGE_W, PAGE_H = 612, 792
INK = (0.1, 0.12, 0.15)
LINE = (0.55, 0.58, 0.62)
HUMAN = (0.42, 0.27, 0.76)

FIELDS = [
    ("Client Name*", (205, 120, 562, 142), False),
    ("Date Of Birth*", (205, 164, 562, 186), False),
    ("Phone*", (205, 208, 562, 230), False),
    ("Home Address*", (205, 252, 562, 274), False),
    ("Medicaid Id*", (205, 296, 562, 318), False),
    ("Appointment Date*", (205, 340, 562, 362), False),
    ("Appointment Time*", (205, 384, 562, 406), False),
    ("Pickup Address*", (205, 428, 562, 450), False),
    ("Destination*", (205, 472, 562, 494), False),
    ("Mobility Needs", (205, 516, 562, 538), False),
    ("Referring Clinic*", (205, 560, 562, 582), False),
    ("Client Signature*", (205, 620, 562, 650), True),
    ("Date Signed*", (205, 664, 562, 686), True),
]

doc = fitz.open()
page = doc.new_page(width=PAGE_W, height=PAGE_H)
page.insert_text((50, 60), "Non-Emergency Medical Transport - Intake", fontsize=16, color=INK)
page.insert_text((50, 80), "Synthetic fixture - no real PHI", fontsize=9, color=LINE)
for label, (x0, y0, x1, y1), human in FIELDS:
    page.insert_text((50, y1 - 6), label, fontsize=10, color=INK)
    if human:
        page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=HUMAN, width=0.8)
    else:
        page.draw_line(fitz.Point(x0, y1), fitz.Point(x1, y1), color=LINE, width=0.7)

pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
out = os.path.join(HERE, "page.png")
pix.save(out)
print("wrote", out, pix.width, "x", pix.height)
