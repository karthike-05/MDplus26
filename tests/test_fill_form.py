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
from backend.tools.fill_form.fill_form import (
    prepare_from_records, build_sources, service_request_writeback,
)
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


# --- service_requests: the shared trip row form-fill reads and writes back ----
# Request-specific values (pickup/destination, requested date+time) live on the shared
# `service_requests` row that Voice reads too, instead of being duplicated onto the
# referral. Each field's own `source` declares the correspondence, used in both
# directions — so these tests pin the mapping, not a hand-maintained column list.

def test_writeback_maps_via_each_field_source():
    schema = FormSchema(
        form_id="f", target_type="pdf", source_ref="x.pdf",
        fields=[
            FormField(name="pickup_address", fill_policy="auto",
                      source="service_request.pickup_address"),
            FormField(name="client_name", fill_policy="auto", source="patient.name"),
        ],
    )
    out = service_request_writeback(schema, {"pickup_address": "12 Oak St", "client_name": "Ada"})
    assert out == {"pickup_address": "12 Oak St"}       # patient.* is not a writeback target


def test_writeback_skips_blanks_and_human_only():
    """A blank must never overwrite a stored value, and human_only fields are not ours
    to write at all (§2) — fillable_fields() already excludes them."""
    schema = FormSchema(
        form_id="f", target_type="pdf", source_ref="x.pdf",
        fields=[
            FormField(name="pickup_address", fill_policy="auto",
                      source="service_request.pickup_address"),
            FormField(name="requested_note", fill_policy="human_only",
                      source="service_request.request_notes"),
        ],
    )
    assert service_request_writeback(schema, {"pickup_address": "", "requested_note": "x"}) == {}


def test_prepare_sources_trip_values_from_the_service_request():
    db = MockReferralDB()
    payload = asyncio.run(prepare("ref_1002", db))
    sr = asyncio.run(db.get_service_request("ref_1002"))
    assert payload.values["pickup_address"] == sr["pickup_address"]
    assert payload.values["destination"] == sr["destination_address"]
    # provenance tells the review UI where each value came from
    assert payload.provenance["appointment_time"] == "service_request.requested_start_time"


def test_submit_writes_reviewed_values_back_to_the_service_request(tmp_path):
    db = MockReferralDB()
    payload = asyncio.run(prepare("ref_1002", db))
    values = dict(payload.values)
    values["pickup_address"] = "999 Corrected Ave, Austin TX"   # the reviewer fixes it

    outcome = asyncio.run(
        submit("ref_1002", values, db, attempt_id="att_sr",
               from_state="outreach_in_progress", out_path=tmp_path / "out.pdf")
    )
    assert outcome.status == "success"

    row = asyncio.run(db.get_service_request("ref_1002"))
    assert row["pickup_address"] == "999 Corrected Ave, Austin TX"
    # and a re-prepare reads the corrected row back (read-your-writes)
    assert asyncio.run(prepare("ref_1002", db)).values["pickup_address"] == \
        "999 Corrected Ave, Austin TX"


def test_failed_submit_does_not_touch_the_shared_row(tmp_path):
    """A failed injection must not leave the shared row claiming values that were
    never submitted — Voice reads that row."""
    db = MockReferralDB()
    payload = asyncio.run(prepare("ref_1002", db))
    values = dict(payload.values)
    values["pickup_address"] = "SHOULD NOT PERSIST"
    before = asyncio.run(db.get_service_request("ref_1002"))["pickup_address"]

    blocker = tmp_path / "blocker"      # a FILE where a directory would have to be,
    blocker.write_text("x")             # so creating the output path cannot succeed
    outcome = asyncio.run(
        submit("ref_1002", values, db, attempt_id="att_fail",
               from_state="outreach_in_progress", out_path=blocker / "out.pdf")
    )
    assert outcome.status == "failed"
    assert asyncio.run(db.get_service_request("ref_1002"))["pickup_address"] == before


# --- F2: the service_requests write-back must never undo a real submission ---

class _WritebackBoom(MockReferralDB):
    """A DB whose write-back fails the way a live `time` column does."""

    async def save_service_request(self, referral_id, fields):
        raise RuntimeError('invalid input syntax for type time: "2:45 PM"')


def test_a_failing_writeback_does_not_lose_the_submission(tmp_path):
    """The PDF is already written by the time the write-back runs. If this raises and
    escapes, the ToolOutcome is never recorded, the caller never closes the action,
    advance_referral's open-action guard freezes the referral, and the reviewer is told
    their submit failed — for a submission that really happened. Same shape as the
    save_call_outcome bug (changes-2026-07-31 §2)."""
    db = _WritebackBoom()
    payload = asyncio.run(prepare("ref_1002", db))
    out = tmp_path / "filled.pdf"

    outcome = asyncio.run(submit("ref_1002", dict(payload.values), db,
                                 attempt_id="att_wb", out_path=out))

    assert outcome.status == "success", "the injection happened; report it"
    assert out.exists()
    assert "writeback_failed" in outcome.data
    assert "type time" in outcome.data["writeback_failed"]
    assert db.attempts["att_wb"] is outcome        # still recorded (§8)


def test_writeback_normalises_through_each_field_format():
    """The PDF should carry what the reviewer typed ("2:45 PM" is what a human reads on
    a form); `service_requests.requested_start_time` is a Postgres `time` column and
    must get 14:45:00. The schema's own `format` is the single place that's declared."""
    db = MockReferralDB()
    schema = asyncio.run(db.get_form_schema("transport_intake"))

    writeback = service_request_writeback(
        schema, {"appointment_time": "2:45 PM", "pickup_address": "  12 Main St  "})

    assert writeback["requested_start_time"] == "14:45:00"
    assert writeback["pickup_address"] == "12 Main St"


def test_a_bad_time_is_caught_before_injection_not_at_the_writeback():
    """Prevention, not just containment. `appointment_time` had no `format`, so
    "quarter" passed validation (under an 8-char maxlength) and only failed at the DB —
    after the PDF was injected."""
    db = MockReferralDB()
    schema = asyncio.run(db.get_form_schema("transport_intake"))
    field = next(f for f in schema.fields if f.name == "appointment_time")

    assert validate_field(field, "2:45 PM") == []
    assert validate_field(field, "09:30:00") == []
    assert validate_field(field, "25:99") != []
    assert validate_field(field, "quarter") != []
