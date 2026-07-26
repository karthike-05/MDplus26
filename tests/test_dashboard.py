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


# --- The SW board: live-status translation, patient response, data-source switch ------

def test_display_state_translates_the_live_vocabulary():
    """Live rows have no `current_state` — advance_referral() owns the workflow there
    (§7a) — so the board maps THEIR status. `enrolled` means the SERVICE accepted, so it
    must land on `confirmed`, never `completed`; collapsing the two milestones is the one
    thing the dashboard exists to avoid."""
    from backend.main import _display_state

    assert _display_state({"current_state": sm.SUBMITTED}) == sm.SUBMITTED   # ours wins
    assert _display_state({"status": "waiting_for_consent"}) == sm.CONSENT_PENDING
    assert _display_state({"status": "waiting_for_response"}) == sm.SUBMITTED
    assert _display_state({"status": "enrolled"}) == sm.CONFIRMED
    assert _display_state({"status": "enrolled",
                           "completion_outcome": "patient_confirmed_utilization"}) == sm.COMPLETED
    assert _display_state({"status": "nonsense_from_the_future"}) == sm.CREATED


def test_patient_response_never_reports_silence_as_a_no():
    """"Not asked yet" and "said no" are different facts. `used_service` is None until
    the patient actually answers — rendering that as False would invent a refusal."""
    from backend.main import _patient_response

    fresh = _patient_response({"current_state": sm.CONSENT_PENDING}, {})
    assert fresh["consent"] == "pending" and fresh["used_service"] is None

    done = _patient_response({"current_state": sm.COMPLETED}, {})
    assert done["used_service"] is True                      # offline: completed IS the answer

    denied = _patient_response({"status": "escalated",
                               "patient_confirmed_utilization": False}, {})
    assert denied["used_service"] is False

    live = _patient_response({"status": "in_progress"}, {"consent_status": "confirmed"})
    assert live["consent"] == "confirmed"                    # live column wins over inference


def test_dashboard_row_carries_channels_and_patient_response():
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_drive_senior",
                                         outreach_channel="phone"))
    asyncio.run(scheduler.run(rid, db, TOOLS))               # one notify_patient attempt
    import backend.main as m
    saved, m.db = m.db, db
    try:
        row = asyncio.run(m._dashboard_row(asyncio.run(db.get_referral(rid))))
    finally:
        m.db = saved
    assert row["channels_tried"] == ["whatsapp"]             # all three services land here
    assert row["attempt_count"] == 1
    assert row["patient_response"]["used_service"] is None


def test_db_mode_reports_mock_and_refuses_supabase_without_creds(monkeypatch):
    """A switch that silently stayed on the mock would be indistinguishable from a
    working one, so a missing credential must be a 400."""
    import backend.main as m
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    status = asyncio.run(m.get_db_mode())
    assert status["mode"] == "mock" and status["supabase_configured"] is False
    assert status["scheduler"] == "ours"

    try:
        asyncio.run(m.set_db_mode(m.DBMode(mode="supabase")))
    except Exception as e:
        assert getattr(e, "status_code", None) == 400
    else:
        raise AssertionError("expected a 400 when Supabase creds are absent")

    assert asyncio.run(m.set_db_mode(m.DBMode(mode="mock")))["mode"] == "mock"


def test_db_switch_reaches_captured_references():
    """Routers capture the handle at import (build_inbound_router(db, ...)), so a swap has
    to reach them — reassigning a module global would not."""
    import backend.main as m
    from backend.db.mock import MockReferralDB as Mock

    handle = m.DBSwitch(Mock())
    captured = handle                      # what a router would have held onto
    fresh = Mock()
    handle.swap(fresh)
    assert captured.kind == "MockReferralDB"
    assert captured._impl is fresh         # the captured reference sees the new impl
