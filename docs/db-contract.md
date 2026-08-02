# DB contract — what the backend needs from Supabase

This is the **minimal** set of tables/columns the form-fill + orchestration code
reads and writes, so all four workstreams (form / SMS / phone / ranking) can share one
database without colliding. Adapt-don't-rewrite: our adapter (`backend/db/supabase.py`)
translates the shared schema's column names to our contract keys, so **rename freely** —
just update the maps at the top of that file. The few things below that genuinely have to
*exist* are marked **REQUIRED**; everything else the adapter maps to whatever the shared
schema already has.

> **Status (2026-07-27): this document describes the ORIGINAL contract, and the live
> schema won the argument on three points.** Kept for the rationale; where it disagrees
> with the live DB, the live DB wins and `backend/db/supabase.py`'s `*_COLS` maps are
> authoritative. Verify anytime with `python -m backend.scripts.db_introspect`.
>
> 1. **`referrals.current_state` does not exist and must never be added** (§7a). The live
>    DB ships its own scheduler, `advance_referral()`, plus a `referral_actions` queue; a
>    second state column would be a second owner of truth. `set_state()` is a documented
>    no-op on both real adapters, and the dashboard translates *their* `status` for
>    display only.
> 2. **`referrals.form_id` does not exist.** The form is resolved through
>    `form_templates.service_id` — a better design, since a form belongs to a service
>    rather than to each referral.
> 3. **`attempts.attempt_id` / `from_state` do not exist**, and aren't needed:
>    `referral_actions(referral_id, deduplication_key)` is already the idempotency key.
>    But `attempts.attempt_number` **is** NOT NULL with no default and carries a UNIQUE
>    `(referral_id, service_id, attempt_number)` — see `next_attempt_number()`.

> The DB is the integration bus (CLAUDE.md §2, §5a). Nobody imports anybody's code;
> everyone reads/writes rows. That's what ties form/SMS/phone back together.

---

## `patients` (read + insert)
Any columns; the adapter maps them. Used as form-fill `source` values:
`name`, `dob`, `phone`, `address`, `medicaid_id`, `mobility_needs`, `household_size`.
- **REQUIRED:** a primary key we can return on insert (`id`).

## `referrals` (read + insert + update)
- **REQUIRED:** `id` (pk), `patient_id` (fk), `form_id` (which form to fill),
  and **`current_state` (text)** — this is the scheduler's spine (§7). Without a
  persisted `current_state` the workflow can't advance.
- Optional (mapped): `service_name`, `referring_clinic`, `appointment_date`,
  `appointment_time`.
- **`need_category` (text, 2026-07-24)** — a real column in the live HSDS schema
  (`docs/integration-status.md`), read directly by `backend/service_ranking`'s
  `rank_referral(referral_id)`. Our mock backfills it from the chosen service's
  existing `category` at creation time (slugified — `backend/main.py`'s
  `_slugify_category`); Data's real schema populates it independently.
- New referrals are inserted with `current_state = 'created'`.
- **`service_id` is updatable post-creation (2026-07-24)** — `ReferralDB.set_referral_service(referral_id, service_id, **fields)`
  (`backend/db/interface.py`). CONTRACT TOUCH — added so a social worker can act on
  `backend/service_ranking`'s output (ranking runs upstream, picks candidates; we only
  consume the chosen `service_id`, per CLAUDE.md §2). See
  `backend/service_ranking/integration_plan_service_ranking.md`.

### `current_state` vocabulary (the state machine, §7)
```
created · consent_pending · consent_granted · outreach_in_progress ·
submitted · needs_human · confirmed · check_in_scheduled · completed · escalated
```

## `outreach_attempts` (insert/upsert) — **the shared write contract**
Every attempt by **any** of the three methods writes one row here in the
`ToolOutcome` shape (`contracts/models.py`). This is the single most important table
to agree on, because form/SMS/phone all write it and the scheduler reads it.
- **REQUIRED columns:**
  - `attempt_id` (text) **with a UNIQUE constraint** — idempotency key (§10); the
    upsert is `ON CONFLICT (attempt_id)`. Without UNIQUE, retries duplicate rows.
  - `referral_id` (fk)
  - `channel` (text) — `form | email | phone | whatsapp | escalation`
  - `status` (text) — `success | needs_human | failed`
  - `from_state` (text, nullable) — the state the attempt was produced for
  - `data` (jsonb) — tool-specific payload (confirmation, output path, problems…)
  - `error` (text, nullable)

> **Enums:** `channel` and `status` above are frozen. If Messaging/Voice write other
> strings, the scheduler's transition table (keyed on `(from_state, status)`) won't
> match and referrals stall. Consider a CHECK constraint on `status` to enforce it.

## `form_schemas` — **not needed by this backend**
We load schemas from the authoritative JSON in `contracts/schemas/` (§5c), so we
touch none of the form tables. If a cache table already exists, fine — we ignore it.

---

## Tables this backend does NOT touch
`social_services`, `check_ins` — owned by others; no reads/writes from form-fill.
`ranking_results`, `sw_feedback` — owned by `backend/service_ranking`; we only proxy
its HTTP endpoints and consume the `service_id` a social worker chooses
(`set_referral_service`, above) — no direct reads/writes of its tables.

## To activate the real DB
**Preferred path = the REST API (service_role key), not a Postgres DSN.** The direct
DSN is IPv6-only/flaky and the DB password was unreliable; the API is HTTPS/IPv4 and
uses the same auth the Voice arm uses. `make_db()` picks the API adapter when
`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` are set (falls back to DSN, then mock).

1. ~~Apply `001_orchestration_bus.sql`~~ — **do not.** That migration is **obsolete** and
   would add a second owner of workflow state plus fork the outreach log away from the
   `attempts` table the ranker reads. The file carries a banner explaining why. The only
   migration applied is `002_utilization_milestone.sql`.
2. Align `backend/db/supabase.py`'s `*_COLS` maps — **already done** (2026-07-26);
   `db_introspect` reports `patients: OK` and `services: OK`.
3. Set `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` in `.env`. Until you do, the backend
   stays on the mock. The dashboard's **Use Supabase** button flips it at runtime.
4. Smoke test: `python -m backend.scripts.db_introspect`, then one `get_patient`.

> Expect the 3 live referrals to sit in "In progress": nothing writes
> `referral_service_candidates`, which `advance_referral()` reads, so they cannot advance
> yet. That blocker is upstream of us — see
> **[`integration-status.md`](integration-status.md)**.

---

## Reconciled with the live DB (updated 2026-07-26)

**The maps are now aligned** — `python -m backend.scripts.db_introspect` reports
`patients: OK` and `services: OK`. The sections above describe our *contract keys*; the
right-hand side of each `*_COLS` map holds the real column. A `None` there means the
column doesn't exist, annotated with where the value actually comes from.

- **`patients`**: `dob` → `date_of_birth`; `medicaid_id` → `insurance_member_id`;
  `referring_clinic` → `referring_clinic_name`. **No `address` column at all** — only
  `postal_code`/`county`/lat-long, and the `addresses` table is keyed by `location_id`
  (service locations, not homes). An INSERT must supply `name`, `phone` and
  `referring_clinic_name` (NOT NULL, no default).
- **`referrals`**: their `status` is the live orchestration field.
  **`current_state` is deliberately NOT added** — see below.
- **`services`** (the table is `services`, not `social_services`): `website` → `url`;
  `category` → `need_category`. No `preferred_channel`/`form_id`/`phone` columns because
  better ones exist: **`service_application_channels`** (channel + priority +
  `application_url` + `channel_contact`) and **`form_templates`**.
- **`attempts`** *is* the shared outreach log — `data` → `structured_result`, `error` →
  `notes`, and our one `status` becomes their (`status`, `outcome`) pair. Never fork it:
  the ranker reads it for responsiveness.
- **`service_requests`** holds the trip payload a form fills, and Voice reads the same
  row (CLAUDE.md §6a).

### ⚠ Two things this document used to say, now reversed

1. **Do not add `referrals.current_state`, and do not create `outreach_attempts`.** The
   live DB owns a scheduler (`advance_referral()`) that dispatches through
   `referral_actions`; a second state field would be a second owner of truth.
   `001_orchestration_bus.sql` is **obsolete — do not run it.**
2. **`attempt_id` + a UNIQUE constraint are not needed.** Their
   `referral_actions(referral_id, deduplication_key)` unique index already provides that
   idempotency.

Architecture and blockers: [`integration-status.md`](integration-status.md).
