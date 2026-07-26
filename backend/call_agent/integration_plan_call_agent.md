# Integration plan — wiring `call_agent` into the orchestrator + UI

**Last updated: 2026-07-24 — Seams A & B are built, wired, and unit-tested.** Live
end-to-end testing is still blocked on DB convergence (§4) — see the flip procedure in
[`docs/integration-status.md`](../../docs/integration-status.md).

This is the pick-up doc for the **phone** channel. It assumes [`CLAUDE.md`](../../CLAUDE.md)
§5–§7, [`docs/integration-plan.md`](../../docs/integration-plan.md), and
[`docs/integration-status.md`](../../docs/integration-status.md) as background — this
file is the call_agent-specific detail those docs point at but don't spell out in code
terms.

**Bottom line up front:** two thin HTTP calls now tie our orchestrator and the vendored
`call_agent` (Voice/Retell) service together — no shared code, per CLAUDE.md §2/§10.
`make_phone_call` dispatches a real call via `call_agent`'s `/place-referral-call`
(Seam A); `call_agent`'s post-call webhook forwards the outcome to our
`/api/voice/call-outcome` adapter (Seam B). Both are unit-tested against mocks; neither
has been run against the real Supabase project yet (§4).

---

## 1. Current state

| Piece | State | Where |
| --- | --- | --- |
| Orchestrator's `make_phone_call` tool | **built** — POSTs to `call_agent`'s `/place-referral-call`; maps `escalated:true` → `failed`, unreachable/timeout → `needs_human`, else `success` | [`backend/tools/make_phone_call.py`](../tools/make_phone_call.py) |
| Inbound adapter `POST /api/voice/call-outcome` | **built + tested**, translates Retell vocab → our frozen set, calls `scheduler.apply_inbound` | [`backend/adapters/inbound.py`](../adapters/inbound.py), [`tests/test_adapters.py`](../../tests/test_adapters.py) |
| `call_agent` service (Voice/Retell) | **built + deployed** on Railway, own FastAPI app, own Supabase client, own DB vocab | [`backend/call_agent/`](.) — `main.py`, `db.py` |
| `call_agent` → orchestrator forwarding (Seam B) | **built** — `log_outcome` forwards to `ORCHESTRATOR_BASE_URL` after its own Supabase write; skipped (not an error) if that env var is unset | [`main.py`](main.py) `_forward_to_orchestrator` |
| orchestrator → `call_agent` dispatch (Seam A) | **built** | [`make_phone_call.py`](../tools/make_phone_call.py) |
| `booking_id` resolution from `referral_id` alone | **built** | [`db.py`](db.py) `get_latest_booking_id`, wired into `/place-referral-call` |
| Unit tests (mocked network, no live Supabase/Railway) | **built** | [`tests/test_tools.py`](../../tests/test_tools.py) |
| Live end-to-end run (real Supabase, real Railway deploy of both services) | **not yet possible** — see §4 | — |

`call_agent` is vendored (per CLAUDE.md §2: "modules talk through the DB + scheduler,
never by importing each other") — we don't import its code, it doesn't import ours;
we edited its files directly for this integration (a deliberate exception to
`docs/integration-plan.md`'s "edit none of their files," since this repo now owns both
sides and someone has to wire the seam — flagged to the Voice owner).

Deployed base URL (from [`transportation_caller.md`](transportation_caller.md)):
`https://md-catalyst-call-agent-production.up.railway.app`. The orchestrator backend
itself is **not deployed anywhere yet** — local `uvicorn --reload` only.

---

## 2. The two seams

### Seam A — OUTBOUND: our scheduler → `call_agent` places the call

`backend/tools/make_phone_call.py` is dispatched by the scheduler at
`outreach_in_progress` when `referral.outreach_channel == "phone"`
([`state_machine.py`](../orchestrator/state_machine.py) `OUTREACH_TOOLS`). Per the
async pattern (CLAUDE.md §7), it does not block on the conversation — it places the
call and returns immediately; the result arrives later via Seam B.

```python
async def make_phone_call(referral_id, db, *, attempt_id, from_state=None, **params):
    base_url = os.environ["CALL_AGENT_BASE_URL"]      # required; no silent fallback
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{base_url}/place-referral-call", json={"referral_id": referral_id},
            )
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPError as e:
        status, error, data = "needs_human", f"could not reach call_agent: {e}", {}
    else:
        if result.get("escalated"):
            status, error = "failed", result.get("reason", "call_agent escalated before placing the call")
            data = {"escalated": True, "reason": result.get("reason")}
        else:
            status, error, data = "success", None, {"placed": True, "call_agent_response": result}
    ...  # write the ToolOutcome, as every tool does
```

`CALL_AGENT_BASE_URL` is a new **required** env var (`.env.example`) — deliberately no
default fallback to the deployed Railway URL, so a missing config fails loudly instead
of a test or a misconfigured local run silently no-op'ing (or worse, silently hitting
production). Read lazily inside the function (not at module import), so importing
`backend.main` / running the test suite never crashes just because it's unset.

**Why the `escalated` branch matters:** `place_referral_call()`
([`main.py`](main.py) `place_referral_call`) can silently *not place a call* and
instead write an `escalations` row when `next_attempt_number(...) > MAX_ATTEMPTS`
([`db.py`](db.py), `MAX_ATTEMPTS = 3`). Treating every non-error HTTP response as
`status="success"` would strand that referral at `submitted`
(`WAITING_FOR_INBOUND`, `state_machine.py`) forever, waiting for a webhook that will
never come. The tool branches on `result.get("escalated")` and returns
`status="failed"` in that case, so the scheduler routes
`outreach_in_progress → escalated` immediately.

**Why network failures map to `needs_human`, not `failed`:** unlike an explicit
`escalated: true` (call_agent's own retry protocol is exhausted — a final answer),
an unreachable/timed-out call_agent is a recoverable infra hiccup, not a decision. It
maps to `needs_human` so it reads distinctly on the dashboard, even though today there's
no dedicated retry UI for either case (both currently surface as the same generic "⚠
Needs social worker" flag — see §5).

### Seam B — INBOUND: `call_agent`'s post-call webhook → our adapter

Retell POSTs to `call_agent`'s `/log-call-outcome` ([`main.py`](main.py)), which calls
`db.save_call_outcome(...)` ([`db.py`](db.py)) — writing to Supabase's `attempts` /
`service_bookings` / `escalations` tables (its own source of truth, per
[`database_usage.md`](database_usage.md)). After that write succeeds, it now also
forwards the outcome to our adapter:

```python
async def _forward_to_orchestrator(body, call_id, result):
    if not ORCHESTRATOR_BASE_URL:          # os.environ.get — optional, unlike CALL_AGENT_BASE_URL
        return                             # see §"Deviations" for why this one isn't required
    payload = {
        "referral_id": body.case_id,        # case_id IS our referral_id (already true today)
        "status": body.status,              # vocab is IDENTICAL to VOICE_STATUS_MAP's keys
        "attempt_no": _attempt_number_from(result),   # pulled from the row save_call_outcome just wrote
        "confirmation_id": body.confirmation_id,
        "pickup_window": body.pickup_window,
        "offered_datetime": body.offered_datetime,
        "call_id": call_id,
        "notes": body.notes,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_BASE_URL}/api/voice/call-outcome",
                json={k: v for k, v in payload.items() if v is not None},
            )
            response.raise_for_status()
    except httpx.HTTPError as e:
        print(f"[log_outcome] orchestrator forward failed (non-fatal): {e}")
```

**Field mapping — this is the good news.** `LogOutcomeRequest.status` (`main.py`) and
`VOICE_STATUS_MAP`'s keys ([`inbound.py`](../adapters/inbound.py)) are the same
six-value vocabulary (`confirmed`, `ineligible`, `unavailable`, `callback_required`,
`escalation_needed`, `alt_slot_offered`) — no translation table needed on the
`call_agent` side, just a pass-through POST. `VoiceCallOutcome`'s fields
(`confirmation_id`, `pickup_window`, `offered_datetime`, `notes`, `call_id`) line up
1:1 with `LogOutcomeRequest`'s. `attempt_no` is pulled from the just-written/duplicate
`attempts` row via `_attempt_number_from()`, which handles both shapes
`save_call_outcome` can return (`dict` on the duplicate-`call_id` path, `list` on the
fresh-insert path).

This forward is **best-effort and non-fatal**: it's wrapped in its own try/except so a
down or misconfigured orchestrator never breaks `call_agent`'s response to Retell's
webhook, and it's skipped entirely (not an error) when `ORCHESTRATOR_BASE_URL` is unset
— see §"Deviations" for why.

---

## 3. `booking_id` — resolved

Our `make_phone_call` tool's signature (`tool(referral_id, db, *, attempt_id,
from_state)`) has no `booking_id`, and `ReferralDB` has no concept of one. `call_agent`'s
`/place-referral-call` used to require `{"booking_id", "referral_id"}`. It now accepts
`booking_id` as **optional** — if omitted, `db.get_latest_booking_id(referral_id)`
resolves the latest `service_bookings` row for that referral server-side (the same
query `trigger_call.py` already did inline for manual testing). So Seam A's POST body
is just `{"referral_id": referral_id}`, matching `database_usage.md`'s stated contract
("Receives: `referral_id`"). `trigger_call.py` itself is untouched — it still calls
`place_referral_call(booking_id, referral_id)` directly with an explicit `booking_id`.

---

## 4. Blocking dependency: same `referral_id`, same database

Seam B's forward will 404 against our adapter (`db.get_referral` raises `KeyError` →
`HTTPException(404, ...)` in `inbound.py`) unless both services are pointed at the
same referral row. Today:

- Our backend defaults to `MockReferralDB` (in-memory fixtures, ids like `ref_1003`)
  unless `SUPABASE_URL`+`SUPABASE_SERVICE_KEY` are set (`backend/main.py` `make_db()`).
- `call_agent` always talks to the real Supabase project (no mock mode).

So this wiring is **built and unit-tested** (against `MockReferralDB` + a mocked
`httpx.AsyncClient.post`, same spirit as `tests/test_adapters.py`), but it can't run
**live end-to-end** until our backend flips onto the real Supabase path — tracked as
parked pending schema freeze in `docs/integration-status.md`. Two options, not
mutually exclusive:

1. **Demo path (Aug-2 safe):** keep using the dashboard's simulated inbound buttons for
   phone outcomes (already fully working, per `frontend/README.md`'s "Sim buttons"
   note) — don't depend on this integration for the recorded take. These changes are
   additive and don't touch the sim-button code path.
2. **Real path:** once `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` are live (the flip
   procedure in `docs/integration-status.md`), Seams A and B start working against
   real rows with no further code change — `call_agent`'s `case_id` already *is* our
   `referral_id`.

---

## 5. Frontend — no new work required, one small polish (still open)

The UI is entirely state-driven (CLAUDE.md's golden rule: frontend never decides
workflow). Once Seams A+B run live, the dashboard and timeline update automatically
because they just read `current_state` and `outreach_attempts` / `ToolOutcome.data`.
Nothing here required a frontend change, and none was made. Still worth doing
(cosmetic, low-risk, do any time):

- `ReferralDetail.jsx`'s `summarize()` ([`ReferralDetail.jsx`](../../frontend/src/ReferralDetail.jsx))
  renders phone outcomes via the generic fallback today. Add a phone-specific branch,
  e.g. `if (data.voice_status) return \`${data.voice_status}${data.confirmation_id ? " · conf " + data.confirmation_id : ""}\``.
- Confirm the existing `flag`/`needs_human` UI path (`ui.jsx` `actionFor`) reads
  naturally once real phone referrals land there via `needs_human` (alt-slot /
  ineligible / unavailable / callback) or the new `needs_human` network-failure case
  (§2). Generic "⚠ Needs social worker" today — fine as-is for the demo.

No new endpoints, no new frontend routes, no changes to `api.js` or `main.jsx`.

---

## 6. Deviations from the original plan (judgment calls made during implementation)

- **`ORCHESTRATOR_BASE_URL` is optional, not required.** `call_agent` is already
  deployed and running live on Railway. A required-env-var-crashes-on-import pattern
  (like `RETELL_API_KEY`) would have broken that live service the moment this merges,
  since Railway doesn't have this var set. It's read via `os.environ.get(...)` and the
  forward is simply skipped (with nothing logged as an error) when absent.
- **`CALL_AGENT_BASE_URL` (our side) is required, with no fallback.** The orchestrator
  backend isn't deployed anywhere yet, so there's no live service to protect —
  requiring it explicitly means a misconfigured local run fails loudly instead of
  quietly hitting the wrong environment (or the real production Railway URL by
  accident).
- **Network/transport failures reaching `call_agent` map to `needs_human`, not
  `failed`** — kept distinct from an explicit `escalated: true` response (§2).

---

## 7. Tests

`tests/test_tools.py` covers `make_phone_call` with `httpx.AsyncClient.post` mocked via
stdlib `unittest.mock` (no real network, no new dependency, CLAUDE.md §9 layering):

- success when `call_agent` places the call,
- `escalated: true` → `status="failed"`,
- missing `CALL_AGENT_BASE_URL` → raises clearly rather than silently no-op'ing,
- the existing scheduler-dispatch test (`test_scheduler_dispatches_phone_method`)
  updated to run under the same mock.

No test exercises Seam B (`call_agent`'s forward) yet — it lives in `call_agent`'s own
`main.py`, outside this repo's `tests/` suite conventions (call_agent isn't imported by
our test suite; see §1). If `call_agent` grows its own test suite, `_forward_to_orchestrator`
and `_attempt_number_from` are the two units worth covering there.

---

## 8. Build order (status)

1. ~~`call_agent` side: add the `referral_id`-only lookup to `/place-referral-call`,
   add the forward-to-adapter call in `log_outcome`.~~ **Done** (§2, §3).
2. ~~Orchestrator side: replace the stub body of `make_phone_call.py` with the real
   POST, branching on `escalated`. Add `CALL_AGENT_BASE_URL` to `.env.example`.~~
   **Done** (§2).
3. ~~Test in isolation: mock `call_agent`'s HTTP response in a `make_phone_call` unit
   test.~~ **Done** (§7).
4. **Still open** — Live smoke test after the DB flip (§4): trigger a real referral
   end-to-end with `python backend/call_agent/trigger_call.py <referral_id>` and
   confirm our dashboard reflects the state change without touching the sim buttons.
   Also still open: setting `ORCHESTRATOR_BASE_URL` on the live Railway `call_agent`
   deployment once the orchestrator itself has a deployed URL to point at.

Nothing here changed `contracts/models.py`, the state machine, or the scheduler — this
was purely two HTTP calls at the edges, consistent with every other channel
(`send_email`, `notify_patient`) integrating the same way.
