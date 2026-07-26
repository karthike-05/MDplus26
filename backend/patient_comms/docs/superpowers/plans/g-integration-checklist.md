# G Live-Integration Checklist — Flexible Inbound Router

Verify the flexible inbound router end-to-end against live Supabase with mock
sends, then clean up. Mirrors the E checklist discipline (seed → exercise →
delete-everything). WhatsApp is the channel; `SMS_PROVIDER=mock` keeps it dry.

## 0. Migration (once, already applied 2026-07-23)
The live `patient_outreach` predates the `paused` column, and the app's
`create_all` does NOT alter existing tables, so this ALTER was run against
`DATABASE_URL`:
```sql
ALTER TABLE patient_outreach ADD COLUMN IF NOT EXISTS paused boolean NOT NULL DEFAULT false;
```
Confirm: `\d patient_outreach` shows `paused boolean not null default false`.

## 1. Run
Start the app (or use the deployed Railway service) with:
```
SMS_PROVIDER=mock CLASSIFIER=llm DEMO_TIMESCALE=seconds ENABLE_SCHEDULER=0 uvicorn main:app
```
(`ENABLE_SCHEDULER=0` for a controlled local pass; on Railway the scheduler is on.)

## 2. Seed + drive a case
Use `scripts/add_mock_patient.py "Name" "+1<verified-number>"` then confirm consent
and notify (`--notify`). Then exercise each new intent by simulating inbound
replies (dashboard Simulate box, or POST to `/webhook/sms-inbound`).

Check each with the queries below (all keyed by the synthetic `referral_id`):

- [ ] **Question** — reply "where do I go?" →
  - an outbound `messages` row at stage `ack` whose body contains the real
    booking details (pickup/instructions/confirmation) — i.e. `answer_appointment`
    rendered from `v_patient_service_booking_details`.
  - NO new `escalations` row.
- [ ] **Problem** — reply "I don't have a photo ID" →
  - exactly ONE `escalations` row: `status='open'`, `reason_code='patient_reported_problem'`.
  - `patient_outreach.paused` is still `false` (loop keeps running).
  - an inbound `attempts` row: `channel='whatsapp'`, `status='delivered'`
    (NOT 'received'), `purpose` = the stage the reply arrived in (e.g. `notified`
    — note: inbound purpose is the stage value, distinct from outbound
    `booking`/`reminder`/`verification`).
- [ ] **Resolution** — reply "nevermind, found it" →
  - that same escalation flips to `status='resolved'` with `resolved_at` set.
  - NO second escalation row (dedupe held).
  - the ack sent is `ack_resolved` (body: "glad that's sorted").
- [ ] **Reschedule** — reply "can we move it to next week?" →
  - `patient_outreach.paused = true`; a `reschedule_requested` open escalation.
  - the scheduler does NOT send a reminder/verification for this row while paused
    (spot-check by leaving it a cycle in demo mode, or assert the reminder track
    skips it).
- [ ] **Channel preference** — reply "can you call me instead?" →
  - `patients.preferred_contact_method = 'phone'`; a `channel_preference` open
    escalation; `paused` stays `false` (SMS/WhatsApp loop continues).
- [ ] **Opt-out** — reply "STOP" →
  - `patients.consent_status='declined'`, `patient_outreach.stage='escalated'`,
    loop stopped.

## 3. Degradation check (optional)
Restart with `CLASSIFIER=keyword`; the new intents fall to `unclear` → `ack_unclear`
(problem/reschedule handled generically). No crash, no dropped reply.

## 4. Cleanup (leave no synthetic data)
`python3 scripts/add_mock_patient.py --cleanup` removes the mock patient(s) and
their referrals/actions/attempts/escalations/service_bookings/patient_outreach/
messages by the synthetic `referral_id`. Verify:
```sql
SELECT count(*) FROM escalations WHERE referral_id = '<synthetic-rid>';   -- 0
SELECT count(*) FROM patients WHERE phone = '+1<verified-number>' AND name LIKE 'DEMO%';  -- 0
```

## Notes
- Inbound `attempts.purpose` carries the arrival-stage value (`consent`,
  `notified`, `reminded`, `verifying`) — `attempts.purpose` has no CHECK
  constraint, so this is accepted; it just differs from the outbound purpose
  vocabulary. Reconcile the labels later if a report needs a unified taxonomy.
- Deferred: org-side propagation of reschedule/cancel (SW queue only);
  `patients.accessibility_needs` column (accessibility currently captured in the
  escalation summary).
