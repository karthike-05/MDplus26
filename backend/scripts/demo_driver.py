"""Diagnose — and if you must, unstick — the live four-service loop.

Two jobs, in order of how often you should use them:

1. **`--diagnose` (default).** Walk every live referral, work out exactly what
   `advance_referral()` will do with it next, and name whatever is stopping it. Nothing
   is written. This is the thing to run before a group walkthrough: it turns "the board
   isn't moving" into "referral X is parked at step 6 because Ranking hasn't written
   candidates, and referral Y's service has no channel row".

2. **`--bridge-candidates` — a SHIM, off by default.** Copies `ranking_results` rows
   that passed the hard filter into `referral_service_candidates`.

   Ranking SHIPPED this properly on 2026-07-28 (`rank_referral()` writes candidates,
   closes the action, advances) — but their Railway service still runs the old code
   until they redeploy, and they built no poller, so nothing triggers a run either way.
   Until both land, this bridges data their pipeline has *already computed*: the rows
   exist in `ranking_results`, scored, ranked and filtered. It isn't inventing a
   ranking. **Delete it once they redeploy.**

3. **`--enable-form-channel` / `--reset-selection` — demo setup.** Give a service an
   `online_form` channel so referrals can route to the form component (A11), and clear a
   referral's pre-seeded `service_id` so the SW selection gate actually fires. The gate
   only asks when nothing is chosen, and the live transport referral was seeded with a
   service already attached — not chosen by a social worker — so clearing it restores
   the state the flow is meant to start from.

Every write is dry-run first. `--yes` is the only thing that makes this touch the shared
database, and it prints what it would write before writing it.

    python -m backend.scripts.demo_driver                       # diagnose (read-only)
    python -m backend.scripts.demo_driver --bridge-candidates   # show what it WOULD write
    python -m backend.scripts.demo_driver --bridge-candidates --yes    # actually write

Needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys

OPEN = ("ready", "in_progress", "blocked")
TERMINAL = ("enrolled", "failed", "escalated")

# Components that have a poller as of 2026-07-27. An open action for anything else is a
# deadlock in waiting, because advance_referral's first guard is "any open action -> wait".
POLLED = {"karthik_form", "backend", "twilio"}

# ...except `social_worker`, where a *human* is the poller by design (§7b): the dashboard's
# "Choose service" screen completes the action. Calling that deadlocked reads as a broken
# demo when it is the demo, so it gets its own label rather than the skull.
HUMAN_POLLED = {"social_worker": 'the dashboard\'s "Choose service" screen completes it'}


def _client():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        sys.exit("SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY must be set.")
    from supabase import create_client

    return create_client(url, key)


def _rows(c, table, **eq):
    q = c.table(table).select("*")
    for k, v in eq.items():
        q = q.eq(k, v)
    return q.execute().data or []


def diagnose(c) -> None:
    referrals = _rows(c, "referrals")
    services = {s["id"]: s["name"] for s in _rows(c, "services")}
    print(f"{len(referrals)} referral(s)\n" + "=" * 78)

    for r in referrals:
        rid = r["id"]
        patient = (_rows(c, "patients", id=r["patient_id"]) or [{}])[0]
        actions = _rows(c, "referral_actions", referral_id=rid)
        open_actions = [a for a in actions if a["action_status"] in OPEN]
        candidates = _rows(c, "referral_service_candidates", referral_id=rid)
        ranked = [x for x in _rows(c, "ranking_results", referral_id=rid)
                  if x.get("passed_hard_filter")]
        attempts = _rows(c, "attempts", referral_id=rid)

        print(f"\n{patient.get('name', '?')}  ({rid[:8]})")
        print(f"  status={r['status']}  consent={patient.get('consent_status')}  "
              f"service={services.get(r.get('service_id'), '—')}")
        print(f"  candidates={len(candidates)}  ranked(passing)={len(ranked)}  "
              f"attempts={len(attempts)}  open actions={len(open_actions)}")

        # Replicate advance_referral's branch order (verified against the deployed
        # function 2026-07-27) so the verdict matches what the DB will actually do.
        if open_actions:
            for a in open_actions:
                comp = a["assigned_component"]
                if comp in HUMAN_POLLED:
                    note = f"   ⏸ awaiting a human — {HUMAN_POLLED[comp]}"
                elif comp not in POLLED:
                    note = "   ⛔ NOBODY POLLS THIS — deadlocked"
                else:
                    note = ""
                print(f"  → WAITING on {a['action_type']} [{a['action_status']}] "
                      f"-> {comp}{note}")
            continue

        if r["status"] in TERMINAL:
            print(f"  → terminal ({r['status']}); "
                  f"outcome={r.get('completion_outcome') or '—'}")
            continue
        if patient.get("consent_status") != "confirmed":
            print("  → will queue confirm_consent -> twilio (consent gate)")
            continue
        if not candidates:
            print("  → will park at status='ranking' and queue rank_resources -> backend")
            print("     Ranking writes candidates now (03e21fc), but nothing TRIGGERS a run:"
                  " no poller by design, and BACKEND_CLAIM_RANKING is off (whats-left A1b).")
            if ranked:
                print(f"     {len(ranked)} ranking_results rows already passed the hard "
                      f"filter and can be bridged with --bridge-candidates.")
            continue

        # The SW gate (003_sw_selection_gate.sql) answers before the old auto-picker.
        if not r.get("service_id"):
            chosen = [c for c in candidates if c.get("selected")]
            if chosen:
                print(f"  → SW chose rank {chosen[0]['rank']}; will adopt it and dispatch")
                continue
            usable = [c for c in candidates if c["candidate_status"] == "available"
                      and c["eligibility_state"] in
                      ("eligible", "potentially_eligible", "unknown")]
            if usable:
                print(f"  → ⏸ AWAITING SOCIAL WORKER: {len(usable)} option(s) ranked, none "
                      f"chosen. Queues select_resource -> social_worker; the dashboard's "
                      f"\"Choose service\" screen completes it.")
            else:
                print("  → no candidate available -> will escalate to a social worker")
            continue

        chans = _rows(c, "service_application_channels", service_id=r["service_id"])
        tried = {a["channel"] for a in attempts if a.get("service_id") == r["service_id"]}
        unused = [ch for ch in chans if ch["channel"] not in tried]
        if not chans:
            print("  → ⛔ service has NO service_application_channels row: "
                  "advance_referral treats it as exhausted immediately and queues "
                  "try_next_resource -> backend")
        elif not unused:
            print(f"  → every channel tried ({sorted(tried)}); will move down the shortlist")
        else:
            nxt = sorted(unused, key=lambda ch: ch["priority"])[0]["channel"]
            component = {"online_form": "karthik_form", "phone": "retell",
                         "email": "backend"}[nxt]
            mark = "  ← that's us" if component == "karthik_form" else ""
            print(f"  → will dispatch {nxt} -> {component}{mark}")

    print("\n" + "=" * 78)
    print("Full context: docs/handoff-ranking-candidates.md · docs/whats-left.md")


def bridge_candidates(c, apply: bool) -> None:
    """SHIM — Ranking's job (A1). See the module docstring before using."""
    print("┌" + "─" * 76)
    print("│ SHIM: writing referral_service_candidates is RANKING's job (blocker A1).")
    print("│ Spec handed to them in docs/handoff-ranking-candidates.md.")
    print("│ This exists so a demo isn't blocked on their timing. Delete it when they ship.")
    print("└" + "─" * 76)

    planned = []
    for r in _rows(c, "referrals"):
        rid = r["id"]
        if _rows(c, "referral_service_candidates", referral_id=rid):
            continue
        ranked = [x for x in _rows(c, "ranking_results", referral_id=rid)
                  if x.get("passed_hard_filter") and x.get("rank") is not None]
        for x in sorted(ranked, key=lambda x: x["rank"]):
            planned.append({
                "referral_id": rid,
                "service_id": x["service_id"],
                "rank": x["rank"],
                # `score` is NOT NULL; `eligibility_state` is NOT NULL and 'unknown' is
                # in the CHECK list and accepted by advance_referral's candidate select.
                "score": x.get("combined_score") or 0,
                "eligibility_state": "unknown",
                "candidate_status": "available",
                "reasons": {
                    "combined_score": x.get("combined_score"),
                    "objective_score": x.get("objective_score"),
                    "objective_breakdown": x.get("objective_breakdown"),
                    "subjective_score": x.get("subjective_score"),
                    "subjective_rationale": x.get("subjective_rationale"),
                    "_source": "demo_driver shim — replace with Ranking's own write",
                },
            })

    if not planned:
        print("\nNothing to bridge: every referral either already has candidates, or has "
              "no passing ranking_results.")
        return

    print(f"\n{len(planned)} candidate row(s):")
    for p in planned:
        print(f"  referral {p['referral_id'][:8]}  rank {p['rank']}  "
              f"score {p['score']}  service {p['service_id'][:8]}")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --yes to apply.")
        return

    for p in planned:
        c.table("referral_service_candidates").insert(p).execute()
    print(f"\nwrote {len(planned)} row(s).")

    # The rank_resources action advance_referral queued is still open, and the
    # open-action guard means the referral stays frozen until it's closed. Bridging
    # without this achieves nothing visible, which is a confusing way to fail.
    closed = 0
    for r in _rows(c, "referrals"):
        for a in _rows(c, "referral_actions", referral_id=r["id"]):
            if a["action_type"] == "rank_resources" and a["action_status"] in OPEN:
                c.table("referral_actions").update(
                    {"action_status": "completed",
                     "result": {"bridged_by": "demo_driver shim"}},
                ).eq("id", a["id"]).execute()
                closed += 1
    print(f"closed {closed} open rank_resources action(s).")
    print("\nNow run --diagnose again, or let the worker tick.")


def enable_form_channel(c, service_id: str | None, apply: bool) -> None:
    """Give a service an `online_form` channel so referrals can route to us (A11).

    `advance_referral` step 10 maps `service_application_channels.channel` to a
    component: online_form -> karthik_form, phone -> retell, email -> backend. Today NO
    ground-transport service has an online_form row — all 13 in the DB belong to
    air-ambulance charities — so the form component can never fire on a transport
    referral no matter what Ranking does. And two of the four ranked services have no
    channel row at all, which is worse than useless: step 9's exhaustion test reads
    "no channel exists that hasn't been tried" as vacuously TRUE, so such a service is
    marked exhausted the instant it's selected.

    Defaults to whichever service the transport referral already points at.
    """
    if service_id is None:
        candidates = [r for r in _rows(c, "referrals")
                      if r.get("need_category") == "transportation" and r.get("service_id")]
        if not candidates:
            sys.exit("No transportation referral with a service — pass --service-id.")
        # Prefer a referral that HAS a `service_requests` row. Most of the transport
        # form's fields source from `service_request.*` (pickup, destination, requested
        # time, mobility needs), so a referral without one produces a review screen of
        # blanks — technically routed to us, and useless as a demo.
        with_request = {r["referral_id"] for r in _rows(c, "service_requests")}
        chosen = next((r for r in candidates if r["id"] in with_request), candidates[0])
        service_id = chosen["service_id"]
        note = ("has a service_requests row" if chosen["id"] in with_request
                else "⚠ NO service_requests row — the form will fill mostly blank")
        print(f"referral: {chosen['id']}  ({note})")
        others = [r for r in candidates if r["id"] != chosen["id"]]
        if others:
            print(f"          {len(others)} other transport referral(s); "
                  f"use --service-id to target one of those instead")

    svc = (_rows(c, "services", id=service_id) or [None])[0]
    if svc is None:
        sys.exit(f"service '{service_id}' does not exist.")

    print(f"service : {svc['name']}  ({service_id})")
    print(f"          verification_status={svc.get('verification_status')}"
          + ("   ← synthetic/test data, safe to modify"
             if svc.get("verification_status") == "exclude" else
             "   ⚠ NOT marked 'exclude' — this may be real HSDS data, confirm with Data"))

    existing = _rows(c, "service_application_channels", service_id=service_id)
    print(f"channels: {[(x['priority'], x['channel']) for x in sorted(existing, key=lambda x: x['priority'])] or 'NONE — dead-ends on selection'}")

    if any(x["channel"] == "online_form" for x in existing):
        print("\nAlready has an online_form channel. Nothing to do.")
    else:
        # UNIQUE (service_id, priority) as well as (service_id, channel), and priority is
        # CHECK 1..3 — so take the lowest free slot rather than assuming 1 is available.
        taken = {x["priority"] for x in existing}
        priority = next((p for p in (1, 2, 3) if p not in taken), None)
        if priority is None:
            sys.exit("All three priority slots are taken; free one before adding a channel.")

        row = {
            "service_id": service_id,
            "channel": "online_form",
            "priority": priority,
            # Our hero form is a local PDF, not a hosted page. The column is nullable and
            # advance_referral never reads it, so record where the form actually lives
            # rather than inventing a URL that 404s for anyone who clicks it.
            "application_url": None,
            "notes": "Relay demo: PDF intake handled by the karthik_form component "
                     "(contracts/schemas/transport_intake_pdf.json).",
        }
        print(f"\nWOULD INSERT into service_application_channels:")
        for k, v in row.items():
            print(f"  {k:18} {v}")
        if apply:
            c.table("service_application_channels").insert(row).execute()
            print("\n✔ inserted.")
        else:
            print("\nDRY RUN — nothing written. Re-run with --yes to apply.")

    templates = _rows(c, "form_templates", service_id=service_id)
    print(f"\nform_templates for this service: {len(templates)}")
    if not templates:
        print("  ⚠ none — the review screen will 404 even once the channel exists, "
              "because `referrals` has no form_id column and the form is resolved via "
              "form_templates.service_id. Seed it with:")
        print(f"    python -m backend.scripts.seed_form_templates \\\n"
              f"        --form transport_intake --service-id {service_id}")


def reset_selection(c, referral_id: str | None, apply: bool) -> None:
    """Clear a referral's chosen service so the SW gate fires (demo only).

    A referral that already has `referrals.service_id` skips the gate entirely — the
    gate only asks when nothing is chosen — so it dispatches straight to outreach and
    the selection screen never appears. The live transport referral was seeded with a
    service already attached, not chosen by a social worker, so clearing it restores the
    state the flow is actually meant to start from.

    Only touches synthetic referrals, and only the two selection columns.
    """
    if referral_id is None:
        with_request = {r["referral_id"] for r in _rows(c, "service_requests")}
        pick = next((r for r in _rows(c, "referrals")
                     if r["id"] in with_request and r.get("service_id")), None)
        if pick is None:
            sys.exit("No suitable referral — pass --referral-id.")
        referral_id = pick["id"]

    r = (_rows(c, "referrals", id=referral_id) or [None])[0]
    if r is None:
        sys.exit(f"referral '{referral_id}' does not exist.")
    print(f"referral {referral_id}")
    print(f"  status={r['status']}  service_id={r.get('service_id')}  "
          f"rank={r.get('current_resource_rank')}")
    stale = [a for a in _rows(c, "referral_actions", referral_id=referral_id)
             if a["action_status"] not in OPEN]
    print("\nWOULD SET service_id=NULL, current_resource_rank=NULL, status='ranking'")
    print("  release any candidate previously flagged selected")
    print(f"  DELETE {len(stale)} finished action(s) for this referral")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --yes to apply.")
        return

    # DELETE, not cancel. `queue_referral_action` upserts on
    # (referral_id, deduplication_key) with ON CONFLICT DO UPDATE SET updated_at — it
    # does NOT reset action_status. So a completed/cancelled row permanently poisons its
    # dedup key: advance_referral "queues" the action, gets the dead row's id back, and
    # the gate silently never fires again. Cost me a confusing ten minutes; deleting is
    # the only way to genuinely re-arm a referral.
    c.table("referral_actions").delete().eq("referral_id", referral_id).neq(
        "action_status", "ready").neq("action_status", "in_progress").neq(
        "action_status", "blocked").execute()
    c.table("referral_service_candidates").update(
        {"selected": False, "candidate_status": "available"},
    ).eq("referral_id", referral_id).eq("candidate_status", "selected").execute()
    c.table("referrals").update(
        {"service_id": None, "current_resource_rank": None, "status": "ranking"},
    ).eq("id", referral_id).execute()

    # Fire the gate now rather than waiting for something else to advance the referral:
    # a reset that leaves no open action just looks like nothing happened.
    print("\n✔ reset. advance_referral ->", advance_referral(c, referral_id))


def advance_referral(c, referral_id: str) -> dict:
    res = c.rpc("advance_referral", {"p_referral_id": referral_id}).execute()
    return res.data if isinstance(res.data, dict) else {"result": res.data}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--diagnose", action="store_true", help="read-only walk (default)")
    p.add_argument("--bridge-candidates", action="store_true",
                   help="SHIM: ranking_results -> referral_service_candidates (A1)")
    p.add_argument("--enable-form-channel", action="store_true",
                   help="give a service an `online_form` channel so it routes to us (A11)")
    p.add_argument("--service-id", help="target for --enable-form-channel "
                                        "(default: the transport referral's service)")
    p.add_argument("--reset-selection", action="store_true",
                   help="clear a referral's chosen service so the SW gate fires (demo only)")
    p.add_argument("--referral-id", help="target for --reset-selection")
    p.add_argument("--yes", action="store_true", help="actually write (default is dry-run)")
    args = p.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    c = _client()
    if args.bridge_candidates:
        bridge_candidates(c, apply=args.yes)
    elif args.enable_form_channel:
        enable_form_channel(c, args.service_id, apply=args.yes)
    elif args.reset_selection:
        reset_selection(c, args.referral_id, apply=args.yes)
    else:
        diagnose(c)


if __name__ == "__main__":
    main()
