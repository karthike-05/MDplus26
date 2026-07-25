# DB contract — what the backend needs from Supabase

This is the **minimal** set of tables/columns the form-fill + orchestration code
reads and writes, so three teams (form / SMS / phone) can share one database
without colliding. Adapt-don't-rewrite: our adapter (`backend/db/supabase.py`)
translates *his* column names to our contract keys, so **rename freely** — just
update the maps at the top of that file. The few things below genuinely have to
*exist* are marked **REQUIRED**; everything else the adapter can map to whatever
Data already has.

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
`SUPABASE_URL` + `SUPABASE_SERVICE_KEY` are set (falls back to DSN, then mock).

1. Apply `contracts/migrations/001_orchestration_bus.sql` in the Supabase SQL Editor.
2. Align `backend/db/supabase.py`'s `*_COLS` maps to Data's real column names
   (reconciled table below).
3. Set `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` in `.env`.
4. Smoke test: `python -m backend.scripts.db_introspect`, then one `get_patient` +
   one `record_attempt`. Until step 3, the backend stays on the mock.

See **[`integration-status.md`](integration-status.md)** for the full flip procedure
and the "wait until the schema is frozen" rationale.

---

## Reconciled with the live DB (2026-07-23)
The live DB differs from the defaults above. The seam absorbs this — update the
`*_COLS` maps; no upstream code moves. Key deltas (full table in `integration-status.md`):

- **`patients`**: `dob` → `date_of_birth`; no `address` column; `medicaid_id` ≈
  `insurance_member_id`.
- **`referrals`**: **no `current_state`** (their `status` is different — don't reuse)
  and **no `form_id`** → both added by the migration / derived from `need_category`.
- **`services`** (table is `services`, not `social_services`): `website` → `url`;
  `category` → `need_category`; **no contact-`phone` column** (gap for the phone
  channel); no `preferred_channel`/`form_id` (derive from `need_category`).
- **`outreach_attempts`**: does not exist yet → created by the migration. Voice's
  `attempts` table is a different shape; we do not write it.
