"""The scheduler — the only place transitions happen (CLAUDE.md §7).

Read ``current_state`` -> pick exactly one tool (or none, if waiting) -> run it ->
advance via ``next_state(from_state, status)``. Keep this loop small and readable;
it is the spine of the demo.

Design invariants it enforces:
  - **Tools never decide the workflow.** A tool records a ``ToolOutcome`` and
    returns; the scheduler reads that outcome's ``status`` and advances the state.
  - **Inbound events don't break "the scheduler owns transitions."** A webhook
    (patient consent, org email, patient "Y") writes a ``ToolOutcome`` via
    ``apply_inbound`` and the state advances there too — same code path, so there's
    one place that mutates ``current_state``.
  - **Idempotency (§10).** ``attempt_id`` is derived deterministically from
    ``(referral_id, from_state, attempt_no)``, so a re-run upserts the same
    ``outreach_attempts`` row instead of duplicating it.

Tools are injected as a ``{name: callable}`` map so the scheduler stays decoupled
from any concrete tool. Each callable has the signature::

    async def tool(referral_id, db, *, attempt_id, from_state) -> ToolOutcome
"""

from __future__ import annotations

import hashlib
from typing import Awaitable, Callable

from contracts.models import ToolOutcome
from backend.db.interface import ReferralDB
from backend.orchestrator import state_machine as sm

Tool = Callable[..., Awaitable[ToolOutcome]]
Tools = dict[str, Tool]


def attempt_id_for(referral_id: str, from_state: str, attempt_no: int = 1) -> str:
    """Deterministic idempotency key (§10): same dispatch -> same id -> upsert."""
    digest = hashlib.sha1(f"{referral_id}:{from_state}:{attempt_no}".encode()).hexdigest()
    return f"att_{digest[:16]}"


def tool_name_for(state: str, referral: dict) -> str | None:
    """Which tool to dispatch at ``state`` for this referral. Most push states map
    1:1 (DISPATCH); OUTREACH_IN_PROGRESS picks one submission method per referral,
    keyed on ``outreach_channel`` (defaults to form) — see state_machine.OUTREACH_TOOLS."""
    if state == sm.OUTREACH_IN_PROGRESS:
        channel = referral.get("outreach_channel") or sm.DEFAULT_OUTREACH_CHANNEL
        return sm.OUTREACH_TOOLS.get(channel)
    return sm.DISPATCH.get(state)


async def tick(referral_id: str, db: ReferralDB, tools: Tools) -> ToolOutcome | None:
    """Advance a referral by one step. Returns the ``ToolOutcome`` a tool produced,
    or ``None`` when the scheduler did nothing (terminal, or waiting for inbound).

    Auto-advance states (e.g. ``consent_granted``) are stepped through without a
    tool run and without returning — call ``run`` to drive until blocked.
    """
    referral = await db.get_referral(referral_id)
    state = referral["current_state"]

    if state in sm.TERMINAL:
        return None
    if state in sm.AUTO_ADVANCE:
        await db.set_state(referral_id, sm.AUTO_ADVANCE[state])
        return None
    if state in sm.WAITING_FOR_INBOUND:
        return None  # a webhook will call apply_inbound; nothing to dispatch

    tool_name = tool_name_for(state, referral)
    if tool_name is None:
        return None  # no tool wired for this push state
    tool = tools.get(tool_name)
    if tool is None:
        # Deliberately unregistered — e.g. the API omits `fill_form` so form-channel
        # outreach stops here and waits for the human review screen, while phone/text
        # stubs (registered) auto-advance. Not an error; just nothing to dispatch.
        return None

    attempt_id = attempt_id_for(referral_id, state)
    outcome = await tool(referral_id, db, attempt_id=attempt_id, from_state=state)
    await db.set_state(referral_id, sm.next_state(state, outcome.status))
    return outcome


async def run(referral_id: str, db: ReferralDB, tools: Tools) -> list[ToolOutcome]:
    """Tick until the referral is terminal or waiting for an inbound signal.

    Stops on a non-``success`` outcome too — ``needs_human`` / ``failed`` route to a
    human (``needs_human`` / ``escalated``), not to another auto-dispatch.
    """
    outcomes: list[ToolOutcome] = []
    while True:
        before = (await db.get_referral(referral_id))["current_state"]
        if before in sm.TERMINAL or before in sm.WAITING_FOR_INBOUND:
            break
        outcome = await tick(referral_id, db, tools)
        if outcome is not None:
            outcomes.append(outcome)
            if outcome.status != "success":
                break
        else:
            # No outcome: either we auto-advanced (state changed, keep going) or
            # there was nothing to do (state unchanged -> stop, avoid a spin loop).
            after = (await db.get_referral(referral_id))["current_state"]
            if after == before:
                break
    return outcomes


async def apply_inbound(
    referral_id: str,
    db: ReferralDB,
    *,
    status: str,
    channel: str,
    attempt_no: int = 1,
    data: dict | None = None,
) -> ToolOutcome:
    """Record an inbound signal and advance the state (§7).

    The webhook layer calls this for patient consent, the org's acceptance email,
    and the patient's utilization "Y". It writes a ``ToolOutcome`` (so inbound
    events look exactly like tool runs to the dashboard) and applies the transition
    through the same ``next_state`` the scheduler uses.
    """
    referral = await db.get_referral(referral_id)
    from_state = referral["current_state"]
    outcome = ToolOutcome(
        referral_id=referral_id,
        channel=channel,
        status=status,
        attempt_id=attempt_id_for(referral_id, from_state, attempt_no),
        from_state=from_state,
        data=data or {},
    )
    await db.record_attempt(outcome)
    await db.set_state(referral_id, sm.next_state(from_state, status))
    return outcome
