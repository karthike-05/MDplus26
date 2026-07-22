"""Headless end-to-end of the warm path (CLAUDE.md §9, §12).

    pick patient -> auto-fill (map + validate) -> review -> submit -> capture
    confirmation -> outcome flows into the tracking loop + check-in -> completed

Runs with NO database and NO browser: the mock DB serves fixtures and the hero
form is a PDF. It exercises the real ``fill_form`` tool and the real scheduler; the
two things a headless run must stand in for a human/network are marked below:

  - **auto-review** — a person would confirm the review screen and type the one
    missing field (ref_1001's appointment time). Here we approve the proposed
    values and fill that gap programmatically.
  - **notify_patient** — Messaging's WhatsApp tool. Stubbed to a success outcome so
    the spine runs; the real tool drops in behind the same signature (§8).
  - **inbound signals** — consent, the org's acceptance email, and the patient's
    "Y" arrive as webhooks in production; here ``apply_inbound`` simulates them.

    python run_demo.py
"""

from __future__ import annotations

import asyncio

from backend.db.mock import MockReferralDB
from backend.orchestrator import scheduler
from backend.orchestrator import state_machine as sm
from backend.tools.fill_form.fill_form import prepare, submit

HERO_REFERRAL = "ref_1001"  # missing appointment_time -> a real "check this" beat


async def fill_form_tool(referral_id, db, *, attempt_id, from_state):
    """The outreach tool the scheduler dispatches at ``outreach_in_progress``.

    Wraps the real ``fill_form.submit`` with a headless auto-review: build the
    proposed values (``prepare``), fill the one field a reviewer would complete,
    then submit. In the live stack the reviewed values come from ReviewUI.jsx.
    """
    payload = await prepare(referral_id, db)
    values = dict(payload.values)
    for name in payload.needs_attention:
        if not values.get(name):
            values[name] = "10:15 AM"  # stand in for the reviewer's keystrokes
            print(f"    [auto-review] filled missing '{name}' = {values[name]!r}")
    return await submit(referral_id, values, db, attempt_id=attempt_id, from_state=from_state)


async def notify_patient_tool(referral_id, db, *, attempt_id, from_state):
    """Demo stand-in for Messaging's WhatsApp tool (§8) — success outcome only."""
    from contracts.models import ToolOutcome

    intent = "consent request" if from_state == sm.CREATED else "utilization check-in"
    print(f"    [notify_patient] sent {intent} (WhatsApp, stubbed)")
    outcome = ToolOutcome(
        referral_id=referral_id,
        channel="whatsapp",
        status="success",
        attempt_id=attempt_id,
        from_state=from_state,
        data={"intent": intent},
    )
    await db.record_attempt(outcome)
    return outcome


TOOLS = {"fill_form": fill_form_tool, "notify_patient": notify_patient_tool}


async def _state(db, ref) -> str:
    return (await db.get_referral(ref))["current_state"]


async def _drive(db, ref):
    """Run the scheduler until it's terminal or waiting, printing each transition."""
    before = await _state(db, ref)
    outcomes = await scheduler.run(ref, db, TOOLS)
    after = await _state(db, ref)
    if outcomes or after != before:
        conf = outcomes[-1].data if outcomes else {}
        detail = ""
        if "output_path" in conf:
            detail = f"  -> {conf['output_path']}"
        print(f"  {before}  ==>  {after}{detail}")
    return after


async def main() -> None:
    db = MockReferralDB()
    ref = HERO_REFERRAL

    # Start the hero referral at the very beginning so the whole loop is on camera.
    await db.set_state(ref, sm.CREATED)
    referral = await db.get_referral(ref)
    patient = await db.get_patient(referral["patient_id"])

    print("=" * 70)
    print("Catalyst-26 — referral-to-completion, headless warm path")
    print("=" * 70)
    print(f"Patient : {patient['name']}")
    print(f"Service : {referral['service_name']}  ({referral['form_id']})")
    print(f"Start   : {await _state(db, ref)}\n")

    print("[1] scheduler: request consent")
    await _drive(db, ref)  # created -> consent_pending (then waits)

    print("\n[2] inbound: patient consents (WhatsApp 'YES')")
    await scheduler.apply_inbound(ref, db, status="success", channel="whatsapp")
    await _drive(db, ref)  # consent_granted -> outreach_in_progress -> submitted

    print("\n[3] inbound: service accepts (org emails the agent back)")
    await scheduler.apply_inbound(ref, db, status="success", channel="email")
    await _drive(db, ref)  # confirmed -> check_in_scheduled (then waits)

    print("\n[4] inbound: patient replies 'Y' to the check-in (used the resource)")
    await scheduler.apply_inbound(ref, db, status="success", channel="whatsapp")
    await _drive(db, ref)  # check_in_scheduled -> completed

    final = await _state(db, ref)
    print("\n" + "=" * 70)
    print(f"Final state: {final}   {'✅ loop closed' if final == sm.COMPLETED else '⚠️'}")
    print(f"Attempts recorded: {len(db.attempts)}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
