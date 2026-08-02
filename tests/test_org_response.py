"""The org-acceptance seam — MILESTONE 1 (§7), and the reason the loop couldn't close.

`advance_referral` (001_orchestration_bus.sql:81) promotes a referral to
`status='enrolled'` if and only if an `attempts` row carries `outcome='enrolled'`:

    if exists(select 1 from attempts where referral_id=r.id and outcome='enrolled')

Nothing ever wrote it. Our own successful submit records `outcome='submitted'`, which is
*correct* — submitting a form is not the org accepting, and collapsing the two would
destroy the distinction the product is built on. So live, a referral could reach
`submitted` and never reach `enrolled` -> never `completed`.

L1: mock DB, stub tools, no network.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.adapters.inbound import ORG_DECISION_OUTCOME, build_router
from backend.db.mock import MockReferralDB
from backend.orchestrator import state_machine as sm

# The live `attempts.outcome` CHECK, read off the deployed schema 2026-07-28. Writing a
# value outside it fails the insert — and only on the real DB, never in these tests.
LIVE_OUTCOME_VOCAB = {
    "no_response", "responded", "information_collected", "submitted", "accepted",
    "rejected", "scheduled", "enrolled", "completed", "patient_declined",
    "needs_human_followup", "technical_failure", "ineligible",
}


@pytest.fixture
def client_and_db():
    db = MockReferralDB()
    app = FastAPI()
    app.include_router(build_router(db, {}))
    return TestClient(app), db


def _at_submitted(db, referral_id="ref_1001"):
    """A referral waiting on the org's answer — the only state this seam applies from."""
    asyncio.run(db.set_state(referral_id, sm.SUBMITTED))
    return referral_id


def test_every_decision_maps_into_the_live_outcome_vocabulary():
    """A value outside the CHECK fails the insert on the real DB and nowhere else, so
    this is the only place the mismatch can be caught before a live run."""
    assert set(ORG_DECISION_OUTCOME.values()) <= LIVE_OUTCOME_VOCAB


def test_acceptance_writes_the_enrolled_attempt_that_advance_referral_reads(client_and_db):
    """The whole point. Without this row, `advance_referral` never promotes the referral
    and the loop stops at `submitted` forever."""
    client, db = client_and_db
    rid = _at_submitted(db)

    response = client.post("/api/org/response", json={
        "referral_id": rid, "decision": "accepted", "confirmation_id": "ORG-4417"})
    assert response.status_code == 200
    assert response.json()["outcome"] == "enrolled"

    enrolled = [a for a in db.shared_attempts if a.get("outcome") == "enrolled"]
    assert len(enrolled) == 1
    row = enrolled[0]
    assert row["referral_id"] == rid
    assert row["direction"] == "inbound"        # the ORG answered us
    assert row["status"] == "completed"
    assert row["structured_result"]["confirmation_id"] == "ORG-4417"


def test_acceptance_advances_submitted_to_confirmed_offline(client_and_db):
    """Offline our scheduler owns transitions (§7a): (submitted, success) -> confirmed.
    That's milestone 1 — the ORG said yes, distinct from the patient having used it."""
    client, db = client_and_db
    rid = _at_submitted(db)

    client.post("/api/org/response", json={"referral_id": rid, "decision": "accepted"})
    assert asyncio.run(db.get_referral(rid))["current_state"] != sm.SUBMITTED


def test_rejection_is_not_recorded_as_enrolled(client_and_db):
    """A rejection that wrote `enrolled` would mark the referral successfully placed at
    an org that just said no — the worst possible silent failure here."""
    client, db = client_and_db
    rid = _at_submitted(db)

    client.post("/api/org/response", json={"referral_id": rid, "decision": "rejected"})
    outcomes = [a.get("outcome") for a in db.shared_attempts]
    assert "rejected" in outcomes
    assert "enrolled" not in outcomes


def test_submitting_a_form_is_not_the_org_accepting():
    """Guards the distinction directly: our own submit must never produce `enrolled`.
    Collapsing milestone 1 into "we sent the form" is exactly what the competitors do."""
    from backend.orchestrator.actions import STATUS_TO_THEIRS

    assert "enrolled" not in {outcome for _, outcome in STATUS_TO_THEIRS.values()}
    assert STATUS_TO_THEIRS["success"] == ("sent", "submitted")


def test_unknown_decision_is_422_and_still_logged(client_and_db):
    """An org reply we can't classify is precisely the event worth a durable trace —
    it's silent from the sender's side otherwise (A12)."""
    client, db = client_and_db
    rid = _at_submitted(db)

    response = client.post("/api/org/response",
                           json={"referral_id": rid, "decision": "maybe?"})
    assert response.status_code == 422
    assert not db.shared_attempts, "nothing should be recorded for an unusable decision"
    assert any(e.get("processing_status") == "failed"
               for e in db.integration_events)


def test_unknown_referral_is_404_and_logged_detached(client_and_db):
    client, db = client_and_db
    response = client.post("/api/org/response",
                           json={"referral_id": "nope", "decision": "accepted"})
    assert response.status_code == 404
    # referral_id is a FK live, so an unknown one is logged with a NULL reference rather
    # than losing the trace entirely.
    assert any(e.get("referral_id") is None for e in db.integration_events)


def test_attempt_number_defaults_to_the_next_free_one(client_and_db):
    """`attempts` is UNIQUE (referral_id, service_id, attempt_number) and the column is
    NOT NULL with no default — a collision or an omission fails the insert live."""
    client, db = client_and_db
    rid = _at_submitted(db)
    referral = asyncio.run(db.get_referral(rid))
    asyncio.run(db.record_shared_attempt({
        "referral_id": rid, "service_id": referral.get("service_id"),
        "attempt_number": 1}))

    client.post("/api/org/response", json={"referral_id": rid, "decision": "accepted"})
    enrolled = [a for a in db.shared_attempts if a.get("outcome") == "enrolled"][0]
    assert enrolled["attempt_number"] == 2


def test_channel_is_validated_against_the_live_check(client_and_db):
    """`attempts.channel` is CHECK-constrained. A bad value reaches PostgREST as an
    opaque 500 from inside record_shared_attempt, and ONLY against the real DB — the
    mock accepts anything, so nothing else would catch it."""
    from backend.adapters.inbound import ATTEMPT_CHANNELS

    client, db = client_and_db
    rid = _at_submitted(db)

    bad = client.post("/api/org/response", json={
        "referral_id": rid, "decision": "accepted", "channel": "carrier_pigeon"})
    assert bad.status_code == 422
    assert not db.shared_attempts

    for channel in ATTEMPT_CHANNELS:
        assert client.post("/api/org/response", json={
            "referral_id": rid, "decision": "accepted", "channel": channel,
        }).status_code == 200
