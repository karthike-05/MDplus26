"""Regressions for the review-UI submit path against the LIVE adapters.

Every bug below was found by actually walking a referral through the deployed bus on
2026-08-01 and none of them could have been caught by the existing suite, because the
suite exercises the *worker* path (`actions.py` -> `record_shared_attempt`) and the
review UI is the only caller that reaches the other one. The whole path had never been
run against a real database.

No network here: the live adapters are instantiated but their HTTP/asyncpg clients are
never created, since every method under test either returns before touching one or is
driven through a fake. L1 by CLAUDE.md §9.
"""

from __future__ import annotations

import asyncio

import pytest

from contracts.models import ToolOutcome
from backend.db.supabase import ATTEMPT_COLS, SupabaseReferralDB
from backend.db.supabase_api import SupabaseAPIReferralDB
from backend.orchestrator import actions


def _outcome() -> ToolOutcome:
    return ToolOutcome(
        referral_id="ref_1", channel="form", status="success",
        attempt_id="att_1", from_state="outreach_in_progress",
        data={"target": "pdf"}, error=None,
    )


# --- 1. record_attempt on the live adapters ----------------------------------

@pytest.mark.parametrize("adapter", [SupabaseAPIReferralDB, SupabaseReferralDB])
def test_record_attempt_is_a_noop_on_live_adapters(adapter):
    """It must not write, and — the actual bug — must not raise.

    `attempt_id` and `from_state` both map to None in ATTEMPT_COLS, so the old body
    built a row whose keys collapsed onto a single literal `None` and passed
    `on_conflict=None`. PostgREST answered PGRST204 "Could not find the 'null' column of
    'attempts'" on EVERY call, which made `POST /api/submit` a guaranteed 500 live.

    A no-op is the right behaviour, not merely a safe one: live, the same event is
    written by `record_shared_attempt` in their vocabulary, and a second row would be
    counted by `advance_referral`'s per-channel exhaustion test.
    """
    db = adapter.__new__(adapter)          # no __init__: never opens a client
    assert asyncio.run(db.record_attempt(_outcome())) is None


def test_attempt_id_and_from_state_still_have_no_live_column():
    """Guards the premise of the no-op above. If someone adds these columns, the
    no-op should be revisited rather than silently continuing to drop data."""
    assert ATTEMPT_COLS["attempt_id"] is None
    assert ATTEMPT_COLS["from_state"] is None


def test_direction_and_purpose_are_readable_but_never_written():
    """The timeline needs `direction` to tell "we sent" from "the patient replied"
    (Messaging writes status='sent' outbound and 'delivered' inbound). Both are
    read-only projections — every write path lists its columns explicitly, so adding
    them to the map must not put them into an INSERT."""
    assert ATTEMPT_COLS["direction"] == "direction"
    assert ATTEMPT_COLS["purpose"] == "purpose"


# --- 2. a successful submit is PENDING, not concluded ------------------------

def test_success_maps_to_sent_so_the_referral_waits_for_the_org():
    """`advance_referral` parks a referral at `waiting_for_response` only while some
    attempt has status in (queued, started, sent, delivered); otherwise it treats the
    channel as spent and moves to `try_next_resource`.

    Mapping our `success` to 'completed' therefore made a SUCCESSFUL submission look
    like an exhausted channel — live, the referral abandoned the service it had just
    applied to, one second after applying. `outcome` must stay 'submitted': submitting
    is not the org accepting (§7f), and collapsing the two milestones is the one thing
    the product exists not to do.
    """
    status, outcome = actions.STATUS_TO_THEIRS["success"]
    assert status == "sent"
    assert outcome == "submitted"

    pending = {"queued", "started", "sent", "delivered"}
    assert status in pending, "a submitted application must read as pending"


def test_no_mapping_ever_claims_enrollment():
    """§7f — only the org's own answer may write outcome='enrolled'."""
    assert "enrolled" not in {outcome for _, outcome in actions.STATUS_TO_THEIRS.values()}


def test_attempt_row_carries_the_pending_status():
    referral = {"service_id": "svc_1"}
    row = actions.attempt_row(referral, _outcome(), "pdf", 1, "online_form")
    assert (row["status"], row["outcome"]) == ("sent", "submitted")
    assert row["channel"] == "online_form"
    assert row["attempt_number"] == 1          # NOT NULL, no default


# --- 3. the review UI must close the action it just answered -----------------

class _FakeDB:
    """Just enough surface for `_close_open_form_action`."""

    def __init__(self, actions_):
        self.actions = actions_
        self.closed: list[tuple[str, str]] = []

    async def list_actions(self, referral_id=None, limit=50):
        return self.actions

    async def set_action_status(self, action_id, status, *, result=None):
        self.closed.append((action_id, status))


def _close(monkeypatch, fake) -> None:
    from backend import main

    monkeypatch.setattr(main, "db", fake)
    asyncio.run(main._close_open_form_action("ref_1", "success"))


def test_submit_closes_the_blocked_prepare_action(monkeypatch):
    """The worker leaves `prepare_online_form` **blocked** awaiting a human, and this
    route is that human. Leaving it open means `advance_referral`'s first guard ("any
    open action -> waiting") freezes the referral on the submit that just succeeded —
    verified live before the fix: a 200 submit returned
    {"state": "waiting", "reason": "An action is already open"}."""
    fake = _FakeDB([{"id": "a1", "action_type": "prepare_online_form",
                     "action_status": "blocked"}])
    _close(monkeypatch, fake)
    assert fake.closed == [("a1", "completed")]


def test_submit_leaves_other_components_actions_alone(monkeypatch):
    fake = _FakeDB([
        {"id": "a1", "action_type": "contact_service_by_phone", "action_status": "ready"},
        {"id": "a2", "action_type": "confirm_consent", "action_status": "ready"},
    ])
    _close(monkeypatch, fake)
    assert fake.closed == []


def test_submit_does_not_reopen_an_already_finished_action(monkeypatch):
    """Closing a `completed` row again is harmless here, but re-queueing under its
    dedup key never works (§7c) — so the filter is on OPEN statuses only."""
    fake = _FakeDB([{"id": "a1", "action_type": "prepare_online_form",
                     "action_status": "completed"}])
    _close(monkeypatch, fake)
    assert fake.closed == []


def test_closing_never_raises_into_the_submit(monkeypatch):
    """The injection already happened and is recorded. A bookkeeping failure must not
    tell the social worker their submit failed."""
    class _Boom(_FakeDB):
        async def list_actions(self, referral_id=None, limit=50):
            raise RuntimeError("postgrest down")

    _close(monkeypatch, _Boom([]))          # must not propagate


# --- 4. ranking's degrade path must not itself violate a NOT NULL -----------

def test_unfiltered_fallback_writes_a_non_null_candidate_score():
    """`referral_service_candidates.score` is NOT NULL on the live DB.

    The fallback exists so a referral never parks on a poisoned `rank:<id>` dedup key
    when scoring fails (§7c). Writing score=None made the fallback ITSELF raise, so the
    500 it was written to prevent happened anyway — and because `ranking_results.score`
    IS nullable, the live symptom was 59 ranking_results rows, zero candidates, and a
    bare 500 (af536831, 2026-08-01).

    Asserted on the source rather than by running the pipeline: `ranking.py` imports a
    module-level `db` bound to a real Supabase client at import time, so exercising
    `_run_unfiltered_fallback` here would need a live connection.
    """
    import ast
    import pathlib

    src = pathlib.Path("backend/service_ranking/ranking.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_unfiltered_fallback")

    scores = [
        value
        for node in ast.walk(fn)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value == "score"
    ]
    assert scores, "expected the fallback to build candidate rows with a 'score' key"
    for value in scores:
        assert not (isinstance(value, ast.Constant) and value.value is None), (
            "referral_service_candidates.score is NOT NULL — a null here makes the "
            "degrade path raise, which is the exact failure it exists to prevent"
        )


# --- 5. F1: a validation bounce is not an outreach attempt -------------------

class _SubmitDB(_FakeDB):
    """Records what post_submit would write to the shared bus."""

    def __init__(self, actions_):
        super().__init__(actions_)
        self.shared_attempts: list[dict] = []
        self.advanced = 0

    async def get_referral(self, referral_id):
        return {"id": referral_id, "patient_id": "p1", "form_id": "transport_intake",
                "service_id": "svc_1", "status": "in_progress", "outreach_channel": "online_form"}

    async def get_form_schema(self, form_id):
        from backend.db.mock import SCHEMA_DIR, _load_schemas
        return _load_schemas(SCHEMA_DIR)[form_id]

    async def next_attempt_number(self, referral_id, service_id):
        return len(self.shared_attempts) + 1

    async def record_shared_attempt(self, row):
        self.shared_attempts.append(row)

    async def advance_referral(self, referral_id):
        self.advanced += 1
        return {"state": "advanced"}


def _post_submit(monkeypatch, fake, outcome_status):
    """Drive post_submit with a stubbed submit() returning `outcome_status`."""
    from backend import main
    from contracts.models import ToolOutcome

    async def fake_submit(referral_id, values, db, **kw):
        return ToolOutcome(referral_id=referral_id, channel="form",
                           status=outcome_status, attempt_id="att_1",
                           data={"problems": {"appointment_date": ["bad date format"]}}
                                if outcome_status == "needs_human" else {})

    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "submit", fake_submit)
    monkeypatch.setattr(main, "_owns_transitions", lambda: False)
    return asyncio.run(main.post_submit("ref_1", main.ReviewedValues(values={})))


def test_validation_bounce_leaves_the_action_open_for_the_reviewer(monkeypatch):
    """F1. `needs_human` means re-validation rejected a value: nothing was injected and
    nothing reached the service. Closing the action as `failed` — what this used to do —
    poisoned attempt:<referral>:<service>:online_form permanently (§7c), so the
    corrected resubmit could never be re-queued."""
    fake = _SubmitDB([{"id": "a1", "action_type": "prepare_online_form",
                       "action_status": "blocked"}])

    _post_submit(monkeypatch, fake, "needs_human")

    assert fake.closed == [], "the reviewer isn't finished — leave it blocked"


def test_validation_bounce_does_not_spend_an_outreach_attempt(monkeypatch):
    """advance_referral counts shared attempts for the three-attempt cap AND reads
    'is there an attempt on this channel' as channel-exhausted. Recording a bounce
    would burn a real attempt on a form that was never sent, and step 9 would then move
    the referral off the service it was about to apply to."""
    fake = _SubmitDB([{"id": "a1", "action_type": "prepare_online_form",
                       "action_status": "blocked"}])

    _post_submit(monkeypatch, fake, "needs_human")

    assert fake.shared_attempts == []
    assert fake.advanced == 0, "nothing changed, so nothing to advance"


def test_a_successful_submit_still_records_closes_and_advances(monkeypatch):
    """The other side of F1 — the fix must not disarm the happy path."""
    fake = _SubmitDB([{"id": "a1", "action_type": "prepare_online_form",
                       "action_status": "blocked"}])

    _post_submit(monkeypatch, fake, "success")

    assert len(fake.shared_attempts) == 1
    assert fake.closed == [("a1", "completed")]
    assert fake.advanced == 1
