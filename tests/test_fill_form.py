"""Layered fill_form suite (CLAUDE.md §9).

L1 (here): mapping + validation + prepare — pure, no DB/browser, runs in ms. This is
the correctness core (G2). L2 (injector -> real PDF) and L3 (Playwright) come with
submit(). Run: ``pytest -q``.
"""

from __future__ import annotations

import asyncio

import fitz  # PyMuPDF — L2 text-extraction assertion

from contracts.models import FormField, FormSchema
from backend.mapping import mapper
from backend.tools.fill_form.validation import validate_field
from backend.tools.fill_form.fill_form import prepare_from_records, build_sources
from backend.db.mock import MockReferralDB
from backend.tools.fill_form.fill_form import prepare, submit


# --- L1: mapper normalization ------------------------------------------------

def test_normalize_date_variants():
    assert mapper.normalize("03/12/1958", "date") == "1958-03-12"
    assert mapper.normalize("1958-03-12", "date") == "1958-03-12"


def test_normalize_phone():
    assert mapper.normalize("5127654321", "phone") == "(512) 765-4321"
    assert mapper.normalize("+1 512 765 4321", "phone") == "(512) 765-4321"


def test_map_field_missing_source_is_none():
    f = FormField(name="x", source="referral.appointment_time")
    assert mapper.map_field(f, {"referral": {"appointment_time": ""}}) is None


# --- L1: validation ----------------------------------------------------------

def test_required_missing_flags():
    f = FormField(name="t", required=True)
    assert "required" in validate_field(f, None)


def test_bad_date_format_flags():
    f = FormField(name="d", format="date")
    assert validate_field(f, "08/05/2026")            # not ISO -> error
    assert validate_field(f, "2026-08-05") == []      # ISO -> clean


def test_maxlength_flags():
    f = FormField(name="m", maxlength=3)
    assert validate_field(f, "abcd")
    assert validate_field(f, "abc") == []


# --- L1: prepare (human_only never auto-filled; missing required flagged) -----

def _schema():
    return FormSchema(
        form_id="t", target_type="pdf", source_ref="t.pdf",
        fields=[
            FormField(name="name", fill_policy="auto", source="patient.name", required=True),
            FormField(name="time", fill_policy="review", source="referral.time", required=True),
            FormField(name="sig", fill_policy="human_only", required=True),
        ],
    )


def test_prepare_holds_human_only_and_flags_missing():
    sources = build_sources({"name": "Ada"}, {"time": ""})
    p = prepare_from_records("ref_x", _schema(), sources)
    assert p.values["name"] == "Ada"
    assert "sig" not in p.values and "sig" in p.pending_human   # §2: never auto-filled
    assert p.needs_attention == ["time"]                        # required + missing


# --- L1: prepare via the DB seam (mock) --------------------------------------

def test_prepare_clean_referral_via_mock():
    p = asyncio.run(prepare("ref_1002", MockReferralDB()))
    assert p.needs_attention == []                              # ref_1002 fills clean
    assert p.pending_human == ["client_signature", "date_signed"]
    assert p.values["date_of_birth"] == "1971-11-02"


# --- L2: submit -> real PDF, asserted by extracting the text layer (§9) -------

def test_submit_writes_pdf_and_records_outcome(tmp_path):
    db = MockReferralDB()
    payload = asyncio.run(prepare("ref_1002", db))              # fills clean
    out = tmp_path / "filled.pdf"
    outcome = asyncio.run(
        submit("ref_1002", dict(payload.values), db,
               attempt_id="att_test", from_state="outreach_in_progress", out_path=out)
    )

    assert outcome.status == "success"
    assert db.attempts["att_test"] is outcome                  # recorded (idempotency key)

    doc = fitz.open(out)
    text = "".join(page.get_text() for page in doc)
    doc.close()
    assert "James Whitfield" in text                           # auto-mapped
    assert "1971-11-02" in text                                # normalized date landed
    # human_only never injected (§2)
    assert outcome.data["left_blank"] == ["client_signature", "date_signed"]
    assert "client_signature" not in outcome.data["filled_fields"]


def test_submit_missing_required_is_needs_human_no_injection(tmp_path):
    db = MockReferralDB()
    payload = asyncio.run(prepare("ref_1001", db))             # appointment_time missing
    out = tmp_path / "should_not_exist.pdf"
    outcome = asyncio.run(
        submit("ref_1001", dict(payload.values), db,
               attempt_id="att_r1001", from_state="outreach_in_progress", out_path=out)
    )

    assert outcome.status == "needs_human"                    # validation gate before inject
    assert "appointment_time" in outcome.data["problems"]
    assert not out.exists()                                    # nothing injected
