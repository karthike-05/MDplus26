# Transportation Referral Call — Agent Workflow

## Backend / Retell functions to build

**Pre-call (assembles dynamic variables before the call is placed)**
1. `prep_call_variables(case_referral_id)` — pulls patient + clinic + case data from Postgres/EHR and builds the variable payload handed to Retell at call start (patient identity/contact, pickup address, mobility needs, insurance/Medicaid ID, appointment details, clinic/SW routing info, org contact info).

**Dispatch / orchestration (cron scheduler + Voice MCP tool)**
2. `dial_transportation_org(case_referral_id)` — invoked by the cron scheduler; calls the Voice MCP tool with the transportation agent ID + variables from #1, and enforces the 3-attempt retry protocol.

**Mid-call (Retell function-calling, live during the call — step [6])**
3. `get_appointment_details(patient_id)` — real-time lookup of the patient's upcoming appointment (exact time, provider, appt type) if the org asks for something not already in the passed variables.
   - open question: does this also need to cover live Medicaid/eligibility lookup, or is that pre-verified before the call?

**Post-call (Retell post-call webhook → our backend)**
4. `log_call_outcome(case_referral_id, status, ...)` — parses the structured outcome (status, confirmation_id, pickup_window, offered_datetime, escalation_reason, notes) and writes it to Postgres, keyed by `case_referral_id`.
5. `notify_social_worker(case_referral_id, reason, transcript)` — creates a dashboard alert/queue item for the assigned social worker. Used for `ineligible`, `unavailable`, and `escalation_needed` outcomes.
6. `notify_patient(case_referral_id, status, message_variant)` — sends SMS/WhatsApp/email with status-specific plain-language content (alternate time offered, follow-up needed, ride confirmed).
7. `schedule_retry_or_close(case_referral_id, status)` — deterministic routing: `no_answer`/`callback_required` → retry via cron (up to 3 attempts) → escalate to SW if exhausted; `confirmed` → close case; everything else → route to #5/#6.

**Needs confirming — may already be native Retell features, not custom builds**
8. IVR menu navigation (DTMF/speech) at step [1] — check whether Retell's call-flow handles this natively before building custom logic.
9. Voicemail/answering-machine detection at step [1] — same question, check Retell's native AMD support first.

## Legend
- 🤖 `AGENT` — live conversational step, handled by the Retell LLM in real time
- ⚙ `FUNCTION` — deterministic backend/tool call (DB lookup, webhook, notification) — needs to be implemented in Retell (mid-call function) or on our backend
- ⚠ `NEEDS INFO` — data the agent needs but that isn't a simple static variable; flagged for solutioning
- 🔺 `ESCALATION` — call ends early, routes to social worker + patient notification

## Variables passed to agent at call start (dynamic variables)

**Patient**
- `patient_name`
- `patient_dob`
- `patient_phone`
- `pickup_address` (may differ from address on file)
- `mobility_needs` (ambulatory / wheelchair / stretcher / other equipment)
- `insurance_type` / `medicaid_id` — ⚠ see Needs Info below
- `appointment_datetime`
- `appointment_location` (dropoff address)
- `appointment_provider_name`
- `appointment_type`
- `patient_availability_window` — ⚠ see Needs Info below

**Clinic / referral / routing**
- `referring_clinic_name`
- `referring_clinician_name`
- `clinic_callback_number`
- `assigned_social_worker_name` + contact (escalation routing)
- `case_referral_id` (internal ID — ties outcome back to the correct record in Postgres)
- `transportation_org_name` / `transportation_org_number` (call destination)

## Flowchart

```
START: Cron scheduler triggers call → Voice MCP tool dials transportation org
  │
  ▼
[1] 🤖 Call connects — branch on what picks up
  ├─ Voicemail / no answer → END: ⚙ log outcome "no_answer" → ⚙ trigger retry (3-attempt protocol)
  ├─ IVR menu detected → 🤖 navigate to "scheduling"/"intake" option (DTMF/speech), loop back to [1]
  └─ Human answers → continue to [2]
  │
  ▼
[2] 🤖 Agent opens: identifies itself as an AI assistant calling on behalf of
    {referring_clinic_name} to schedule non-emergency medical transportation
    for a patient, discloses AI nature
  │
  ▼
[3] 🤖 Gatekeeper check — is this person able to handle scheduling?
  ├─ No, transfers → wait for new person to connect, restart from [2]
  ├─ No, asks to call back → 🤖 capture correct number/hours → END: ⚙ log "callback_required"
  ├─ Org says they need to verify directly with the patient → 🔺 ESCALATION (patient_required)
  └─ Yes → continue to [4]
  │
  ▼
[4] 🤖 Agent provides patient identifying info
    (name, DOB, phone, insurance/Medicaid ID — from dynamic variables)
  │
  ▼
[5] 🤖 Org asks: "is this patient in our system / eligible?"
  ├─ Not eligible / not covered → END: ⚙ log outcome "ineligible" → ⚙ flag for social worker
  ├─ Eligible, but org needs extra verification/documentation agent doesn't have
  │    (e.g. proof of Medicaid, prior auth, signed consent) → 🔺 ESCALATION (verification_required)
  └─ Eligible, no issues → continue to [6]
  │
  ▼
[6] 🤖 Agent provides trip details: pickup address, dropoff address (appointment
    location), appointment date/time, mobility needs
    ↳ ⚙ FUNCTION: real-time lookup of patient's upcoming appointment if org asks
       for details agent doesn't already have (exact time, provider, appt type)
  │
  ▼
[7] 🤖 Org checks availability for that date/time
  ├─ No availability at requested time, offers alternate slot → [ALT] (below) —
  │    agent does NOT decide if the alternate slot works; that requires the
  │    patient's input (see Needs Info #3)
  ├─ No service to that area at all → END: ⚙ log outcome "unavailable" → ⚙ flag for social worker
  └─ Available at requested time → continue to [8]
  │
  ▼
[8] 🤖 Org confirms trip + gives confirmation/reference number
  │
  ▼
[9] 🤖 Agent reads back trip details to confirm accuracy (pickup window, address, date)
  │
  ▼
END: ⚙ FUNCTION — post-call webhook writes structured outcome to Postgres
    { status: "confirmed" | "unavailable" | "ineligible" | "callback_required"
             | "no_answer" | "escalation_needed" | "alt_slot_offered",
      escalation_reason: "patient_required" | "verification_required" | null,
      confirmation_id, pickup_window, offered_datetime, notes }
  │
  ▼
If status ≠ "confirmed" → ⚙ route to escalation/retry logic
```

### 🔺 Escalation sub-flow (shared by all escalation branches)
```
[ESC] 🤖 Agent politely closes out the call, thanks the org, ends call
  → ⚙ FUNCTION: log outcome to Postgres (status="escalation_needed", reason=<...>, transcript)
  → ⚙ FUNCTION: notify assigned social worker (dashboard alert/queue item) with reason + transcript
  → ⚙ FUNCTION: notify patient (SMS/WhatsApp/email) that a next step is needed, with plain-language
     explanation and expected follow-up (e.g. "someone will call you to confirm ride details")
  → END
```

### [ALT] Alternate slot offered (not a full escalation — no decision needed from org, just from patient)
```
[ALT] 🤖 Agent thanks the org for the information, does NOT confirm the alternate
    slot, ends call politely
  → ⚙ FUNCTION: log outcome to Postgres (status="alt_slot_offered", offered_datetime=<...>, notes)
  → ⚙ FUNCTION: notify patient (SMS/WhatsApp/email) with the alternate time offered,
     asking them to confirm whether it works
  → (social worker sees this on the standard case dashboard via the logged status —
     no urgent SW push needed, since nothing has gone wrong, it's just pending a decision)
  → END
```