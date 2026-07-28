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

import pytest

from backend.db.mock import MockReferralDB
from backend.orchestrator import actions, backend_component, worker


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
        task = asyncio.create_task(worker.run_forever(Flaky(), poll_seconds=0.01))
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
    """The bug A2 describes, demonstrated. advance_referral queues `select_resource` to
    `backend` AFTER it has already chosen the service — so the work is done, but the open
    row makes every later call return `waiting`. Forever, with no poller."""
    db = _awaiting_selection()
    assert asyncio.run(db.advance_referral("ref_1001"))["state"] == "resource_selected"
    assert db._referrals["ref_1001"]["service_id"] == "svc_capmetro"   # already done

    for _ in range(3):
        assert asyncio.run(db.advance_referral("ref_1001"))["state"] == "waiting"


def test_servicing_the_backend_action_unblocks_the_chain():
    db = _awaiting_selection()
    asyncio.run(db.advance_referral("ref_1001"))

    report = asyncio.run(backend_component.run_once(db))
    assert report["action"] == "select_resource"
    assert report["result"]["service_id"] == "svc_capmetro"
    # and it advanced: the next step is now addressed to US
    assert report["advanced"]["state"] == "in_progress"
    assert [a["action_type"] for a in asyncio.run(db.list_ready_actions("karthik_form"))] \
        == ["prepare_online_form"]


def test_one_tick_clears_a_cross_component_chain():
    """`backend` unblocks `karthik_form` within the same tick. Draining one component
    fully before the other would need two ticks — two poll intervals of visible stall."""
    db = _awaiting_selection()
    asyncio.run(db.advance_referral("ref_1001"))

    reports = asyncio.run(worker.tick(db))
    assert [r["action"] for r in reports] == ["select_resource", "prepare_online_form"]
    assert reports[-1]["state"] == "awaiting_review"


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


def test_email_action_writes_a_shared_attempt():
    db = _consenting_db()
    asyncio.run(db.queue_action("ref_1001", "svc_capmetro", "contact_service_by_email",
                                "backend", "email:ref_1001", "email the service",
                                {"attempt_number": 2}))
    report = asyncio.run(backend_component.run_once(db))

    assert report["result"]["sent"] is True
    row = db.shared_attempts[0]
    assert row["channel"] == "email" and row["attempt_number"] == 2
    # attempts.provider is CHECK-constrained and has no `backend` value.
    assert row["provider"] == "internal"


def test_orchestrator_tick_is_opt_in(monkeypatch):
    """Opt-in because it's one of two valid designs (A3): a central tick, or every
    component advancing itself. Doing both is safe only because of the open-action
    guard, so the choice should be explicit."""
    db = _consenting_db()
    monkeypatch.setattr(worker, "ORCHESTRATOR_TICK", False)
    assert asyncio.run(worker.advance_open_referrals(db)) == []

    monkeypatch.setattr(worker, "ORCHESTRATOR_TICK", True)
    advanced = asyncio.run(worker.advance_open_referrals(db))
    assert advanced and all("advanced" in a for a in advanced)


def test_orchestrator_tick_skips_terminal_referrals(monkeypatch):
    monkeypatch.setattr(worker, "ORCHESTRATOR_TICK", True)
    db = _consenting_db()
    for referral in db._referrals.values():
        referral["status"] = "enrolled"
    assert asyncio.run(worker.advance_open_referrals(db)) == []


# --- helpers ------------------------------------------------------------------

def _age(db: MockReferralDB, action_id: str, *, seconds: int) -> None:
    """Backdate an action's updated_at so the stale sweep can see it."""
    for a in db._actions:
        if a["id"] == action_id:
            a["updated_at"] = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            return
    raise KeyError(action_id)
