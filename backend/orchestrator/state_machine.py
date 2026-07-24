"""Referral lifecycle states + transitions (CLAUDE.md §7).

The state machine owns the workflow; tools never decide it. The scheduler reads
``current_state``, dispatches at most one tool, then calls ``next_state(from_state,
outcome.status)`` to advance. Transitions key on **(from_state, status)** — the
scheduler already knows ``from_state`` from the DB, so a generic status vocabulary
("success"/"needs_human"/"failed") is enough to disambiguate.

Two milestones close the loop, and they are different signals:
  - the *service* accepts (org emails the agent back)  -> submitted -> confirmed
  - the *patient* actually uses the resource ("Y" text) -> check_in_scheduled -> completed

Both arrive as **inbound events**, not scheduler dispatches. The webhook that
receives them writes a ToolOutcome row; the scheduler applies the transition on its
next pass, so "the scheduler owns transitions" stays true.
"""

from __future__ import annotations

# --- States -----------------------------------------------------------------

CREATED = "created"
CONSENT_PENDING = "consent_pending"
CONSENT_GRANTED = "consent_granted"
OUTREACH_IN_PROGRESS = "outreach_in_progress"
SUBMITTED = "submitted"
NEEDS_HUMAN = "needs_human"
CONFIRMED = "confirmed"
CHECK_IN_SCHEDULED = "check_in_scheduled"
COMPLETED = "completed"
ESCALATED = "escalated"

TERMINAL = {COMPLETED, ESCALATED}

# States where the scheduler dispatches NOTHING and waits for an inbound webhook
# to write an outcome (patient reply / org email). Documented so nobody adds a
# polling tool here by accident.
WAITING_FOR_INBOUND = {CONSENT_PENDING, SUBMITTED, CHECK_IN_SCHEDULED}

# --- Which tool the scheduler runs at each push state ------------------------
# (Inbound-wait states are absent on purpose.) OUTREACH_IN_PROGRESS is NOT here — it
# picks one of several interchangeable submission methods per referral (below).
DISPATCH = {
    CREATED: "notify_patient",           # request consent via WhatsApp/SMS
    CONFIRMED: "notify_patient",          # schedule the utilization check-in text
}

# The three interchangeable submission methods at OUTREACH_IN_PROGRESS (§7). The
# scheduler picks ONE per referral, keyed on referrals["outreach_channel"]. All three
# write the SAME ToolOutcome shape, so the state machine treats them identically —
# this is what lets form (Form-fill), email, and phone (Voice) be built in parallel on
# different infra and still tie back together (§10). Patient messaging (notify_patient,
# Messaging) is separate: it's dispatched at CREATED/CONFIRMED, not here.
OUTREACH_TOOLS = {
    "form": "fill_form",
    "email": "send_email",
    "phone": "make_phone_call",
}
DEFAULT_OUTREACH_CHANNEL = "form"

# States the scheduler passes straight through on the same tick: no tool to run,
# no inbound to wait for — just advance and continue. `consent_granted` is the
# inbound landing state after the patient consents (§7); the moment we have it,
# outreach begins, so we step into `outreach_in_progress` where fill_form fires.
AUTO_ADVANCE = {
    CONSENT_GRANTED: OUTREACH_IN_PROGRESS,
}

# --- Transition table: (from_state, status) -> to_state ---------------------
# status is ToolOutcome.status. Inbound-driven transitions are marked [inbound].
_TRANSITIONS: dict[tuple[str, str], str] = {
    (CREATED, "success"): CONSENT_PENDING,             # consent request sent
    (CONSENT_PENDING, "success"): CONSENT_GRANTED,     # [inbound] patient consented
    (CONSENT_PENDING, "failed"): ESCALATED,            # [inbound] declined / no reply
    (CONSENT_GRANTED, "success"): OUTREACH_IN_PROGRESS,
    (OUTREACH_IN_PROGRESS, "success"): SUBMITTED,
    (OUTREACH_IN_PROGRESS, "needs_human"): NEEDS_HUMAN,
    (OUTREACH_IN_PROGRESS, "failed"): ESCALATED,
    (SUBMITTED, "success"): CONFIRMED,                 # [inbound] org email / phone accepted
    (SUBMITTED, "needs_human"): NEEDS_HUMAN,           # [inbound] phone alt-slot/ineligible -> SW acts
    (SUBMITTED, "failed"): ESCALATED,
    (CONFIRMED, "success"): CHECK_IN_SCHEDULED,        # check-in text queued
    (CHECK_IN_SCHEDULED, "success"): COMPLETED,        # [inbound] patient "Y" (used it)
    (CHECK_IN_SCHEDULED, "failed"): ESCALATED,         # [inbound] "N" / no reply -> SW follows up
    (NEEDS_HUMAN, "success"): ESCALATED,               # handed to a social worker
    (NEEDS_HUMAN, "failed"): ESCALATED,
}


def next_state(from_state: str, status: str) -> str:
    """Return the state to advance to, or ``from_state`` if no transition applies
    (idempotent: re-running a step that already advanced is a no-op)."""
    if from_state in TERMINAL:
        return from_state
    return _TRANSITIONS.get((from_state, status), from_state)
