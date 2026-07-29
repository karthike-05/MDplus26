"""Intake front-door tests (CLAUDE.md §12 "pick patient" beat). No DB/browser.

Covers the seam the find-patient page depends on: identity match (auto-populate),
create-when-absent, and referral creation starting at `created` so the scheduler
drives it. Also pins instance isolation — create_* must not leak into the shared
fixtures.
"""

from __future__ import annotations

import asyncio

from backend.db.mock import MockReferralDB


def test_find_patient_matches_on_normalized_name_and_dob():
    db = MockReferralDB()
    # pat_001 is "Maria Gonzalez", dob "03/12/1958" (non-ISO in the fixture).
    p = asyncio.run(db.find_patient("  maria   GONZALEZ ", "1958-03-12"))
    assert p is not None and p["id"] == "pat_001"

    # Same person, dob typed in the fixture's own non-ISO shape -> still matches.
    p2 = asyncio.run(db.find_patient("Maria Gonzalez", "03/12/1958"))
    assert p2 is not None and p2["id"] == "pat_001"


def test_find_patient_no_match_returns_none():
    db = MockReferralDB()
    assert asyncio.run(db.find_patient("Nobody Here", "2000-01-01")) is None
    # Right name, wrong dob -> not a match (identity is name AND dob).
    assert asyncio.run(db.find_patient("Maria Gonzalez", "1990-01-01")) is None


def test_create_patient_then_find_it():
    db = MockReferralDB()
    pid = asyncio.run(db.create_patient({"name": "Ada Lovelace", "dob": "1815-12-10"}))
    assert pid.startswith("pat_")
    found = asyncio.run(db.find_patient("ada lovelace", "1815-12-10"))
    assert found is not None and found["id"] == pid


def test_create_referral_starts_at_created():
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_002", "transport_intake", service_name="Drive A Senior ATX"))
    ref = asyncio.run(db.get_referral(rid))
    assert ref["current_state"] == "created"
    assert ref["patient_id"] == "pat_002" and ref["service_name"] == "Drive A Senior ATX"


def test_instances_are_isolated():
    """create_* on one instance must not bleed into another (§ shared-fixture guard)."""
    db1 = MockReferralDB()
    asyncio.run(db1.create_patient({"id": "pat_ghost", "name": "Ghost", "dob": "2000-01-01"}))
    db2 = MockReferralDB()
    assert asyncio.run(db2.find_patient("Ghost", "2000-01-01")) is None


def test_new_patient_requires_the_not_null_columns():
    """`patients.phone` and `patients.referring_clinic_name` are NOT NULL with no
    default in the shared schema, so the API must reject a payload missing either
    rather than letting Postgres refuse the insert as an opaque 500."""
    from pydantic import ValidationError
    from backend.main import NewPatient

    ok = NewPatient(name="Ada", dob="1815-12-10", phone="5125550000",
                    referring_clinic="CommUnityCare Hancock", address="1 Main St")
    assert ok.phone and ok.referring_clinic

    for missing in ({"phone": "5125550000"}, {"referring_clinic": "X"}):
        try:
            NewPatient(name="Ada", dob="1815-12-10", address="1 Main St", **missing)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"expected rejection when omitting one of {missing}")


def test_live_intake_guard_defaults_off_and_withholds_the_advance(monkeypatch):
    """The app has no auth. On a permanent public URL, "+ New referral" would let anyone
    type a phone number and cause a REAL WhatsApp on the team's Twilio account, because
    create_referral kicks advance_referral -> confirm_consent -> twilio -> Messaging's
    deployed poller. The guard withholds only that kick; the rows are still written.

    Defaults OFF so a fresh deploy is safe without remembering to set anything.
    """
    from backend.main import allow_live_intake

    monkeypatch.delenv("ALLOW_LIVE_INTAKE", raising=False)
    assert allow_live_intake() is False          # safe by default
    monkeypatch.setenv("ALLOW_LIVE_INTAKE", "1")
    assert allow_live_intake() is True
    monkeypatch.setenv("ALLOW_LIVE_INTAKE", "0")
    assert allow_live_intake() is False


def test_appointment_date_accepts_both_formats_and_rejects_garbage():
    """`service_requests.requested_date` is a DATE column (same failure mode as patient
    DOB, CLAUDE.md §7d-adjacent bug) — validate at the edge so a typo 422s instead of
    surfacing Postgres's raw `invalid input syntax for type date` as a bare 500."""
    from pydantic import ValidationError
    from backend.main import NewReferral

    iso = NewReferral(patient_id="pat_001", appointment_date="2026-08-05")
    us = NewReferral(patient_id="pat_001", appointment_date="08/05/2026")
    assert iso.appointment_date == us.appointment_date == "2026-08-05"

    assert NewReferral(patient_id="pat_001").appointment_date is None  # optional

    try:
        NewReferral(patient_id="pat_001", appointment_date="not a date")
    except ValidationError:
        pass
    else:
        raise AssertionError("expected rejection of an unparseable appointment_date")


def test_create_referral_writes_pickup_address_to_service_requests():
    """B13: nothing created a `service_requests` row, so `transport_intake`'s
    pickup_address/home_address fields (both sourced from service_request.* now — see
    contracts/schemas/transport_intake_pdf.json) rendered blank on every UI-created
    referral. `POST /api/referrals` must thread the collected address through rather
    than dropping it (REFERRAL_COLS maps it to nothing on `referrals` itself)."""
    from fastapi.testclient import TestClient

    from backend.main import app, db

    client = TestClient(app)
    resp = client.post("/api/referrals", json={
        "patient_id": "pat_001",
        "pickup_address": "123 Main St, Austin, TX",
        "appointment_date": "08/05/2026",
    })
    assert resp.status_code == 200
    referral_id = resp.json()["referral_id"]

    request = asyncio.run(db.get_service_request(referral_id))
    assert request["pickup_address"] == "123 Main St, Austin, TX"
    assert request["requested_date"] == "2026-08-05"


def test_create_referral_with_no_trip_details_skips_the_service_request_write(monkeypatch):
    """The common case for an existing patient found by name+DOB, where intake never
    asked for an address — nothing should be written on their behalf. Asserted against
    `save_service_request` directly (a spy), not `get_service_request`'s return value:
    the mock derives a plausible-looking service_request from fixture patient data
    (CLAUDE.md §9 — this exact derivation is what made B13 invisible offline), so an
    empty-dict assertion there would pass for the wrong reason."""
    from fastapi.testclient import TestClient

    from backend.main import app, db

    calls = []
    real_save = db.save_service_request

    async def spy(referral_id, fields):
        calls.append((referral_id, fields))
        return await real_save(referral_id, fields)

    monkeypatch.setattr(db, "save_service_request", spy)

    client = TestClient(app)
    resp = client.post("/api/referrals", json={"patient_id": "pat_002"})
    assert resp.status_code == 200
    assert calls == []
