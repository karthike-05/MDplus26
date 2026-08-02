"""The action-queue runner + the two seam fixes that ship with it (L1, no I/O).

`tests/test_actions.py` covers servicing ONE action. This covers the things that only
show up when something drives that on a loop: draining a backlog, recovering an action
whose worker died, and not raising into the event loop when a tool blows up.

Also covers the two live-schema constraints our writes had been ignoring —
`attempts.attempt_number` and the `integration_events` log — because both are invisible
offline (the mock accepts anything) and fail hard on the first real insert.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.db.mock import MockReferralDB
from backend.orchestrator import actions, backend_component, voice_component, worker
from backend.tools import send_email as send_email_mod


def _consenting_db():
    db = MockReferralDB()
    db._patients["pat_001"]["consent_status"] = "confirmed"
    return db


# --- The runner ---------------------------------------------------------------

def test_tick_drains_the_queue_rather_than_one_action_per_interval():
    """A backlog must clear in one tick. One-per-interval means a five-action backlog
    takes five poll intervals to clear, which on a 5s poll is a visibly stalled demo."""
    db = _consenting_db()
    asyncio.run(db.advance_referral("ref_1001"))          # queues a prepare for us
    # A second, independent referral on the same form service.
    rid = asyncio.run(db.create_referral("pat_001", "transport_intake",
                                         service_id="svc_capmetro"))
    asyncio.run(db.advance_referral(rid))

    reports = asyncio.run(worker.tick(db))
    assert len(reports) == 2
    assert {r["state"] for r in reports} == {"awaiting_review"}
    assert asyncio.run(db.list_ready_actions(actions.COMPONENT)) == []


def test_tick_on_an_empty_queue_is_a_no_op():
    assert asyncio.run(worker.tick(MockReferralDB())) == []


def test_stale_in_progress_action_is_reclaimed():
    """The deadlock this exists to prevent: a worker dies holding an action, the row
    stays `in_progress`, and advance_referral's open-action guard then returns `waiting`
    for that referral forever."""
    db = _consenting_db()
    asyncio.run(db.advance_referral("ref_1001"))
    action = asyncio.run(db.list_ready_actions(actions.COMPONENT))[0]
    asyncio.run(db.set_action_status(action["id"], "in_progress"))   # ...then "crash"

    assert asyncio.run(db.list_ready_actions(actions.COMPONENT)) == []
    assert asyncio.run(db.advance_referral("ref_1001"))["state"] == "waiting"

    # Not yet old enough — reclaiming eagerly would yank an action out from under a
    # worker that is simply still working.
    assert asyncio.run(db.reclaim_stale_actions(actions.COMPONENT, 120)) == 0

    _age(db, action["id"], seconds=300)
    assert asyncio.run(db.reclaim_stale_actions(actions.COMPONENT, 120)) == 1
    assert [a["action_type"] for a in asyncio.run(db.list_ready_actions(actions.COMPONENT))] \
        == ["prepare_online_form"]


def test_blocked_actions_are_never_reclaimed():
    """`prepare_online_form` parks its action at `blocked` while it waits for a human
    reviewer (§2 — form outreach is human-gated). Reclaiming that would re-run prepare
    in a loop behind the reviewer's back, and worse, look like progress."""
    db = _consenting_db()
    asyncio.run(db.advance_referral("ref_1001"))
    asyncio.run(actions.run_once(db))                     # -> blocked, awaiting review
    blocked = [a for a in db._actions if a["action_status"] == "blocked"]
    assert len(blocked) == 1

    _age(db, blocked[0]["id"], seconds=10_000)
    assert asyncio.run(db.reclaim_stale_actions(actions.COMPONENT, 120)) == 0
    assert blocked[0]["action_status"] == "blocked"


def test_a_failing_tool_marks_the_action_failed_instead_of_raising():
    """If servicing raised, the action would stay `in_progress` and deadlock the
    referral until the stale sweep — and in the loop, an escaping exception would take
    the whole worker down. Record the failure, keep going."""
    db = _consenting_db()
    asyncio.run(db.advance_referral("ref_1001"))
    action_id = asyncio.run(db.list_ready_actions(actions.COMPONENT))[0]["id"]

    async def boom(*a, **k):
        raise RuntimeError("injector exploded")

    import backend.tools.fill_form.fill_form as ff
    original, ff.prepare = ff.prepare, boom
    try:
        report = asyncio.run(actions.run_once(db))
    finally:
        ff.prepare = original

    assert report["state"] == "failed"
    assert "injector exploded" in report["error"]
    closed = [a for a in db._actions if a["id"] == action_id][0]
    assert closed["action_status"] == "failed"
    assert "injector exploded" in closed["error_message"]


def test_run_forever_survives_a_broken_db_and_keeps_ticking():
    """A transient DB error must not kill the loop — a dead worker and an idle queue
    look identical from outside, which is the failure mode this whole module guards."""
    class Flaky(MockReferralDB):
        calls = 0

        async def list_ready_actions(self, component):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise ConnectionError("supabase unreachable")
            return []

    async def drive():
        task = asyncio.create_task(worker.run_forever(Flaky(), interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert Flaky.calls > 1                    # it kept going after the failure


def test_worker_is_disabled_by_the_env_flag(monkeypatch):
    monkeypatch.setenv("WORKER_ENABLED", "0")
    assert worker.enabled() is False
    monkeypatch.setenv("WORKER_ENABLED", "1")
    assert worker.enabled() is True


# --- attempts.attempt_number (NOT NULL, no default, UNIQUE per referral+service) ----

def test_attempt_row_carries_an_attempt_number():
    """Live, `attempt_number` is NOT NULL with no default — omitting it doesn't default
    to 1, it fails the insert. Offline the mock accepts anything, so only this test
    stands between that and the first real submit."""
    from contracts.models import ToolOutcome
    outcome = ToolOutcome(referral_id="ref_1001", channel="form", status="success",
                          attempt_id="a1")
    row = actions.attempt_row({"service_id": "svc_capmetro"}, outcome, "pdf", 2)
    assert row["attempt_number"] == 2


def test_next_attempt_number_counts_per_referral_and_service():
    """The live UNIQUE is (referral_id, service_id, attempt_number), so the counter is
    per pair — a second service on the same referral starts again at 1."""
    db = MockReferralDB()
    assert asyncio.run(db.next_attempt_number("ref_1001", "svc_capmetro")) == 1
    asyncio.run(db.record_shared_attempt(
        {"referral_id": "ref_1001", "service_id": "svc_capmetro", "attempt_number": 1}))
    assert asyncio.run(db.next_attempt_number("ref_1001", "svc_capmetro")) == 2
    assert asyncio.run(db.next_attempt_number("ref_1001", "svc_other")) == 1
    assert asyncio.run(db.next_attempt_number("ref_other", "svc_capmetro")) == 1


def test_attempt_is_recorded_under_the_dispatched_channel_not_the_file_format():
    """A PDF submitted through a service configured for `online_form` must record
    `online_form`. Recording `email` leaves advance_referral's step-9 exhaustion test
    unable to see the channel as tried: it re-picks the same channel, the dedup key is
    unchanged, ON CONFLICT hands back the already-completed action, and the referral
    stalls at `in_progress` with no error anywhere."""
    from contracts.models import ToolOutcome
    outcome = ToolOutcome(referral_id="ref_1001", channel="form", status="success",
                          attempt_id="a1")
    dispatched = actions.attempt_row({"service_id": "s"}, outcome, "pdf", 1, "online_form")
    assert dispatched["channel"] == "online_form"

    # ...and with nothing dispatched, fall back to how the document reaches the service.
    assert actions.attempt_row({"service_id": "s"}, outcome, "pdf", 1)["channel"] == "email"


def test_submit_records_the_channel_advance_referral_chose():
    """End to end through the worker: the mock's fixture service is a form service, so
    the dispatched channel is `online_form` and that is what lands in `attempts`."""
    db = _consenting_db()
    asyncio.run(db.advance_referral("ref_1001"))
    prep = asyncio.run(actions.run_once(db))
    values = dict(prep["review"]["values"], appointment_time="10:15 AM")
    asyncio.run(db.queue_action("ref_1001", "svc_capmetro", actions.SUBMIT,
                                actions.COMPONENT, "sub:ref_1001", "approved",
                                {"values": values, "channel": "online_form"}))
    asyncio.run(actions.run_once(db, submit_values=values))
    assert db.shared_attempts[0]["channel"] == "online_form"


def test_submit_writes_a_numbered_attempt():
    db = _consenting_db()
    asyncio.run(db.advance_referral("ref_1001"))
    prep = asyncio.run(actions.run_once(db))
    values = dict(prep["review"]["values"], appointment_time="10:15 AM")
    asyncio.run(db.queue_action("ref_1001", "svc_capmetro", actions.SUBMIT,
                                actions.COMPONENT, "submit:ref_1001", "approved",
                                {"values": values}))
    asyncio.run(actions.run_once(db, submit_values=values))
    assert db.shared_attempts[0]["attempt_number"] == 1


# --- integration_events (A12) --------------------------------------------------

def test_inbound_event_is_persisted_to_the_webhook_log():
    from fastapi.testclient import TestClient
    import backend.main as main

    db = MockReferralDB()
    db._referrals["ref_1001"]["current_state"] = "check_in_scheduled"
    main.db.swap(db)
    try:
        with TestClient(main.app) as client:
            r = client.post("/api/patient-comms/event",
                            json={"referral_id": "ref_1001", "event": "verified_utilized",
                                  "outreach_id": "out_77"})
            assert r.status_code == 200
    finally:
        main.db.swap(MockReferralDB())

    assert len(db.integration_events) == 1
    event = db.integration_events[0]
    # provider is the SENDER's name, not ours — the live CHECK allows only
    # twilio / retell / karthik_form.
    assert event["provider"] == "twilio"
    assert event["event_type"] == "verified_utilized"
    assert event["referral_id"] == "ref_1001"
    assert event["external_id"] == "out_77"        # dedupe key for a retried webhook
    assert event["processing_status"] == "processed"


def test_a_rejected_event_is_logged_too():
    """An unrecognised vocabulary word is silent from the sender's side — they get a 422
    and nothing else. It's precisely what the durable log is for."""
    from fastapi.testclient import TestClient
    import backend.main as main

    db = MockReferralDB()
    main.db.swap(db)
    try:
        with TestClient(main.app) as client:
            r = client.post("/api/voice/call-outcome",
                            json={"referral_id": "ref_1001", "status": "nonsense",
                                  "call_id": "call_9"})
            assert r.status_code == 422
    finally:
        main.db.swap(MockReferralDB())

    assert [e["processing_status"] for e in db.integration_events] == ["failed"]
    assert db.integration_events[0]["provider"] == "retell"


def test_a_retried_webhook_collapses_onto_one_row():
    """Mirrors the live UNIQUE (provider, external_id, event_type): a webhook delivered
    twice is one event, not two."""
    db = MockReferralDB()
    event = {"provider": "retell", "event_type": "confirmed", "external_id": "call_1",
             "payload": {}, "referral_id": "ref_1001", "processing_status": "processed"}
    asyncio.run(db.record_integration_event(dict(event)))
    asyncio.run(db.record_integration_event(dict(event)))
    assert len(db.integration_events) == 1

    # ...but without an external_id, Postgres treats NULLs as distinct, so both land.
    anon = dict(event, external_id=None)
    asyncio.run(db.record_integration_event(dict(anon)))
    asyncio.run(db.record_integration_event(dict(anon)))
    assert len(db.integration_events) == 3


def test_a_failing_event_log_does_not_break_the_transition():
    """The event has already been applied by the time we log it. Failing the request
    because the audit write failed would turn bookkeeping into a lost transition."""
    from fastapi.testclient import TestClient
    import backend.main as main

    class NoLog(MockReferralDB):
        async def record_integration_event(self, event):
            raise ConnectionError("integration_events unreachable")

    db = NoLog()
    db._referrals["ref_1001"]["current_state"] = "check_in_scheduled"
    main.db.swap(db)
    try:
        with TestClient(main.app) as client:
            r = client.post("/api/patient-comms/event",
                            json={"referral_id": "ref_1001", "event": "verified_utilized"})
            assert r.status_code == 200
            assert r.json()["state"] == "completed"
    finally:
        main.db.swap(MockReferralDB())


# --- the `backend` component (A2) ---------------------------------------------

def _awaiting_selection():
    """A consented referral with a shortlist but no service chosen yet — the state that
    makes advance_referral emit `select_resource` to `backend`."""
    db = _consenting_db()
    db._referrals["ref_1001"]["service_id"] = None
    db._candidates["ref_1001"] = [
        {"service_id": "svc_capmetro", "rank": 1,
         "candidate_status": "available", "eligibility_state": "eligible"},
    ]
    return db


def test_an_unserviced_backend_action_deadlocks_the_referral():
    """The bug A2 describes. advance_referral queues bookkeeping to `backend` AFTER it
    has already done the work, so the open row makes every later call return `waiting` —
    forever, with no poller."""
    db = _consenting_db()
    asyncio.run(db.queue_action("ref_1001", "svc_capmetro", "complete_referral",
                                "backend", "complete:ref_1001", "enrollment recorded"))
    for _ in range(3):
        assert asyncio.run(db.advance_referral("ref_1001"))["state"] == "waiting"


def test_servicing_the_backend_action_unblocks_the_chain():
    db = _consenting_db()
    asyncio.run(db.queue_action("ref_1001", "svc_capmetro", "complete_referral",
                                "backend", "complete:ref_1001", "enrollment recorded"))

    report = asyncio.run(backend_component.run_once(db))
    assert report["action"] == "complete_referral"
    assert report["result"]["service_id"] == "svc_capmetro"
    # and it advanced: the next step is now addressed to US
    assert report["advanced"]["state"] == "in_progress"
    assert [a["action_type"] for a in asyncio.run(db.list_ready_actions("karthik_form"))] \
        == ["prepare_online_form"]


def test_one_tick_clears_a_cross_component_chain():
    """`backend` unblocks `karthik_form` within the same tick. Draining one component
    fully before the other would need two ticks — two poll intervals of visible stall."""
    db = _consenting_db()
    asyncio.run(db.queue_action("ref_1001", "svc_capmetro", "complete_referral",
                                "backend", "complete:ref_1001", "enrollment recorded"))

    reports = asyncio.run(worker.tick(db))
    assert [r["action"] for r in reports] == ["complete_referral", "prepare_online_form"]
    assert reports[-1]["state"] == "awaiting_review"


# --- the `retell` component (A4) -----------------------------------------------

def _phone_ready_db():
    """A consented referral whose chosen service's next unused channel is `phone` --
    the state that makes advance_referral emit `contact_service_by_phone` to `retell`."""
    db = _consenting_db()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_drive_senior"))
    asyncio.run(db.advance_referral(rid))
    return db, rid


def _mock_call_agent_response(json_body):
    """A fake httpx.Response for call_agent's /place-referral-call, so these tests
    never hit the real (deployed) network (CLAUDE.md §9: layered tests, no I/O)."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=json_body)
    return response


def test_retell_queue_ignored_when_empty():
    assert asyncio.run(voice_component.run_once(MockReferralDB())) is None


def test_placed_call_leaves_the_action_blocked_awaiting_the_webhook(monkeypatch):
    """The common path. call_agent's own /log-call-outcome webhook is what eventually
    writes the attempts row and closes this action (main.py's
    _close_action_and_advance) -- so this module must NOT call advance_referral itself
    on this path, and must NOT mark the action completed (that would let
    advance_referral re-queue the same channel under a now-dead dedup key, §7c)."""
    db, rid = _phone_ready_db()
    monkeypatch.setenv("ALLOW_LIVE_CALLS", "1")
    monkeypatch.setenv("CALL_AGENT_BASE_URL", "http://call-agent.test")
    with patch("httpx.AsyncClient.post",
               new=AsyncMock(return_value=_mock_call_agent_response({"call_id": "call_123"}))):
        report = asyncio.run(voice_component.run_once(db))

    assert report["action"] == voice_component.DISPATCH
    assert report["state"] == "awaiting_call_outcome"
    action = next(a for a in db._actions if a["referral_id"] == rid)
    assert action["action_status"] == "blocked"
    assert db.shared_attempts == []                     # nothing written -- not our job
    # still open, so a re-poll must not re-dispatch
    assert asyncio.run(db.advance_referral(rid))["state"] == "waiting"


def test_call_agent_unreachable_marks_the_action_failed(monkeypatch):
    """Mirrors actions.py / backend_component.py: a servicing error is recorded on the
    action, never raised -- an action left `in_progress` would deadlock the referral."""
    db, rid = _phone_ready_db()
    monkeypatch.setenv("ALLOW_LIVE_CALLS", "1")
    monkeypatch.setenv("CALL_AGENT_BASE_URL", "http://call-agent.test")
    import httpx
    with patch("httpx.AsyncClient.post",
               new=AsyncMock(side_effect=httpx.ConnectError("ngrok tunnel down"))):
        report = asyncio.run(voice_component.run_once(db))

    assert report["state"] == "failed"
    assert "ngrok tunnel down" in report["error"]
    action = next(a for a in db._actions if a["referral_id"] == rid)
    assert action["action_status"] == "failed"


def test_missing_call_agent_base_url_fails_cleanly(monkeypatch):
    monkeypatch.setenv("ALLOW_LIVE_CALLS", "1")
    db, rid = _phone_ready_db()
    monkeypatch.delenv("CALL_AGENT_BASE_URL", raising=False)
    report = asyncio.run(voice_component.run_once(db))
    assert report["state"] == "failed"
    assert "CALL_AGENT_BASE_URL" in report["error"]


def test_escalated_response_closes_the_action_and_advances(monkeypatch):
    """call_agent's own MAX_ATTEMPTS cap already hit -- no call was placed, so no
    webhook is ever coming. Unlike the normal path, this module has to close the
    action and advance the referral itself, since nobody else will."""
    db, rid = _phone_ready_db()
    monkeypatch.setenv("ALLOW_LIVE_CALLS", "1")
    monkeypatch.setenv("CALL_AGENT_BASE_URL", "http://call-agent.test")
    escalated = {"escalated": True, "reason": "max_attempts_exceeded"}
    with patch("httpx.AsyncClient.post",
               new=AsyncMock(return_value=_mock_call_agent_response(escalated))):
        report = asyncio.run(voice_component.run_once(db))

    assert report["state"] == "escalated"
    assert report["result"]["escalated"] is True
    assert "state" in report["advanced"]
    action = next(a for a in db._actions if a["referral_id"] == rid)
    assert action["action_status"] == "completed"


def test_worker_tick_drains_the_retell_queue_too(monkeypatch):
    """End to end through the actual COMPONENTS tuple, not just the module directly --
    confirms voice_component is really registered on the worker's drain loop."""
    db, rid = _phone_ready_db()
    monkeypatch.setenv("ALLOW_LIVE_CALLS", "1")
    monkeypatch.setenv("CALL_AGENT_BASE_URL", "http://call-agent.test")
    with patch("httpx.AsyncClient.post",
               new=AsyncMock(return_value=_mock_call_agent_response({"call_id": "call_123"}))):
        reports = asyncio.run(worker.tick(db))

    assert any(r.get("action") == voice_component.DISPATCH for r in reports)
    assert asyncio.run(db.list_ready_actions("retell")) == []   # claimed, not left ready


# --- The social-worker selection gate (003_sw_selection_gate.sql) --------------

def test_a_ranked_shortlist_waits_for_the_social_worker():
    """The product intent: the SW sees the options and picks. Before the gate,
    advance_referral silently took rank 1 and dispatched outreach, so the human was never
    asked and `sw_feedback` — the signal Layer 3 learns from — had no event to record."""
    db = _awaiting_selection()
    out = asyncio.run(db.advance_referral("ref_1001"))
    assert out["state"] == "awaiting_sw_selection"

    queued = asyncio.run(db.list_ready_actions("social_worker"))
    assert [a["action_type"] for a in queued] == ["select_resource"]
    # nothing was dispatched to an outreach component, and no service was chosen
    assert asyncio.run(db.list_ready_actions("karthik_form")) == []
    assert db._referrals["ref_1001"]["service_id"] is None
    # ...and it stays parked until a human acts
    assert asyncio.run(db.advance_referral("ref_1001"))["state"] == "waiting"


def test_the_social_workers_pick_is_adopted_not_overruled():
    """They may choose rank 2. The scheduler must take THAT service — not re-sort and
    quietly substitute its own favourite."""
    db = _awaiting_selection()
    db._candidates["ref_1001"].append(
        {"service_id": "svc_drive_senior", "rank": 2,
         "candidate_status": "available", "eligibility_state": "eligible"})
    asyncio.run(db.advance_referral("ref_1001"))

    asyncio.run(db.select_candidate("ref_1001", "svc_drive_senior"))       # the SW picks #2
    for a in db._actions:                                                   # ...and it's closed
        if a["action_type"] == "select_resource":
            a["action_status"] = "completed"

    out = asyncio.run(db.advance_referral("ref_1001"))
    assert out["state"] == "resource_selected"
    assert out["service_id"] == "svc_drive_senior"
    assert db._referrals["ref_1001"]["current_resource_rank"] == 2
    # and the next dispatch follows THEIR choice — a phone service, not the form one
    assert asyncio.run(db.advance_referral("ref_1001"))["channel"] == "phone"


def test_select_candidate_releases_the_previous_pick():
    """Only ever one `selected` row: otherwise the gate's "adopt the selected one" branch
    would pick arbitrarily between two."""
    db = _awaiting_selection()
    db._candidates["ref_1001"].append(
        {"service_id": "svc_drive_senior", "rank": 2,
         "candidate_status": "available", "eligibility_state": "eligible"})

    asyncio.run(db.select_candidate("ref_1001", "svc_capmetro"))
    asyncio.run(db.select_candidate("ref_1001", "svc_drive_senior"))   # changed their mind
    chosen = [c for c in asyncio.run(db.list_candidates("ref_1001")) if c.get("selected")]
    assert [c["service_id"] for c in chosen] == ["svc_drive_senior"]
    released = {c["service_id"]: c["candidate_status"]
                for c in asyncio.run(db.list_candidates("ref_1001"))}
    assert released["svc_capmetro"] == "available"


def test_choose_service_completes_the_whole_gate():
    """The endpoint has to do FOUR things, and skipping any one of them silently breaks
    the gate: flag the candidate (else advance_referral re-asks), point the referral,
    close the social_worker action (else the open-action guard freezes the referral on
    the very choice just made), and record the label (the ranker's only training signal).
    """
    from fastapi.testclient import TestClient
    import backend.main as main

    db = _awaiting_selection()
    db._candidates["ref_1001"].append(
        {"service_id": "svc_drive_senior", "rank": 2,
         "candidate_status": "available", "eligibility_state": "eligible"})
    asyncio.run(db.advance_referral("ref_1001"))
    assert asyncio.run(db.list_ready_actions("social_worker"))            # parked

    main.db.swap(db)
    try:
        with TestClient(main.app) as client:
            board = client.get("/api/dashboard").json()["rows"]
            row = next(r for r in board if r["referral_id"] == "ref_1001")
            assert row["awaiting_sw_selection"] is True
            assert row["needs_attention"] is True      # surfaces under "Needs you"

            r = client.post("/api/referrals/ref_1001/choose-service",
                            json={"service_id": "svc_drive_senior", "label": "good_fit",
                                  "label_notes": "patient has used them before"})
            assert r.status_code == 200, r.text
    finally:
        main.db.swap(MockReferralDB())

    chosen = [c for c in asyncio.run(db.list_candidates("ref_1001")) if c.get("selected")]
    assert [c["service_id"] for c in chosen] == ["svc_drive_senior"]
    assert db._referrals["ref_1001"]["service_id"] == "svc_drive_senior"
    assert [a["action_status"] for a in db._actions
            if a["action_type"] == "select_resource"] == ["completed"]
    # And the referral is free to move again, straight to outreach on THEIR choice —
    # not `resource_selected`, because choose-service already pointed the referral, so
    # the gate's "adopt the selected candidate" branch has nothing left to do. That
    # branch is the safety net for a candidate flagged without a service_id.
    assert asyncio.run(db.advance_referral("ref_1001"))["state"] == "in_progress"


def test_choose_service_survives_a_live_service_row():
    """Regression: SERVICE_COLS maps `preferred_channel` to None live (no such column),
    so _to_ours omits the key and `svc["preferred_channel"]` raised KeyError — taking out
    the entire endpoint against the real DB while every offline test passed."""
    import backend.main as main

    live_shaped = {"id": "svc_x", "name": "A Service", "category": "Transportation",
                   "description": None, "email": None, "website": None}
    assert main._service_backfill(live_shaped) == {
        "service_name": "A Service", "need_category": "transportation"}


def test_an_empty_shortlist_still_escalates():
    """The pre-existing "no candidate remains" path must survive the new gate — a
    referral with a shortlist that is entirely exhausted needs a human, not a wait."""
    db = _consenting_db()
    db._referrals["ref_1001"]["service_id"] = None
    db._candidates["ref_1001"] = [
        {"service_id": "svc_capmetro", "rank": 1,
         "candidate_status": "exhausted", "eligibility_state": "eligible"},
    ]
    out = asyncio.run(db.advance_referral("ref_1001"))
    assert out["state"] == "escalated"
    assert [a["action_type"] for a in asyncio.run(db.list_ready_actions("social_worker"))] \
        == ["escalate_to_social_worker"]


def test_rank_resources_is_left_for_ranking_by_default():
    """Claiming it would complete an action whose work never ran, and the referral would
    then escalate as "no eligible resource" — worse than the deadlock (A1 is Ranking's)."""
    db = _consenting_db()
    db._referrals["ref_1001"]["service_id"] = None
    asyncio.run(db.advance_referral("ref_1001"))            # no candidates -> rank_resources

    queued = asyncio.run(db.list_ready_actions("backend"))
    assert [a["action_type"] for a in queued] == ["rank_resources"]
    assert asyncio.run(backend_component.run_once(db)) is None      # untouched
    assert asyncio.run(db.list_ready_actions("backend"))[0]["action_status"] == "ready"


def test_claiming_ranking_without_a_ranking_url_refuses_rather_than_faking_it(monkeypatch):
    monkeypatch.setenv("BACKEND_CLAIM_RANKING", "1")
    monkeypatch.delenv("SERVICE_RANKING_BASE_URL", raising=False)
    db = _consenting_db()
    db._referrals["ref_1001"]["service_id"] = None
    asyncio.run(db.advance_referral("ref_1001"))

    report = asyncio.run(backend_component.run_once(db))
    assert report["state"] == "failed"
    assert "SERVICE_RANKING_BASE_URL" in report["error"]


def _queue_email_action(db):
    asyncio.run(db.queue_action("ref_1001", "svc_capmetro", "contact_service_by_email",
                                "backend", "email:ref_1001", "email the service",
                                {"attempt_number": 2}))


def test_email_action_writes_a_shared_attempt(monkeypatch):
    """With a provider configured, the email path records a normal sent attempt."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    db = _consenting_db()
    _queue_email_action(db)

    report = asyncio.run(backend_component.run_once(db))

    assert report["result"]["sent"] is True
    row = db.shared_attempts[0]
    assert row["channel"] == "email" and row["attempt_number"] == 2
    # attempts.provider is CHECK-constrained and has no `backend` value.
    assert row["provider"] == "internal"


def test_email_action_with_no_provider_does_not_claim_a_send(monkeypatch):
    """`send_email` is a stub until a provider is wired (whats-left B3), and the live bus
    reaches it with no human involved — advance_referral picks the email channel from
    service_application_channels by priority, and 12 services carry one. It used to
    record a plain success, so the referral moved to "awaiting service response" for a
    message that was never composed. It must land in the SW's escalations queue instead.
    """
    for name in send_email_mod.PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    db = _consenting_db()
    _queue_email_action(db)

    report = asyncio.run(backend_component.run_once(db))

    assert report["result"]["sent"] is False
    assert report["result"]["stub"] is True
    row = db.shared_attempts[0]
    assert row["channel"] == "email"
    assert row["outcome"] == "needs_human_followup", "must not read as a delivered email"


def test_orchestrator_tick_is_opt_in(monkeypatch):
    """Opt-in because it's one of two valid designs (A3): a central tick, or every
    component advancing itself. Doing both is safe only because of the open-action
    guard, so the choice should be explicit.

    Drives the ENV VAR, not a module attribute. Patching the attribute is what let the
    import-time bug below ship green.
    """
    db = _consenting_db()
    monkeypatch.setenv("ORCHESTRATOR_TICK", "0")
    assert asyncio.run(worker.advance_open_referrals(db)) == []

    monkeypatch.setenv("ORCHESTRATOR_TICK", "1")
    advanced = asyncio.run(worker.advance_open_referrals(db))
    assert advanced and all("advanced" in a for a in advanced)


def test_orchestrator_tick_skips_terminal_referrals(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_TICK", "1")
    db = _consenting_db()
    for referral in db._referrals.values():
        referral["status"] = "enrolled"
    assert asyncio.run(worker.advance_open_referrals(db)) == []


def test_env_flags_are_read_at_call_time_not_import(monkeypatch):
    """REGRESSION (2026-07-28). These were module-level `os.getenv` constants, but
    `backend.main` imports this module BEFORE calling `load_dotenv()` — so a value set
    in `.env` was evaluated against an environment that didn't have it yet. The flag
    read back False at /health and the sweep silently never ran.

    Any of these going back to an import-time constant re-breaks `.env` configuration
    with no error anywhere, so assert the late binding directly.
    """
    monkeypatch.setenv("ORCHESTRATOR_TICK", "1")
    assert worker.orchestrator_tick() is True
    monkeypatch.setenv("ORCHESTRATOR_TICK", "0")
    assert worker.orchestrator_tick() is False

    monkeypatch.setenv("WORKER_POLL_SECONDS", "0.25")
    assert worker.poll_seconds() == 0.25
    monkeypatch.setenv("WORKER_STALE_AFTER_SECONDS", "7")
    assert worker.stale_after() == 7


# --- helpers ------------------------------------------------------------------

def _age(db: MockReferralDB, action_id: str, *, seconds: int) -> None:
    """Backdate an action's updated_at so the stale sweep can see it."""
    for a in db._actions:
        if a["id"] == action_id:
            a["updated_at"] = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            return
    raise KeyError(action_id)


# --- The static file mount (security) -----------------------------------------

def test_spa_fallback_cannot_read_files_outside_dist():
    """Regression: `GET /%2e%2e%2f%2e%2e%2f.env` returned the repo's .env —
    SUPABASE_SERVICE_ROLE_KEY and ANTHROPIC_API_KEY — to anyone who could reach this
    process. Starlette does not normalise `..` out of a `:path` param and
    percent-encoded traversal survives into the string, so containment has to be
    enforced in the handler itself.

    Skipped when frontend/dist is absent: the route only exists once the UI is built.
    """
    from pathlib import Path
    from fastapi.testclient import TestClient
    import backend.main as main

    if not (Path(main.ROOT) / "frontend" / "dist").is_dir():
        pytest.skip("frontend/dist not built")

    secrets = ("SUPABASE_SERVICE_ROLE_KEY", "ANTHROPIC_API_KEY", "sk-ant", "root:")
    attacks = [
        "/%2e%2e%2f%2e%2e%2f.env",
        "/..%2f..%2f.env",
        "/%2e%2e/%2e%2e/.env",
        "/../../.env",
        "/../../CLAUDE.md",
        "/....//....//.env",
        "/../../../../etc/passwd",
    ]
    with TestClient(main.app) as client:
        for path in attacks:
            body = client.get(path).text
            assert not any(s in body for s in secrets), f"{path} leaked a secret"
            assert "<!doctype html" in body.lower(), f"{path} returned a non-SPA body"


def test_sweep_entries_are_not_counted_as_serviced_actions(monkeypatch):
    """REGRESSION (2026-07-28). The advance sweep returns one entry per open referral
    whether or not anything happened, and tick() folded those into `actions_serviced`.
    Live that read 580 actions serviced after 145 idle ticks — a number the Integration
    screen renders, and which was flatly untrue. They're separate counters now."""
    monkeypatch.setenv("ORCHESTRATOR_TICK", "1")
    monkeypatch.setenv("ORCHESTRATOR_SWEEP_SECONDS", "0")   # sweep every tick
    db = _consenting_db()
    worker.status.serviced = worker.status.referrals_advanced = worker.status.ticks = 0

    asyncio.run(worker.tick(db))                            # nothing queued for us yet
    assert worker.status.serviced == 0, "an idle tick serviced no actions"
    assert worker.status.referrals_advanced > 0, "but it did sweep referrals"


def test_the_advance_sweep_runs_slower_than_the_drain(monkeypatch):
    """Every sweep is one advance_referral() RPC per open referral against the TEAM's
    database. The drain has to stay responsive (5s) for the demo to feel live; the sweep
    is only a safety net for components that don't self-advance, so it gets its own
    slower cadence rather than firing on every tick."""
    monkeypatch.setenv("ORCHESTRATOR_TICK", "1")
    monkeypatch.setenv("WORKER_POLL_SECONDS", "5")
    monkeypatch.setenv("ORCHESTRATOR_SWEEP_SECONDS", "30")  # -> every 6th tick
    db = _consenting_db()
    worker.status.referrals_advanced = worker.status.ticks = 0

    for _ in range(12):
        asyncio.run(worker.tick(db))

    # 12 ticks at 5s = 60s of wall clock -> 2 sweeps, not 12.
    referrals = len(asyncio.run(db.list_referrals()))
    assert worker.status.referrals_advanced == 2 * referrals


def test_sweep_still_fires_on_the_first_tick(monkeypatch):
    """A safety net that waits 30s before its first run makes a fresh deploy look stuck
    for exactly as long as someone is watching it start."""
    monkeypatch.setenv("ORCHESTRATOR_TICK", "1")
    monkeypatch.setenv("WORKER_POLL_SECONDS", "5")
    monkeypatch.setenv("ORCHESTRATOR_SWEEP_SECONDS", "30")
    db = _consenting_db()
    worker.status.referrals_advanced = worker.status.ticks = 0

    asyncio.run(worker.tick(db))
    assert worker.status.referrals_advanced > 0


# --- ALLOW_LIVE_CALLS: the guard between a demo and dialling a stranger -------

def _queued_phone_action() -> tuple[MockReferralDB, str]:
    db = MockReferralDB()
    ref = "ref_1003"
    asyncio.run(db.queue_action(ref, "svc_1", voice_component.DISPATCH, "retell",
                                f"attempt:{ref}:svc_1:phone", "phone outreach"))
    return db, ref


def test_live_calls_are_withheld_by_default(monkeypatch):
    """Default OFF. 23 live services carry a real phone number and 11 have `phone` at
    priority 1, so anyone with the URL picking one of those would make Retell dial an
    actual county health department. The flag has to be opted into, never defaulted."""
    monkeypatch.delenv("ALLOW_LIVE_CALLS", raising=False)
    monkeypatch.setenv("CALL_AGENT_BASE_URL", "http://call-agent.test")
    db, _ = _queued_phone_action()

    report = asyncio.run(voice_component.run_once(db))

    assert report["state"] == "withheld"
    assert "ALLOW_LIVE_CALLS" in report["reason"]


def test_a_withheld_call_leaves_the_action_ready_not_failed(monkeypatch):
    """NOT marked failed. A failed action poisons `attempt:<ref>:<svc>:phone`
    permanently (§7c), so enabling the flag later would find nothing to re-run. Leaving
    it `ready` means the queue drains the moment calls are turned on."""
    monkeypatch.delenv("ALLOW_LIVE_CALLS", raising=False)
    db, _ = _queued_phone_action()

    asyncio.run(voice_component.run_once(db))

    assert [a["action_status"] for a in db._actions] == ["ready"]
    # ...and it is still claimable, which is the whole point.
    monkeypatch.setenv("ALLOW_LIVE_CALLS", "1")
    monkeypatch.setenv("CALL_AGENT_BASE_URL", "http://call-agent.test")
    with patch.object(voice_component, "_place_call",
                      new=AsyncMock(return_value={"call_id": "c1"})):
        assert asyncio.run(voice_component.run_once(db))["state"] == "awaiting_call_outcome"


def test_withholding_never_reaches_the_call_agent(monkeypatch):
    monkeypatch.delenv("ALLOW_LIVE_CALLS", raising=False)
    monkeypatch.setenv("CALL_AGENT_BASE_URL", "http://call-agent.test")
    db, _ = _queued_phone_action()

    place = AsyncMock()
    with patch.object(voice_component, "_place_call", new=place):
        asyncio.run(voice_component.run_once(db))
    place.assert_not_awaited()


def test_allow_live_calls_is_read_at_call_time_not_import(monkeypatch):
    """§7d — a module-level os.getenv would evaluate before load_dotenv() and report its
    default forever. Drive the env var, don't patch the attribute."""
    monkeypatch.setenv("ALLOW_LIVE_CALLS", "1")
    assert voice_component.allow_live_calls() is True
    monkeypatch.setenv("ALLOW_LIVE_CALLS", "0")
    assert voice_component.allow_live_calls() is False
