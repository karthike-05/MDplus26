# Design — Conversational Responder (template-anchored) for patient replies

**Date:** 2026-08-01
**Track:** Aneesh — patient comms + closed-loop verification (`ptcomm`)
**Builds on:** `2026-07-23-flexible-inbound-router-design.md` (the shipped
`route_inbound` / stateful-escalation inbound path). This design keeps that
router **unchanged** and adds one outbound-phrasing layer on top of it.

---

## 1. Problem

Every reply the patient *receives* is a fixed template (`service.send_templated`
→ `templates.py`). The inbound side is already smart — Claude classifies each
reply into a rich intent set (`classifiers.py`) — but each intent maps to exactly
one canned ack, so:

- The agent "repeats the same message" and reads as robotic (Pranav, 7/31).
- A specific logistics question ("who's picking me up?", "what time again?") gets
  a templated answer that only fills the slots the template declares, not the
  thing the patient actually asked.

The team decision (2026-08-01 brainstorm) is to make patient replies **genuinely
conversational** rather than fully templated — while staying defensible in a
Medicaid/PHI context.

## 2. Decisions (2026-08-01 brainstorm)

1. **Conversational with patient context, logistics-only.** The LLM may now
   generate patient-facing reply text, and its prompt may include *logistics*
   fields (name, service/need category, org name, appointment date/time,
   location, pickup info, contact method). **Clinical PHI is hard-blocked** from
   the prompt (diagnosis, chart notes, Medicaid ID, SSN, DOB, other referrals).
2. **Inform-only; state stays deterministic.** The LLM only *phrases* replies and
   *answers logistics questions*. It never changes state. Consent / utilization /
   escalation / loop-pause continue to flow through the existing
   `classifier → route_inbound → execute_inbound` writebacks, which remain the
   single audited source of truth.
3. **Approach A — template-anchored rephraser.** The state machine still selects
   the intended content (which ack template + which facts). The LLM *rephrases
   that approved content* and answers the patient's question **using only the
   provided facts**. The rendered template is both the **content contract** and
   the **fallback**.
4. **Replies only, never proactive sends.** The responder applies only to acks
   sent in response to an inbound patient message. Proactive/first-contact sends
   (consent, day3 reminder, day7 verification, nudge) stay verbatim templates —
   both because that is where the felt bug is and because WhatsApp only permits
   freeform text inside the 24-hour service window that an inbound reply opens
   (CLAUDE.md §9). Outside that window Meta requires a pre-approved template.

> **Golden-rule alignment (monorepo CLAUDE.md §2):** demo runs on **synthetic
> data only**, so the BAA/allowlist/audit machinery below is *narrated as
> production design and enforced structurally in code*, not a claim that we
> process real PHI. "No live LLM in the submission path" is about form
> submission; this is the patient-messaging reply path, where the track already
> uses an LLM on inbound (`classifiers.py`).

## 3. Architecture — two layers, one new module

```
inbound webhook (main.py)
  └─ execute_inbound(...)                      [UNCHANGED authority]
        route_inbound -> writebacks / escalation / loop / ack_key   (deterministic)
        render_template(ack_key, facts)  -> template_body           (content contract)
        ┌─────────────────────────────────────────────────────────┐
        │ NEW: responder.compose_reply(template_body,              │
        │        facts=..., patient_question=body, history=...)    │
        │   -> natural reply  (or template_body on any failure)    │
        └─────────────────────────────────────────────────────────┘
        provider.send_message(phone, reply)                        (existing send)
```

- `state_machine.py`, `templates.py`, `classifiers.py`, `poller.py`,
  `scheduler.py`, and every proactive send path are **untouched**.
- The only behavioral change is *how the ack string is produced* before the
  existing send + log.

## 4. The `responder.py` module

### 4.1 Entry point

```python
def compose_reply(
    template_body: str,          # the rendered, approved ack — content contract + fallback
    *,
    facts: dict,                 # logistics-only slot dict already flowing through inbound.py
    patient_question: str,       # the inbound reply text (what they actually asked)
    history: list[dict],         # last N thread messages [{direction, body}], for coherence
) -> str:
    ...
```

Returns a natural-language reply, or `template_body` unchanged on any error or
validation failure.

**The responder never touches the DB.** All data reaches it as pre-fetched
`facts` — this is what keeps the PHI allowlist (§4.2) airtight: the module can
only ever see fields explicitly handed in. Booking logistics (appointment
date/time, pickup, location, confirmation #) are queried by the existing
deterministic layer (`repo.get_booking_details` → `compose_details` in
`inbound.py`) and passed in as `facts["details"]`. See §5 for the sourcing
change that makes those facts available on *every* reply, not just ones the
router classified as appointment questions.

### 4.2 PHI allowlist — enforced structurally

`_build_allowed_context(facts)` copies **only** these keys into the prompt
context: `patient_name`, `clinic_name`, `resource_name`, `service_type`,
`details`. Any other key is dropped before prompt construction, so a future
caller cannot leak a clinical field even by accident. A unit test asserts a
planted chart-ish key never appears in the built prompt string.

### 4.3 Prompt

- **Model:** BAA-eligible Anthropic endpoint (same account as `classifiers.py`).
  Default `claude-haiku-4-5` (cost/latency), overridable via `RESPONDER_MODEL`.
- **System prompt (fixed):** "You rephrase an approved outbound message from a
  healthcare social-services outreach program to sound warm and human, and answer
  the patient's question using ONLY the facts provided. Rules: use only the given
  facts; never invent times, addresses, names, or eligibility; never give medical
  advice; if asked something the facts don't cover, don't guess — say a
  coordinator will follow up; ≤2 short sentences, SMS-style, no markdown, no
  emoji, no links."
- **User content:** the approved `template_body`, the allowlisted `facts`, the
  `patient_question`, and the recent `history`.

### 4.4 Output validation (before anything is sent)

Reject and fall back to `template_body` if the completion is: empty/whitespace;
longer than `MAX_REPLY_CHARS` (~320, one WhatsApp segment); contains an unfilled
`{slot}` placeholder; contains markdown or a URL. On reject or on any API
error/timeout (~3s budget), return `template_body`.

### 4.5 Feature flag

`RESPONDER=on|off`, **default `on`** (team decision 2026-08-01). Set
`RESPONDER=off` for byte-identical-to-template behavior — used by CI/dev with no
API dependency. `on` is a safe default because every failure path (§4.4) already
degrades to the template, so turning it on can never make a reply worse than
today's; it can only improve it.

### 4.6 Audit trail

Each invocation emits a structured `responder_audit` log record: model, the fact
**keys** passed (not full values), `patient_question`, the raw completion, and
the decision (`accepted` | `fallback:<reason>`). This is the reconstructable
"what did the agent say and why" trail CLAUDE.md §7 narrates for production.

## 5. Integration points (small, localized)

- **`inbound.py`** — thread the inbound `body` (as `patient_question`) and a
  fetched `history` into the ack step, and route the ack through
  `responder.compose_reply(...)` instead of taking `send_templated`'s rendered
  body directly. All writebacks/escalations are already staged *before* this
  step, so a responder failure cannot affect state.
  - **Booking-facts sourcing (so it can answer "specific questions").** Today
    `repo.get_booking_details` is only called when `route_inbound` sets
    `needs_booking_lookup` (appointment-question intent). When `RESPONDER=on`,
    pre-fetch booking logistics for **any** inbound reply and pass them as
    `facts["details"]`, so the responder can answer logistics follow-ups even
    when the classifier tagged the reply as something else. It is one cheap query
    and `compose_details(None)` already returns a safe placeholder when no
    booking exists yet (e.g. consent stage), so this is safe at every stage. The
    responder stays DB-free; only `inbound.py` gains the extra fetch.
- **`service.py`** — allow the ack send to accept an already-composed body (so
  the responder's output is what gets sent + logged), while proactive/
  first-contact sends keep rendering + sending the template as they do now.
- Everything else unchanged.

## 6. Error handling

- The responder is invoked **after** all DB writes are staged; it never mutates
  state. Worst case = patient receives the plain template, state is still correct.
- Any exception/timeout → `warning` log → `template_body`. The inbound webhook
  must never 500 because of the responder (same discipline as the classifier's
  `except → UNCLEAR` and `org_events`' fire-and-forget).

## 7. Testing

- **Unit (offline, no API):** `_build_allowed_context` drops non-allowlisted
  keys; a clinical field never reaches the prompt string; validation rejects
  too-long / placeholder-leaking / empty / markdown completions → fallback;
  `RESPONDER=off` returns the template unchanged.
- **Stubbed LLM client** (monkeypatched like `classifiers`/`org_events` tests): a
  valid completion is sent; a raising client falls back; an invalid completion
  falls back.
- **Integration:** an `inbound.py` test asserting the ack path calls
  `compose_reply`, and that a responder exception still yields a sent message and
  the correct writebacks/escalation.
- Existing suite stays green because default `off` = current behavior.

## 8. Out of scope

- Proactive/scheduled message phrasing (day3/day7/nudge) — stays templated
  (WhatsApp window + this is the reply-only change).
- LLM-driven state changes / tool actions (explicitly rejected: inform-only).
- Clinical data in prompts (hard-blocked by the allowlist).
- Multi-turn agentic memory beyond feeding the last N thread messages.

## 9. Files

- **New:** `responder.py`, `tests/test_responder.py`.
- **Edited:** `inbound.py` (thread question/history, call responder on ack),
  `service.py` (accept a pre-composed ack body).
- **Unchanged:** `state_machine.py`, `templates.py`, `classifiers.py`,
  `poller.py`, `scheduler.py`, proactive send paths.
