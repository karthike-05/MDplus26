"""Dashboard + services + full-loop-driving tests (CLAUDE.md §7, §12). No I/O.

The important one is ``test_phone_referral_closes_loop_no_human``: a non-form channel
walks created -> completed entirely through the scheduler + simulated inbound events,
with no review step — proving the three submission methods are interchangeable and the
loop closes on any of them.
"""

from __future__ import annotations

import asyncio

from backend.db.mock import MockReferralDB
from backend.orchestrator import scheduler
from backend.orchestrator import state_machine as sm
from backend.main import TOOLS, INBOUND, _confirmation_source


def test_services_and_referrals_listed():
    db = MockReferralDB()
    services = asyncio.run(db.list_services())
    assert len(services) >= 5
    assert {s["preferred_channel"] for s in services} >= {"form", "phone", "text", "email"}
    assert len(asyncio.run(db.list_referrals())) == 3


def test_confirmation_source_distinguishes_milestones():
    assert _confirmation_source(sm.CONFIRMED) == "org_email"        # service said yes
    assert _confirmation_source(sm.COMPLETED) == "patient_reply"    # patient used it
    assert _confirmation_source(sm.SUBMITTED) is None


def _inbound(db, rid, signal):
    status, channel = INBOUND[signal]
    asyncio.run(scheduler.apply_inbound(rid, db, status=status, channel=channel))
    asyncio.run(scheduler.run(rid, db, TOOLS))  # cascade push states, mirrors the API


def test_phone_referral_closes_loop_no_human():
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_drive_senior",
                                         service_name="Drive A Senior ATX", outreach_channel="phone"))
    # created -> request consent
    asyncio.run(scheduler.run(rid, db, TOOLS))
    assert asyncio.run(db.get_referral(rid))["current_state"] == sm.CONSENT_PENDING
    _inbound(db, rid, "consent")   # -> consent_granted -> outreach -> phone tool -> submitted
    assert asyncio.run(db.get_referral(rid))["current_state"] == sm.SUBMITTED
    _inbound(db, rid, "response")  # -> confirmed -> check-in -> check_in_scheduled
    assert asyncio.run(db.get_referral(rid))["current_state"] == sm.CHECK_IN_SCHEDULED
    _inbound(db, rid, "used")      # -> completed
    assert asyncio.run(db.get_referral(rid))["current_state"] == sm.COMPLETED


def test_form_referral_stops_at_review_gate():
    """A form referral must NOT auto-submit: the scheduler stops at outreach_in_progress
    (fill_form isn't in TOOLS) and waits for the human review screen."""
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", "transport_intake",
                                         service_id="svc_capmetro", outreach_channel="form"))
    asyncio.run(scheduler.run(rid, db, TOOLS))
    _inbound(db, rid, "consent")
    assert asyncio.run(db.get_referral(rid))["current_state"] == sm.OUTREACH_IN_PROGRESS


def test_attempts_timeline_is_ordered_with_timestamps():
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_drive_senior",
                                         outreach_channel="phone"))
    asyncio.run(scheduler.run(rid, db, TOOLS))
    _inbound(db, rid, "consent")
    attempts = asyncio.run(db.list_attempts(rid))
    assert len(attempts) >= 2
    assert all(a["at"] for a in attempts)                     # every attempt timestamped
    assert [a["at"] for a in attempts] == sorted(a["at"] for a in attempts)  # oldest first
