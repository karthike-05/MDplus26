# Design — Flexible Inbound Handling (subproject G)

**Date:** 2026-07-21
**Track:** Aneesh — patient comms + closed-loop verification (`ptcomm`)
**Scope:** Make the SMS agent understand messy, natural patient replies and handle
a richer set of intents gracefully, without ever generating freeform outbound text.

Sibling of subproject **E** (the DB rewire): G reshapes the same inbound path
(`classifiers.py`, `state_machine.py`, the webhook) that E rewires, so the two
specs must stay consistent. Where E defines *how the webhook writes*, G defines
*how the webhook decides what to do*.

---

## 1. Problem

Today the inbound path forces every reply into a 5-way label
(`YES` / `NO` / `STOP` / `NEEDS_HELP` / `UNCLEAR`) and routes **stage-first**:
`current_stage()` picks the one open stage and runs its handler. Consequences:
- Natural replies ("yeah that works", "who's this?") often fall to `UNCLEAR`.
- Anything that isn't yes/no/stop (reschedule, a question, an accessibility need,
  "call me instead") has nowhere to go — it dead-ends at `UNCLEAR` / `NEEDS_REVIEW`.
- An out-of-order reply (a question asked during the consent stage) is handled by
  the wrong handler or dropped.

## 2. Hard constraint (unchanged)

**Outbound stays 100% templated** (CLAUDE.md §7; enforced by `render_template()`).
The LLM is used for **inbound classification only** — it maps reply text to one
bounded intent and never generates text shown to a patient. No patient-supplied
string is ever echoed into an outbound message.

## 3. Chosen approach

**LLM classifies intent; deterministic code owns the response.** (Rejected: keyword-
only — won't handle natural language; LLM-picks-template — hands the model control
over what the patient sees and is hard to test.) The LLM emits one intent from a
bounded enum; a pure router maps `(intent, stage)` to a structured outcome; the
webhook executes it (write-backs, lookups, templated send).

## 4. Decisions made during brainstorming

1. Approach 1 — LLM = bounded intent classifier; code = templated response + DB.
2. Any recognized-but-unactionable intent (reschedule, cancel, out_of_scope) →
   **ack the patient with a templated "a team member will follow up" + open an
   escalation.** Never a silent dead end, never an unacknowledged patient.
3. Reschedule / cancel → **SW queue only for now** (no new org-side action-type
   contract yet; revisit when subproject A / dispatch exists).
4. Channel preference ("call me instead") → **record `preferred_contact_method` +
   escalate, but keep the SMS loop running** so nothing stalls during the switch.

## 5. Changes by file

### 5.1 `classifiers.py` — richer, stage-neutral intents
Extend the LLM category enum (and the `_SYSTEM_PROMPT`) to:
`affirmative`, `negative`, `opt_out`, `needs_help`, `unclear` (existing) plus
`reschedule`, `appointment_question`, `accessibility_need`, `channel_preference`,
`cancel`. Prompt stays **stage-neutral and PHI-free** — sees reply text only; the
router assigns per-stage meaning. Keyword fast-path stays for unambiguous
YES/NO/STOP. Flip `DEFAULT_MODEL` to `claude-haiku-4-5` (the file already notes
this is the right cost/speed pick for a short-label classifier).

**Operational requirement:** the new intents only work with `CLASSIFIER=llm` on a
BAA-eligible endpoint. With the classifier off, new intents degrade to `unclear`
→ ack + escalate (safe, just less flexible). Document this.

### 5.2 `state_machine.py` — intent-first, stage-aware router
Replace "pick one stage, run its handler" with: **classify once, then route on
`(intent, stage)`.** The router is **pure and PHI-free**; it returns a structured
`InboundOutcome`, it does not touch the DB:

```
InboundOutcome:
  intent: ReplyClass
  stage: str                     # consent / active / verification / none
  writebacks: list               # e.g. set_consent(confirmed), set_utilization(True)
  ack_template_key: str
  needs_escalation: bool
  escalation_reason: str | None  # consent_no_response, reschedule, cancel, out_of_scope, ...
  needs_booking_lookup: bool     # True for appointment_question -> D
```

The webhook (E's single-transaction handler) executes the outcome: apply
`writebacks` via `repo.py`, run the booking lookup if requested, render + send the
ack, and open the escalation — all in one transaction.

**Stage reconciliation with E.** The router's routing-stage is a coarse *reply
context* (`consent` / `active` / `verification` / `none`), derived from E's
finer `patient_outreach.stage` column: `consent → consent`;
`notified`/`reminded` → `active`; `verifying → verification`;
`done`/`escalated`/`awaiting_booking` → `none` (nothing awaiting a reply, though
an out-of-order question is still classified and handled). A single
`routing_stage(outreach)` helper owns this mapping so the two specs can't drift.

Routing highlights (per `(intent, stage)`):
- `affirmative`/`negative` — meaning depends on stage: consent-confirm vs.
  utilization-yes/no (as today, but now stage-aware rather than stage-forced).
- `opt_out` at any stage — `set_consent(declined)`, stop loop, `ack_declined`.
- `appointment_question` — `needs_booking_lookup=True`; if a booking exists,
  answer with details; if not (e.g. during consent), stage-aware "we'll send
  details once you confirm."
- `reschedule` / `cancel` — `ack_*` + escalate (SW queue only).
- `accessibility_need` — write accessibility field + `ack_accessibility`
  (shared write with subproject D).
- `channel_preference` — update `preferred_contact_method` + escalate; **SMS loop
  continues.**
- `needs_help` / `unclear` — `NEEDS_REVIEW` + ack; consent stage re-prompts rather
  than auto-resolving.

### 5.3 `templates.py` — new templated responses
Add: `answer_appointment` (data-filled `{details}` slot, same deterministic
string-build as `booking_details`), `ack_reschedule`, `ack_cancel`,
`ack_channel_preference`, `ack_accessibility`. **Safety rule:** none of these echo
the patient's raw text — `ack_accessibility` says "we've noted your accessibility
need and will make sure it's accommodated," not a repeat of what they typed.

## 6. Data flow for the two new capability paths

- **D (answer factual questions):** reply → LLM `appointment_question` → router
  sets `needs_booking_lookup` → webhook `get_booking_details()` → render
  `answer_appointment` with real time/place → send. Booking data reaches the
  patient only through a template; it never enters the LLM prompt.
- **Accessibility volunteered:** reply → LLM `accessibility_need` → router
  write-back to the accessibility field → `ack_accessibility`. (Subproject D owns
  the proactive *asking*; both share this write-back.)

## 7. PHI posture (unchanged from the codebase's existing stance)
- Classifier prompt sees reply text only — never name, phone, or chart data.
- Reply text may contain patient-volunteered PHI → run on a BAA endpoint.
- All outbound text templated; all DB-sourced details enter via slots, never via
  the model.

## 8. Deferred / out of scope
- Org-side propagation of reschedule/cancel (needs an action-type contract with
  Pranav; SW queue only for now).
- Full free-text Q&A beyond appointment facts — anything not in the intent enum is
  `out_of_scope` → ack + escalate, deliberately not answered by the agent.

## 9. Cross-team items
- STOP / opt-out must be honored by all agents (shared with E §8).
- `accessibility_need` write-back target column — confirm with Gyan (ties to
  subproject D and the meeting's "add accessibility to the DB" item).

## 10. Testing
- **Classifier:** table of messy replies → expected intent (mock/stub the LLM;
  assert keyword fast-path still bypasses the API for obvious cases).
- **Router (pure):** feed `(intent, stage)` combinations → assert the
  `InboundOutcome` (writebacks, ack key, escalation flag, lookup flag). No DB.
- **Webhook integration:** appointment_question with/without a booking; reschedule
  → escalation opened + ack sent; channel_preference → preference written + SMS
  loop still active; accessibility_need → field written, no raw-text echo.
- **Degradation:** `CLASSIFIER=keyword` → new intents fall to unclear → ack +
  escalate (no crash, no dropped reply).
