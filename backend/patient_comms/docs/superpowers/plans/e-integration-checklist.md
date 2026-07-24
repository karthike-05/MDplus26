# E — Live integration verification checklist (manual, against Supabase)

This is a maintainer-run procedure, not something Claude Code executes. It requires
a real `DATABASE_URL` (Supabase/Postgres) and Gyan's live shared schema (`patients`,
`referrals`, `referral_actions`, `service_bookings` + the `patient_service_booking_details`
view, `attempts`, `escalations`) already applied to that same database. None of the
steps below were run as part of generating this checklist — see the "Not executed"
note at the bottom.

It mirrors the discipline `repo.py`'s Phase 1 functions were originally verified
with (see `progress.md` — "Supabase data-access layer ... Phase 1, DONE + verified"):
seed one synthetic row per table, run the real loop against it, verify each
transition, then delete every synthetic row so nothing is left behind in a
shared database other teammates also use.

The exact column list/types below reflect what `repo.py`'s SQL currently reads
and writes (`get_patient_for_referral`, `get_booking_details`, `set_consent`,
`mark_booking_notified`, `set_utilization`, `log_attempt`, `create_escalation`).
Confirm against Gyan's actual live migration before running — his schema is the
source of truth, not this doc, and this doc has not been reconciled against it
line-by-line.

---

## Step 1 — Apply the DDL

- [ ] Apply `scripts/create_outreach_table.sql` to `DATABASE_URL`, via `psql` or the
  Supabase SQL editor:
  ```bash
  psql "$DATABASE_URL" -f scripts/create_outreach_table.sql
  ```
- [ ] Confirm both `patient_outreach` and `messages` land in the **same** Postgres
  database/schema that already holds Gyan's shared tables (`patients`, `referrals`,
  `referral_actions`, `service_bookings`, `attempts`, `escalations`). This is required
  because the inbound webhook (`main.py: /webhook/sms-inbound`) writes to our tables
  and his tables in a single DB transaction (`session.connection()` shared with
  `repo.*` write-backs) — if they're in different databases, that atomicity is
  impossible, not just degraded.
- [ ] Sanity check: `\dt patient_outreach` and `\dt messages` in `psql`, or the
  table list in the Supabase dashboard, alongside the existing shared tables.

## Step 2 — Seed one synthetic referral + consent action

Pick a real phone number verified with your Twilio/WhatsApp sender (see
`providers.py` — WhatsApp is the confirmed channel for this loop; SMS via Twilio
is implemented but not yet live-verified end to end). Use a single `referral_id`
(a fresh `uuid`) throughout — write it down, cleanup in Step 5 keys on it.

```sql
-- record the id you use here, e.g.:
-- referral_id = '11111111-1111-1111-1111-111111111111'
-- patient_id  = '22222222-2222-2222-2222-222222222222'

INSERT INTO patients (id, name, phone, referring_clinic_name, consent_status,
                       preferred_contact_method, created_at, updated_at)
VALUES ('22222222-2222-2222-2222-222222222222', 'Test Patient',
        '+1XXXXXXXXXX',            -- your verified WhatsApp/Twilio number
        'KU Health Liberty Market', 'pending', 'whatsapp', now(), now());

INSERT INTO referrals (id, patient_id, service_id, need_category, status,
                        created_at, updated_at)
VALUES ('11111111-1111-1111-1111-111111111111',
        '22222222-2222-2222-2222-222222222222',
        NULL, 'transportation', 'open', now(), now());

INSERT INTO referral_actions (id, referral_id, service_id, action_type,
                               assigned_component, action_status, input_payload,
                               created_at, updated_at)
VALUES ('33333333-3333-3333-3333-333333333333',
        '11111111-1111-1111-1111-111111111111',
        NULL, 'confirm_consent', 'twilio', 'pending', '{}'::jsonb, now(), now());
```

- [ ] Rows inserted; `referral_id` written down for cleanup.
- [ ] Confirm the columns above actually match Gyan's live DDL (`\d patients`,
  `\d referrals`, `\d referral_actions`) — adjust names/NOT NULLs as needed before
  running; this block is illustrative of the fields `repo.py` reads/writes, not a
  guaranteed 1:1 copy of his migration.

## Step 3 — Start the app on the compressed demo timescale

```bash
export DATABASE_URL=postgresql://<supabase-connection-string>
export SMS_PROVIDER=whatsapp        # confirmed channel; SMS_PROVIDER=twilio not yet live-verified
export DEMO_TIMESCALE=seconds
export DEMO_DAY_SECONDS=60          # one logical "day" = 60s, long enough to reply live
export CLASSIFIER=llm               # flexible inbound classification ("went yesterday, thanks")
export ENABLE_SCHEDULER=1           # starts both Loop A (poller) and Loop B (scheduler)
export TWILIO_ACCOUNT_SID=...
export TWILIO_AUTH_TOKEN=...
export WHATSAPP_FROM=whatsapp:+1XXXXXXXXXX
uvicorn main:app --reload
```

- [ ] App starts without error; startup log shows `scheduler started:
  timescale=seconds poll=2s` (see `scheduler.py: start_scheduler`).

## Step 4 — Verify: consent

- [ ] **Loop A picks up `confirm_consent`** within one poll interval (~2s at demo
  scale). A `patient_outreach` row appears for the referral: `stage=CONSENT`,
  `active_action_id` set to the `referral_actions.id` above, `consent_attempts=1`,
  `next_consent_retry_at` ≈ now + 2×`day()` (~120s at `DEMO_DAY_SECONDS=60`).
- [ ] A `messages` row is logged: `direction=outbound`, `stage=consent`, body =
  the rendered consent template (clinic name = `referring_clinic_name`).
- [ ] The patient receives the consent WhatsApp message on the verified number.
- [ ] **Reply with a flexible "YES"-style message** (e.g. "yes go ahead" — this is
  what `CLASSIFIER=llm` is for; a bare keyword classifier would also catch a plain
  "YES" but the point of this run is to confirm the LLM path handles a looser
  phrasing). Confirm, in ONE transaction (`main.py: /webhook/sms-inbound`):
  - `patients.consent_status = 'confirmed'` and `referrals.consent_confirmed_at`
    stamped (`repo.set_consent`).
  - `referral_actions` row → `action_status='completed'`, `active_action_id`
    cleared on the outreach row (`repo.finish_action`).
  - `patient_outreach.stage = AWAITING_BOOKING`.
  - An `attempts` row written with `channel='whatsapp'`, `direction='inbound'`,
    `purpose='consent'`, `status='received'`.
  - An outbound ack message logged in `messages` (`stage=ack`) and delivered to
    the patient.

## Step 5 — Verify: booking → reminder → verification

- [ ] Insert a synthetic `service_bookings` row for the same `referral_id`, with
  `scheduled_start_at` ≈ now + 2×`day()` out (so the reminder track — service date
  minus 2×`day()` — fires almost immediately at demo scale) and a
  `notify_patient` action:
  ```sql
  INSERT INTO service_bookings (id, referral_id, patient_id, service_id,
                                 scheduled_start_at, patient_notified, created_at, updated_at)
  VALUES ('44444444-4444-4444-4444-444444444444',
          '11111111-1111-1111-1111-111111111111',
          '22222222-2222-2222-2222-222222222222', NULL,
          now() + interval '120 seconds', false, now(), now());

  INSERT INTO referral_actions (id, referral_id, service_id, action_type,
                                 assigned_component, action_status, input_payload,
                                 created_at, updated_at)
  VALUES ('55555555-5555-5555-5555-555555555555',
          '11111111-1111-1111-1111-111111111111', NULL, 'notify_patient',
          'twilio', 'pending', '{}'::jsonb, now(), now());
  ```
  (Confirm `service_bookings`'/the `patient_service_booking_details` view's exact
  column names against Gyan's live schema — `repo.get_booking_details` reads
  `organization_name`, `confirmation_number`, `pickup_address`,
  `patient_instructions`, `scheduled_start_at` off the view.)
- [ ] **Loop A picks up `notify_patient`**: booking details sent (`messages` row,
  `stage=booking`), `service_bookings.patient_notified=true` +
  `patient_notified_at` stamped, `referral_actions` → `completed`,
  `patient_outreach.stage=NOTIFIED`, `active_action_id` cleared,
  `next_reminder_at`/`next_verify_at` set per `outreach_repo.compute_schedule`
  (reminder = service date − 2×`day()`, verify = service date + 1×`day()`).
- [ ] **Reminder fires** on the compressed timescale (Loop B, `scheduler.py`):
  `messages` row (`stage=reminder`), `patient_outreach.stage=REMINDED`,
  `attempts` row (`purpose='reminder'`, `channel='whatsapp'`).
- [ ] **Verification fires** next (service date + 1×`day()`):
  `messages` row (`stage=verification`), `stage=VERIFYING`,
  `verification_attempts=1`, `next_nudge_at` set, `attempts` row
  (`purpose='verification'`).
- [ ] **Reply with a flexible "yes I went" message** (again exercising
  `CLASSIFIER=llm`, e.g. "yeah went yesterday thanks"). Confirm, in one
  transaction: `referrals.patient_confirmed_utilization=true` +
  `patient_confirmed_at` stamped, `service_bookings.patient_confirmed_details=true`
  (`repo.set_utilization`), `patient_outreach.stage=DONE`, `attempts` row
  (`direction='inbound'`, `purpose='verification'`), ack message sent.

## Step 6 — Verify silence paths (optional but recommended)

- [ ] **Consent silence:** seed a second synthetic referral, do NOT reply to
  consent. Confirm one resend at `next_consent_retry_at` (~2×`day()`,
  `consent_attempts=2`), then escalation at the next retry gap:
  `patient_outreach.stage=ESCALATED`, an `escalations` row
  (`reason_code='consent_no_response'`), and (if `active_action_id` was still
  set) that `referral_actions` row finished.
- [ ] **Verification silence:** let a `VERIFYING` row go unanswered. Confirm a
  nudge (`messages` stage=nudge, `verification_attempts=2`), then escalation
  (`stage=ESCALATED`, `escalations` row `reason_code='verification_no_response'`).

## Step 7 — Cross-cutting checks

- [ ] **`attempts.channel = 'whatsapp'`** on every row written during this run —
  spot check with:
  ```sql
  SELECT channel, purpose, direction, status FROM attempts
  WHERE referral_id IN ('11111111-1111-1111-1111-111111111111', '<second synthetic id if used>');
  ```
- [ ] **BLOCKING invariant — watch this closely during the run:** any patient who
  *replied* must have their `patient_outreach.stage` advanced off the stage the
  reply answered (e.g. `CONSENT` → `AWAITING_BOOKING`, or `VERIFYING` → `DONE`)
  in the SAME transaction as the reply's write-back. If a reply is processed but
  `stage` does NOT advance, Loop B (`scheduler.py`) will incorrectly treat that
  row as still silent on its next pass and send a redundant reminder/nudge or
  escalate a patient who already responded. Confirm this did NOT happen for any
  row you replied to in Steps 4–5.

## Step 8 — Clean up every synthetic row

Run in this order (children before parents, FK-safe) — replace the ids with the
ones you actually used. Do this even if a step above failed partway through, so
no synthetic data is left in the shared database.

```sql
-- messages: keyed off outreach_id, which is keyed off referral_id via patient_outreach
DELETE FROM messages
WHERE outreach_id IN (
  SELECT id FROM patient_outreach
  WHERE referral_id IN ('11111111-1111-1111-1111-111111111111', '<second synthetic id if used>')
);

DELETE FROM patient_outreach
WHERE referral_id IN ('11111111-1111-1111-1111-111111111111', '<second synthetic id if used>');

DELETE FROM attempts
WHERE referral_id IN ('11111111-1111-1111-1111-111111111111', '<second synthetic id if used>');

DELETE FROM escalations
WHERE referral_id IN ('11111111-1111-1111-1111-111111111111', '<second synthetic id if used>');

DELETE FROM referral_actions
WHERE referral_id IN ('11111111-1111-1111-1111-111111111111', '<second synthetic id if used>');

DELETE FROM service_bookings
WHERE referral_id IN ('11111111-1111-1111-1111-111111111111', '<second synthetic id if used>');

DELETE FROM referrals
WHERE id IN ('11111111-1111-1111-1111-111111111111', '<second synthetic id if used>');

DELETE FROM patients
WHERE id IN ('22222222-2222-2222-2222-222222222222', '<second synthetic patient id if used>');
```

- [ ] All eight deletes run, in this order, for every synthetic id used
  (including the optional second referral from Step 6).
- [ ] Re-run the `SELECT` from Step 7 (or `SELECT * FROM patient_outreach`) to
  confirm zero rows remain for the synthetic `referral_id`(s).
- [ ] Confirm no non-synthetic row was touched (the `WHERE ... IN (...)` clauses
  above are scoped to the synthetic ids specifically for this reason).

---

## Not executed

This checklist was written, not run. No `DATABASE_URL` connection was attempted,
no rows were seeded or deleted against any live database, and the app was not
started as part of producing this document. The only thing actually executed
while producing this task was the offline DDL generation in Step 1 above
(`scripts/create_outreach_table.sql`), which requires no database connection —
it was generated via SQLAlchemy's `create_mock_engine`, which compiles DDL
without connecting anywhere.
