-- 001_orchestration_bus.sql
-- Additive migration to make the shared Supabase DB the integration bus for the
-- three agents (form / voice / text) + the UI, per docs/db-contract.md and
-- docs/integration-status.md. Run once in the Supabase SQL Editor.
--
-- SAFETY: additive only. It ADDs two columns and CREATEs one table. It RENAMES,
-- DROPs, and CHANGES nothing that already exists — every query the Voice/Data
-- code runs today returns the same result afterward. Teammates' existing columns
-- (referrals.status, the attempts table, etc.) are untouched. Idempotent: safe to
-- re-run (guards with IF NOT EXISTS).
--
-- WHY THESE TWO THINGS (the only gaps found on 2026-07-23, see integration-status.md):
--   1. referrals has no canonical orchestration state in OUR vocabulary. Their
--      `status` column ('not_started', ...) means something different and is read
--      by Voice/Data, so we do NOT reuse it — we add a separate `current_state`.
--   2. There is no shared outcome log in the ToolOutcome shape. Voice's `attempts`
--      table has a different shape and no idempotency key, so we add a dedicated
--      `outreach_attempts` table that every channel writes to.

-- 1) Canonical state on the shared referral row (the scheduler's spine, §7).
--    DEFAULT 'created' backfills the existing referral(s) to the loop's start state.
ALTER TABLE referrals
  ADD COLUMN IF NOT EXISTS current_state text NOT NULL DEFAULT 'created',
  ADD COLUMN IF NOT EXISTS form_id       text;

-- current_state vocabulary (must match backend/orchestrator/state_machine.py):
--   created · consent_pending · consent_granted · outreach_in_progress ·
--   submitted · needs_human · confirmed · check_in_scheduled · completed · escalated
-- Optional hardening once the vocab is agreed team-wide:
--   ALTER TABLE referrals ADD CONSTRAINT referrals_current_state_chk
--     CHECK (current_state IN ('created','consent_pending','consent_granted',
--       'outreach_in_progress','submitted','needs_human','confirmed',
--       'check_in_scheduled','completed','escalated'));

-- 2) The shared write contract — one row per attempt by ANY channel (§5b).
--    RECONCILE BEFORE APPLYING (see docs/integration-status.md "Ranking system"):
--    The ranking system reads the EXISTING `attempts` table for its
--    responsiveness score. A separate `outreach_attempts` would fork the outreach
--    history and starve the ranker. Preferred convergence = extend `attempts`
--    (ADD attempt_id/from_state; map data->structured_result, error->notes) and
--    DROP this CREATE. Kept here only as the fallback if the team opts to keep the
--    two logs separate. Do not apply this block until that call is made.
CREATE TABLE IF NOT EXISTS outreach_attempts (
  attempt_id  text        NOT NULL UNIQUE,   -- idempotency key (§10): upsert ON CONFLICT
  referral_id text        NOT NULL,
  channel     text,                          -- form | phone | whatsapp | email | escalation
  status      text,                          -- success | needs_human | failed
  from_state  text,                          -- state the attempt was produced for
  data        jsonb       NOT NULL DEFAULT '{}'::jsonb,
  error       text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Fast timeline reads for the dashboard (list_attempts orders by created_at).
CREATE INDEX IF NOT EXISTS outreach_attempts_referral_idx
  ON outreach_attempts (referral_id, created_at);

-- Optional FK (enable once you're sure referral ids line up):
--   ALTER TABLE outreach_attempts
--     ADD CONSTRAINT outreach_attempts_referral_fk
--     FOREIGN KEY (referral_id) REFERENCES referrals (id);
