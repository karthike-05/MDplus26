"""The action-queue seam: how our component joins the shared orchestration bus.

The live DB owns a scheduler — `advance_referral()` (plpgsql) — which decides the next
step for a referral and queues a row in `referral_actions` addressed to a component
(`backend` / `karthik_form` / `retell` / `twilio` / `social_worker`). We are
**`karthik_form`**. So our job here is not to decide the workflow; it is to:

    claim a ready action  ->  do the form work  ->  write an `attempts` row
                          ->  mark the action done  ->  call advance_referral()

which is the same contract Messaging already fulfils for `twilio`. This module holds
that loop plus the vocabulary translation, and nothing else — the decisions live in
the DB function (live) or its mirror in `MockReferralDB` (offline), never here.

WHY A SEPARATE FILE FROM scheduler.py: `scheduler.py` is our own state machine, which
predates the DB one and still drives the offline demo end-to-end. Both exist on
purpose while the DB flow is unblocked (see docs/integration-status.md); this file is
the path that integrates with the other three services.

TWO FORM COMPONENTS, ONE SEAM. Form-filling has two halves: the **PDF** component
(built — `fill_form` + PdfInjector) and the **online application** component (not yet
built — a real service's web form). Our `prepare`/review/`submit` flow is already
target-agnostic (§6: the Injector is chosen by `schema.target_type`), so both halves
enter through the same action types. The mismatch is in the *channel* vocabulary, not
the flow — see CHANNEL_FOR_TARGET.
"""

from __future__ import annotations

from backend.db.interface import ReferralDB

# Who we are on the bus (`referral_actions.assigned_component`, `attempts.provider`).
COMPONENT = "karthik_form"

# `referral_actions.action_type` values addressed to us. The DB's enum also contains
# `verify_form_mapping` (the cold path — schema extraction, §13) and
# `collect_missing_information`; both are deferred, so an action of that type is left
# `ready` for a human rather than silently completed.
PREPARE = "prepare_online_form"
SUBMIT = "submit_online_form"
HANDLED = (PREPARE, SUBMIT)

# --- Vocabulary translation --------------------------------------------------
# Our ToolOutcome vocabulary is deliberately small (§5b: one status set for every
# channel). The shared `attempts` table splits the same information across two
# constrained columns, `status` (transport-level) and `outcome` (what it achieved),
# and the ranker reads `outcome`. So one of ours maps to a PAIR of theirs.
#
# ⚠ `success` MAPS TO 'sent', NOT 'completed', AND THE DIFFERENCE IS THE WHOLE LOOP.
# `advance_referral` decides whether a referral is waiting on anyone with:
#
#     if exists(select 1 from attempts where ... status in
#               ('queued','started','sent','delivered'))
#        then status='waiting_for_response'
#
# and only if that misses does it fall through to "this channel is used up, try the next
# resource". A submitted application is *pending*, not concluded — the org hasn't
# answered yet (§7f: submitting is not accepting). Writing 'completed' made a SUCCESSFUL
# submit look like an exhausted channel, so the demo service (one channel) was instantly
# marked `exhausted` and the referral abandoned the service it had just applied to.
# Verified live 2026-08-01: advance_referral returned `try_next_resource` straight after
# a 200 submit. With 'sent' it parks at `waiting_for_response`, which is exactly our
# `submitted` state (§7, WAITING_FOR_INBOUND) — and `POST /api/org/response` is what
# moves it on. `outcome` is untouched: the ranker still reads 'submitted'.
STATUS_TO_THEIRS = {
    "success":     ("sent",      "submitted"),
    "needs_human": ("completed", "needs_human_followup"),
    "failed":      ("failed",    "technical_failure"),
}

# `attempts.channel` allows email / online_form / phone / sms / whatsapp — there is NO
# value for "a filled PDF". Absent a dispatched channel we fall back to how the document
# reaches the service: a pdf target as `email`, a web target as `online_form`.
#
# THIS IS ONLY A FALLBACK, and getting that wrong stalls a referral silently. When
# `advance_referral` dispatches the work it names the channel it chose (from
# `service_application_channels`) in the action's `input_payload.channel`, and its
# exhaustion test at step 9 asks "is there an attempt whose channel equals this
# configured channel". Record a PDF submitted through an `online_form` channel as
# `email` and that test never matches: step 10 re-picks `online_form`, but the dedup key
# `attempt:<referral>:<service>:online_form` is unchanged, so `queue_referral_action`'s
# ON CONFLICT returns the ALREADY-COMPLETED action instead of queueing a new one. No new
# work, no error, no progress — the referral sits at `in_progress` forever.
#
# So: always record the channel that was dispatched, when one was.
CHANNEL_FOR_TARGET = {"pdf": "email", "web": "online_form"}

# `attempts.channel` is CHECK-constrained to their five values. A dispatched channel read
# from `service_application_channels` is already one of them, but a referral's
# `outreach_channel` may still be OUR vocabulary (the fixtures say `form` / `text`), so
# normalise before writing. Anything unrecognised returns None and lets the caller fall
# back rather than failing the insert on a CHECK violation.
THEIR_CHANNELS = ("online_form", "phone", "email", "sms", "whatsapp")
OUR_CHANNEL_TO_THEIRS = {"form": "online_form", "text": "sms"}


def normalize_channel(channel: str | None) -> str | None:
    if not channel:
        return None
    channel = OUR_CHANNEL_TO_THEIRS.get(channel, channel)
    return channel if channel in THEIR_CHANNELS else None


def attempt_row(referral: dict, outcome, target_type: str, attempt_number: int = 1,
                channel: str | None = None) -> dict:
    """Our ToolOutcome -> a shared `attempts` row.

    `structured_result` is jsonb NOT NULL, so it always gets a dict. Our free-text
    error goes to `notes`; their `outcome` is what the ranker's responsiveness score
    reads, which is why it must be set and not left to a default.

    `attempt_number` is likewise NOT NULL with **no default**, and the table carries a
    UNIQUE (referral_id, service_id, attempt_number) — omitting it doesn't fall back to
    1, it fails the insert. The caller supplies the next free number (the action's own
    `input_payload.attempt_number` when `advance_referral` set one, else counted).
    """
    status, their_outcome = STATUS_TO_THEIRS[outcome.status]
    return {
        "referral_id": outcome.referral_id,
        "service_id": referral.get("service_id"),
        "attempt_number": attempt_number,
        "channel": normalize_channel(channel)
                   or CHANNEL_FOR_TARGET.get(target_type, "online_form"),
        "provider": COMPONENT,
        "direction": "outbound",
        "status": status,
        "outcome": their_outcome,
        "structured_result": dict(outcome.data or {}),
        "notes": outcome.error,
    }


# --- The worker --------------------------------------------------------------

async def run_once(db: ReferralDB, *, submit_values: dict | None = None) -> dict | None:
    """Claim and service ONE ready action addressed to us. Returns a small report, or
    None when the queue holds nothing for us.

    `prepare_online_form` deliberately does NOT submit: it maps + validates and leaves
    the referral at the human review gate (§2/§12 — form outreach is human-gated). The
    reviewer's approval arrives later as `submit_online_form` carrying the confirmed
    values, which is when we inject and write the attempt.

    Never raises for a failure *servicing* an action: the action is marked `failed` and
    the error returned in the report. An action left `in_progress` by a raised exception
    would trip `advance_referral`'s open-action guard and deadlock its referral
    permanently — so the failure has to be recorded, not propagated. Only the queue read
    itself is allowed to raise, since there's no action to blame yet.
    """
    actions = await db.list_ready_actions(COMPONENT)
    action = next((a for a in actions if a.get("action_type") in HANDLED), None)
    if action is None:
        return None

    action_id, referral_id = action["id"], action["referral_id"]
    await db.set_action_status(action_id, "in_progress")
    try:
        return await _service(db, action, submit_values)
    except Exception as exc:                       # noqa: BLE001 — see docstring
        await db.set_action_status(action_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return {"action": action.get("action_type"), "referral_id": referral_id,
                "state": "failed", "error": f"{type(exc).__name__}: {exc}"}


async def _service(db: ReferralDB, action: dict, submit_values: dict | None) -> dict:
    """Do the form work for one already-claimed action. Split out so run_once can wrap
    exactly this in the failure handler above."""
    from backend.tools.fill_form.fill_form import prepare, submit

    action_id, referral_id = action["id"], action["referral_id"]
    referral = await db.get_referral(referral_id)
    schema = await db.get_form_schema(referral["form_id"])

    if action["action_type"] == PREPARE:
        payload = await prepare(referral_id, db)
        # Not an attempt: nothing was sent. The action stays open until a human
        # approves, so advance_referral keeps returning "waiting" rather than
        # racing ahead — its own guard is `count(open actions) > 0`.
        await db.set_action_status(
            action_id, "blocked",
            result={"awaiting": "human_review",
                    "needs_attention": payload.needs_attention,
                    "pending_human": payload.pending_human},
        )
        return {"action": PREPARE, "referral_id": referral_id, "state": "awaiting_review",
                "review": payload.model_dump()}

    # SUBMIT — the reviewer approved; inject and record a real attempt.
    payload = action.get("input_payload") or {}
    values = submit_values if submit_values is not None else payload.get("values", {})
    outcome = await submit(
        referral_id, values, db,
        attempt_id=f"{referral_id}:{action_id}",   # idempotent per action (§10)
        from_state=referral.get("current_state"),
    )
    # `advance_referral` stamps attempt_number onto the action it dispatched; a submit
    # action we queued ourselves may not carry one, so fall back to counting.
    attempt_no = payload.get("attempt_number") or await db.next_attempt_number(
        referral_id, referral.get("service_id"))
    # The channel the DB dispatched, not the one implied by the document format — see
    # CHANNEL_FOR_TARGET. `prepare` and `submit` are separate actions, so a submit we
    # queued ourselves may not carry it; the referral's resolved channel is the same
    # value, read from the same `service_application_channels` row.
    channel = payload.get("channel") or referral.get("outreach_channel")
    await db.record_shared_attempt(
        attempt_row(referral, outcome, schema.target_type, attempt_no, channel))
    await db.set_action_status(
        action_id,
        "completed" if outcome.status == "success" else "failed",
        result=dict(outcome.data or {}), error=outcome.error,
    )
    # Hand control back to the DB scheduler, which picks the next step.
    advanced = await db.advance_referral(referral_id)
    return {"action": SUBMIT, "referral_id": referral_id,
            "status": outcome.status, "advanced": advanced}
