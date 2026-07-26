"""The action-queue seam: our worker on the shared orchestration bus (L1, no I/O).

The live DB's `advance_referral()` decides the next step and addresses work to a
component; we are `karthik_form`. MockReferralDB mirrors that function in Python, so
these tests exercise the SAME worker code that will run against Supabase — which is
the point of the mirror. See backend/orchestrator/actions.py.
"""

from __future__ import annotations

import asyncio

from backend.db.mock import MockReferralDB
from backend.orchestrator import actions


def _consenting_db():
    """A referral past the consent gate, on a form-channel service."""
    db = MockReferralDB()
    db._patients["pat_001"]["consent_status"] = "confirmed"
    return db


# --- Vocabulary translation --------------------------------------------------

def test_our_status_maps_to_their_status_and_outcome_pair():
    """Their schema splits what we carry in one field, and the ranker reads `outcome`,
    so every one of our statuses must produce BOTH halves."""
    assert actions.STATUS_TO_THEIRS["success"] == ("completed", "submitted")
    assert actions.STATUS_TO_THEIRS["needs_human"] == ("completed", "needs_human_followup")
    assert actions.STATUS_TO_THEIRS["failed"] == ("failed", "technical_failure")


def test_pdf_target_records_as_email_channel():
    """attempts.channel has no value for "a filled PDF"; the PDF component's output
    reaches the service as an email attachment, so that's how it's recorded."""
    assert actions.CHANNEL_FOR_TARGET["pdf"] == "email"
    assert actions.CHANNEL_FOR_TARGET["web"] == "online_form"


def test_attempt_row_is_shaped_for_the_shared_table():
    from contracts.models import ToolOutcome
    outcome = ToolOutcome(referral_id="ref_1001", channel="form", status="success",
                          attempt_id="a1", data={"filled_fields": ["client_name"]})
    row = actions.attempt_row({"service_id": "svc_capmetro"}, outcome, "pdf")
    assert row["provider"] == "karthik_form"          # attempts.provider enum
    assert (row["status"], row["outcome"]) == ("completed", "submitted")
    assert row["channel"] == "email"
    assert isinstance(row["structured_result"], dict)  # jsonb NOT NULL
    assert row["notes"] is None


# --- advance_referral: the mirror addresses work to the right component ------

def test_consent_gate_queues_twilio_not_us():
    db = MockReferralDB()                               # consent still pending
    out = asyncio.run(db.advance_referral("ref_1001"))
    assert out["state"] == "waiting_for_consent"
    assert asyncio.run(db.list_ready_actions("karthik_form")) == []   # not ours yet
    assert asyncio.run(db.list_ready_actions("twilio"))[0]["action_type"] == "confirm_consent"


def test_form_channel_dispatches_prepare_to_us():
    db = _consenting_db()
    out = asyncio.run(db.advance_referral("ref_1001"))
    assert out["state"] == "in_progress" and out["channel"] == "online_form"
    ours = asyncio.run(db.list_ready_actions("karthik_form"))
    assert [a["action_type"] for a in ours] == ["prepare_online_form"]


def test_phone_channel_dispatches_to_retell_not_us():
    db = MockReferralDB()
    db._patients["pat_001"]["consent_status"] = "confirmed"
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_drive_senior"))
    out = asyncio.run(db.advance_referral(rid))
    assert out["channel"] == "phone"
    assert asyncio.run(db.list_ready_actions("retell"))[0]["action_type"] == \
        "contact_service_by_phone"


def test_open_action_blocks_a_second_dispatch():
    """advance_referral's first guard: never queue while an action is open. Without it
    a poll loop would fan out duplicate work."""
    db = _consenting_db()
    asyncio.run(db.advance_referral("ref_1001"))
    again = asyncio.run(db.advance_referral("ref_1001"))
    assert again["state"] == "waiting"
    assert len(asyncio.run(db.list_ready_actions("karthik_form"))) == 1


def test_queue_action_dedups_on_its_key():
    """Their ON CONFLICT (referral_id, deduplication_key) — the reason we don't need
    an attempt_id column of our own."""
    db = MockReferralDB()
    a = asyncio.run(db.queue_action("ref_1001", None, "prepare_online_form",
                                    "karthik_form", "k1", "r"))
    b = asyncio.run(db.queue_action("ref_1001", None, "prepare_online_form",
                                    "karthik_form", "k1", "r"))
    assert a == b and len(asyncio.run(db.list_ready_actions("karthik_form"))) == 1


# --- The worker --------------------------------------------------------------

def test_worker_ignores_a_queue_with_nothing_for_us():
    db = MockReferralDB()
    asyncio.run(db.advance_referral("ref_1001"))        # queues a twilio action
    assert asyncio.run(actions.run_once(db)) is None


def test_prepare_stops_at_the_review_gate_and_records_no_attempt():
    """Form outreach is human-gated (§2/§12): prepare must not submit. The action is
    left open so advance_referral keeps returning "waiting" instead of racing ahead."""
    db = _consenting_db()
    asyncio.run(db.advance_referral("ref_1001"))
    report = asyncio.run(actions.run_once(db))

    assert report["action"] == actions.PREPARE
    assert report["state"] == "awaiting_review"
    assert "appointment_time" in report["review"]["needs_attention"]
    assert db.shared_attempts == []                     # nothing was sent
    assert asyncio.run(db.advance_referral("ref_1001"))["state"] == "waiting"


def test_submit_writes_a_shared_attempt_and_hands_control_back(tmp_path):
    db = _consenting_db()
    asyncio.run(db.advance_referral("ref_1001"))
    prep = asyncio.run(actions.run_once(db))            # -> awaiting review
    values = dict(prep["review"]["values"])
    values["appointment_time"] = "10:15 AM"             # the reviewer completes it

    # The approval arrives as a submit action carrying the confirmed values.
    aid = asyncio.run(db.queue_action("ref_1001", "svc_capmetro", actions.SUBMIT,
                                      actions.COMPONENT, "submit:ref_1001", "approved",
                                      {"values": values}))
    report = asyncio.run(actions.run_once(db, submit_values=values))

    assert report["status"] == "success"
    row = db.shared_attempts[0]
    assert row["provider"] == "karthik_form" and row["outcome"] == "submitted"
    assert row["channel"] == "email"                    # transport_intake is a pdf target
    # the action is closed, and control returned to the DB scheduler
    assert [a["action_status"] for a in db._actions if a["id"] == aid] == ["completed"]
    assert "state" in report["advanced"]


# --- Adapter conformance -----------------------------------------------------

def test_no_adapter_silently_inherits_a_protocol_stub():
    """Every adapter subclasses ReferralDB, so a method it forgets to implement is
    INHERITED as the Protocol's `...` body and silently returns None instead of
    raising — e.g. list_ready_actions() -> None would crash the worker on iteration,
    and set_referral_service() would no-op a social worker's choice. Only the real-DB
    adapters are affected (nothing exercises them offline), so this check is the only
    thing standing between that and the flip.
    """
    from backend.db.interface import ReferralDB
    from backend.db.supabase import SupabaseReferralDB
    from backend.db.supabase_api import SupabaseAPIReferralDB

    required = [m for m in ReferralDB.__dict__ if not m.startswith("_")]
    for cls in (MockReferralDB, SupabaseReferralDB, SupabaseAPIReferralDB):
        own: set[str] = set()
        for klass in cls.__mro__:
            if klass is ReferralDB:
                break
            own |= set(klass.__dict__)
        missing = sorted(m for m in required if m not in own)
        assert not missing, f"{cls.__name__} inherits Protocol stubs: {missing}"
