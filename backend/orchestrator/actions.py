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
STATUS_TO_THEIRS = {
    "success":     ("completed", "submitted"),
    "needs_human": ("completed", "needs_human_followup"),
    "failed":      ("failed",    "technical_failure"),
}

# `attempts.channel` allows email / online_form / phone / sms / whatsapp — there is NO
# value for "a filled PDF". The PDF component produces an application that reaches the
# service as an email attachment, so a pdf target is recorded as `email`; a web target
# is a true `online_form`. If Data adds a dedicated value, change it here only.
CHANNEL_FOR_TARGET = {"pdf": "email", "web": "online_form"}


def attempt_row(referral: dict, outcome, target_type: str) -> dict:
    """Our ToolOutcome -> a shared `attempts` row.

    `structured_result` is jsonb NOT NULL, so it always gets a dict. Our free-text
    error goes to `notes`; their `outcome` is what the ranker's responsiveness score
    reads, which is why it must be set and not left to a default.
    """
    status, their_outcome = STATUS_TO_THEIRS[outcome.status]
    return {
        "referral_id": outcome.referral_id,
        "service_id": referral.get("service_id"),
        "channel": CHANNEL_FOR_TARGET.get(target_type, "online_form"),
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
    """
    from backend.tools.fill_form.fill_form import prepare, submit

    actions = await db.list_ready_actions(COMPONENT)
    action = next((a for a in actions if a.get("action_type") in HANDLED), None)
    if action is None:
        return None

    action_id, referral_id = action["id"], action["referral_id"]
    await db.set_action_status(action_id, "in_progress")
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
    values = submit_values if submit_values is not None else (action.get("input_payload") or {}).get("values", {})
    outcome = await submit(
        referral_id, values, db,
        attempt_id=f"{referral_id}:{action_id}",   # idempotent per action (§10)
        from_state=referral.get("current_state"),
    )
    await db.record_shared_attempt(attempt_row(referral, outcome, schema.target_type))
    await db.set_action_status(
        action_id,
        "completed" if outcome.status == "success" else "failed",
        result=dict(outcome.data or {}), error=outcome.error,
    )
    # Hand control back to the DB scheduler, which picks the next step.
    advanced = await db.advance_referral(referral_id)
    return {"action": SUBMIT, "referral_id": referral_id,
            "status": outcome.status, "advanced": advanced}
