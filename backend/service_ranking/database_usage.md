# Service Ranking — Database Usage

This file is the **authority** on how the service ranking system reads from and writes to the database. If code disagrees with this file, fix the code (or update this file deliberately, in the same change). See `ranking_system_plan.md` for the design rationale behind each layer.

## Receives
- `referral_id`

## Reads

**referrals**
`id, patient_id, service_id, need_category, status, urgency`

**patients**
`id, county, date_of_birth, insurance_type, latitude, longitude, need_description`

**services** (candidates in the referral's `need_category`, `status = 'active'`)
`id, organization_id, name, description, eligibility_description, status, need_category, minimum_age, maximum_age, accepted_insurance, response_time_hours`

**service_areas** — free-text area names per service (e.g. "Clay County, MO"), matched against `patients.county` by substring since there's no structured geography yet
`service_id, name`

**service_at_location** / **locations** — service_id → lat/long (or `location_type = 'virtual'`) for the Layer 2 distance score
`service_at_location.service_id, service_at_location.location_id`
`locations.id, location_type, latitude, longitude`

**cost_options** — cheapest `amount` per service for the Layer 2 cost score
`service_id, amount`

**schedules** — used only to check whether a service has unrestricted/24-hour hours vs. fixed hours (no per-referral requested-time field exists yet to do a real overlap check)
`service_id, byday, opens_at, closes_at`

## Writes

**ranking_results** — one row per candidate service per referral (survivors and rejects both), upserted on `(referral_id, service_id)`
`referral_id, service_id, passed_hard_filter, filter_reject_reason, objective_score, objective_breakdown, subjective_score, subjective_rationale, combined_score, rank`

**sw_feedback** — inserted when a social worker picks a service and labels it (`good_fit`, `wrong_service`, `too_far`, `insurance_mismatch`, `other`)
`referral_id, service_id, label, label_notes`
`need_embedding` is **not yet populated** — no embedding provider is wired up, so this column stays null until the few-shot feedback loop (plan §5/§2d) is implemented against real feedback data.

## Not yet implemented
- **Layer 3 few-shot retrieval** — `run_subjective_scoring` in `ranking.py` does not query `sw_feedback` for similar past cases via pgvector; there's nothing to retrieve yet since no feedback rows exist. Requires picking an embedding provider once there's real data to test against.
- **`response_time_hours` backfill** (plan §4) — the nightly job that derives this from `attempts` outcomes is not implemented; `attempts` has no data yet to validate it against. Until then, Layer 2 treats `response_time_hours = null` as neutral (score 70), not zero.
- **Hours/timing match** — currently a schedule-breadth heuristic only (24-hour/no-restriction services score higher), not a real overlap check against a requested time, since no per-referral requested-time field exists at ranking stage.

## Entry points (`ranking.py`)
- `rank_referral(referral_id)` — runs all three layers, upserts `ranking_results`, returns the SW-facing ranked list (survivors only, ordered by rank, with `service_name`/`organization_name` attached).
- `record_sw_feedback(referral_id, service_id, label, label_notes=None)` — writes the SW's choice + label to `sw_feedback`.
