# Design — Flexible Inbound Router with Stateful Escalations (subproject G, v2)

**Date:** 2026-07-23
**Track:** Aneesh — patient comms + closed-loop verification (`ptcomm`)
**Supersedes:** `2026-07-21-flexible-inbound-handling-design.md` — that version was
written before subproject E (the DB rewrite) shipped and referenced the old
`current_stage()` stage-first code. This version reconciles the design with the
**shipped** E code (`route_inbound`, the slim `patient_outreach` model, the
single-transaction webhook, `repo.py`, the two scheduler loops) and adds the
stateful open-issue tracking + hybrid loop-pause decided in the 2026-07-23
brainstorm.

---

## 1. Problem (the felt bug + the old limits)

Every reply that isn't a clear YES/NO/STOP at an active stage
(NOTIFIED/REMINDED) falls into one `ack_needs_help` template:
- "I don't have a photo ID" and "nevermind, I found it" get the **identical**
  canned line — no intent understanding, no memory.
- The line says "we've flagged this, a team member will reach out," but the
  webhook writes **nothing** — it's an empty promise; no escalation row exists.
- Natural phrasings, questions ("where is it again?"), reschedule/cancel, an
  accessibility need, "call me instead" all dead-end at the same template.

## 2. Hard constraint (unchanged)

**Outbound is 100% templated** (CLAUDE.md §7; `render_template()` raises on a
missing slot). The LLM is used for **inbound classification only** — it maps
reply text to one bounded intent and never generates patient-facing text. No
patient-supplied string is ever echoed into an outbound message. With
`CLASSIFIER=keyword` (no API key), new intents degrade to `unclear` → ack +
escalate — safe, just less flexible.

## 3. Chosen approach

**LLM classifies intent; deterministic pure code owns the response; state lives
in the DB.** The LLM emits one intent from a bounded enum. A pure router maps
`(intent, routing_stage, has_open_issue)` → a structured `InboundOutcome`. The
webhook executes the outcome (write-backs, escalation open/resolve, loop
pause/resume, optional booking lookup, templated ack) in ONE transaction.

## 4. Decisions (2026-07-23 brainstorm)

1. **Stateful open-issue tracking.** A raised problem opens a real row in the
   existing `escalations` table (`status='open'`); a resolving follow-up flips it
   to `resolved` (+ `resolved_at`). No duplicate escalations while one is open;
   a positive follow-up while an issue is open → `ack_resolved`, not the flag
   again. This is the fix for the felt bug.
2. **Full intent set** (see §5.1).
3. **Hybrid loop behavior on an issue:**
   - problem / accessibility_need / channel_preference / appointment_question →
     escalate (where applicable), **loop KEEPS running** (appointment still
     stands).
   - reschedule / cancel → escalate, **loop PAUSES** (appointment itself is in
     question) until resolved.
   - opt_out → stop the loop entirely (decline).
4. **Accessibility has no patient column today.** The `accessibility` table is
   location-scoped (`location_id`); `patients` has no accessibility field. So an
   accessibility need is recorded in the escalation `handoff_summary` + acked.
   *Deferred:* recommend Gyan add `patients.accessibility_needs` (subproject D).
5. **reschedule / cancel → social-worker queue only** (no org-side action-type
   contract yet; revisit with subproject A / dispatch).

## 5. Changes by file

### 5.1 `classifiers.py` — richer, stage-neutral intents
Extend the enum + `_SYSTEM_PROMPT` to: `affirmative`, `negative`, `opt_out`,
`needs_help`, `unclear` (existing) + `reschedule`, `cancel`,
`appointment_question`, `accessibility_need`, `channel_preference`.
- **Resolution is NOT a new intent** — it's an `affirmative`/positive reply while
  an issue is open; the router (which is told `has_open_issue`) interprets it as
  "clear the flag."
- Prompt stays stage-neutral + PHI-free (reply text only). Keyword fast-path
  stays for unambiguous YES/NO/STOP. `DEFAULT_MODEL` is already
  `claude-haiku-4-5`.

### 5.2 `state_machine.py` — intent-first, stage-aware, pure router
Replace the current `route_inbound(outreach, reply_class) -> dict` with
`route_inbound(outreach, intent, has_open_issue) -> InboundOutcome`. Pure, no DB,
no mutation. Add a `routing_stage(outreach)` helper mapping the slim
`patient_outreach.stage` to a coarse reply context:
`consent → consent`; `notified`/`reminded` → `active`; `verifying → verification`;
`awaiting_booking`/`done`/`escalated` → `none` (still classified + handled if an
out-of-order reply arrives).

```
InboundOutcome:
  writebacks: list[str]           # "consent_confirmed" | "consent_declined" |
                                  #   "utilized" | "not_utilized" |
                                  #   "channel_preference" | None
  ack_template_key: str
  new_stage: Stage | None         # keeps E's stage-advance invariant intact
  finish_action: bool             # close the in-progress referral_action
  escalation: str | None          # "open" | "resolve" | None
  escalation_reason: str | None   # patient_reported_problem | reschedule_requested
                                  #   | cancel_requested | accessibility_need
                                  #   | channel_preference | consent_declined ...
  loop: str                       # "continue" | "pause" | "resume" | "stop"
  needs_booking_lookup: bool      # appointment_question
```

**E's BLOCKING INVARIANT is preserved:** any terminal reply still advances
`new_stage` off CONSENT/VERIFYING so Loop B never double-messages a responder.

Routing highlights by `(intent, routing_stage, has_open_issue)`:
- `affirmative`/`negative`: consent-confirm vs. utilization yes/no by stage (as
  E does today). BUT if `has_open_issue` and the reply is affirmative at an
  active stage → `escalation="resolve"`, `ack_resolved`, `loop="resume"`.
- `opt_out` (any stage): `set_consent(declined)`, `ack_declined`,
  `new_stage=ESCALATED`, `loop="stop"`.
- `appointment_question`: `needs_booking_lookup=True`; booking exists → answer
  with `answer_appointment`; none yet (e.g. consent stage) → stage-aware "we'll
  send details once you confirm." No escalation. `loop="continue"`.
- `needs_help` (problem): `ack_problem`, `escalation="open"`
  (`patient_reported_problem`) unless one is already open (dedupe → `ack_problem`
  only). `loop="continue"`.
- `reschedule` / `cancel`: `ack_reschedule` / `ack_cancel`, `escalation="open"`,
  `loop="pause"`.
- `accessibility_need`: `ack_accessibility`, `escalation="open"`
  (`accessibility_need`, need summarized), `loop="continue"`.
- `channel_preference`: `writebacks=["channel_preference"]`,
  `ack_channel_preference`, `escalation="open"` (so a human does the switch),
  `loop="continue"` (SMS/WhatsApp keeps running meanwhile).
- `unclear`: `ack_unclear`; consent stage re-prompts, never auto-resolves.

### 5.3 `models.py` — one new column
Add `patient_outreach.paused = Column(Boolean, default=False, nullable=False)`.
Regenerate `scripts/create_outreach_table.sql`.

### 5.4 `repo.py` — escalation lifecycle + preference write
- `find_open_escalation(referral_id) -> dict | None` (status='open', newest).
- `resolve_escalation(escalation_id, *, conn=None)` (status='resolved',
  resolved_at=now).
- `set_preferred_contact_method(patient_id, method, *, conn=None)`.
- `create_escalation(...)` exists (writes status='open'); reused.
All write fns keep the optional `conn=` seam so the webhook commits atomically.

### 5.5 `main.py` — webhook executes the richer outcome
Single transaction, on `conn = session.connection()`:
1. `find_open_by_phone`; classify via `get_classifier()`; `has_open =
   repo.find_open_escalation(referral_id) is not None`.
2. `outcome = route_inbound(outreach, intent, has_open)`.
3. Apply writebacks (`set_consent`/`set_utilization`/`set_preferred_contact_method`),
   run `get_booking_details` if `needs_booking_lookup`, open/resolve the
   escalation, set `outreach.paused` per `loop`, advance `new_stage`,
   `finish_action` if set, `log_attempt`, send the templated ack, `log_message`.
`received_stage` is still captured pre-mutation for the audit log.

### 5.6 `scheduler.py` — respect the pause
Every `run_due_batch` track filter gains `PatientOutreach.paused.is_(False)`
so paused cases stop receiving reminders/verification/nudges until resumed.

### 5.7 `templates.py` — new templated responses
Add: `answer_appointment` (`{details}` slot, deterministic build like
`booking_details`), `ack_problem`, `ack_resolved`, `ack_reschedule`,
`ack_cancel`, `ack_channel_preference`, `ack_accessibility`. **None echo the
patient's raw text** — e.g. `ack_accessibility` = "We've noted your accessibility
need and will make sure it's accommodated."

## 6. State model
- **Open issue** = an `escalations` row with `status='open'` for the referral.
  Opened on problem/reschedule/cancel/accessibility/channel_preference; resolved
  by a positive follow-up (or a human out of band).
- **Loop pause** = `patient_outreach.paused=True` (reschedule/cancel); cleared on
  resume. Scheduler skips paused rows.
- Dedupe: at most one open escalation per referral at a time.

## 7. PHI posture (unchanged)
Classifier prompt sees reply text only. Reply text may contain volunteered PHI →
BAA endpoint. All outbound templated; DB details reach the patient only via
template slots, never the model.

## 8. Deferred / out of scope
- Org-side propagation of reschedule/cancel (needs Pranav's action-type
  contract; SW queue only for now).
- `patients.accessibility_needs` column (Gyan) — until then, accessibility is
  captured in the escalation summary.
- Free-text Q&A beyond appointment facts → `unclear`/escalate, not answered.
- A dedicated dashboard "open escalations" queue view is a nice-to-have; the case
  table already flags attention. Include a minimal open-issue indicator; a full
  queue view is out of scope for this plan.

## 9. Cross-team items
- STOP / opt-out honored by all agents (shared with E).
- Recommend Gyan add `patients.accessibility_needs` (ties to subproject D).

## 10. Testing
- **Classifier:** table of messy replies → expected intent; keyword fast-path
  still bypasses the API for obvious YES/NO/STOP.
- **Router (pure):** `(intent, routing_stage, has_open_issue)` → assert the full
  `InboundOutcome` (writebacks, ack key, new_stage, escalation action, loop,
  lookup). No DB. Includes: problem opens; affirmative-while-open resolves;
  dedupe (problem while open doesn't re-open); reschedule pauses; opt_out stops.
- **repo:** `find_open_escalation` / `resolve_escalation` /
  `set_preferred_contact_method` round-trip (SQLite-compatible where possible;
  shared-table ones verified against Postgres in the live pass).
- **Webhook integration:** appointment_question answered from a booking;
  problem → escalation opened + ack; resolution → escalation resolved + no
  duplicate; reschedule → paused=True + scheduler skips it; channel_preference →
  preference written + loop continues.
- **Degradation:** `CLASSIFIER=keyword` → new intents fall to unclear → ack +
  (where applicable) escalate; no crash, no dropped reply.
- **Live pass:** seed a case, exercise problem→resolve + reschedule→pause against
  Supabase with mock sends, then clean up (mirrors the E integration checklist).
