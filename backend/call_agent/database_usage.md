# Call Agent — Database Usage

This file is the **authority** on how the call_agent reads and writes the database. If code disagrees with this file, fix the code (or update this file deliberately, in the same change).

## Receives
- `referral_id`

## Reads

**referrals**
`id, patient_id, service_id, need_category, status, urgency, current_resource_rank, consent_confirmed_at, assigned_to, escalation_reason, completed_at, completion_outcome, patient_confirmed_utilization, patient_confirmed_at`

**patients**
`id, name, phone, county, consent_status, preferred_language, preferred_contact_method, date_of_birth, household_size, income_status, insurance_type, insurance_member_id, mobility_needs, is_veteran, appointment_date, appointment_location, referring_clinic_name` — `appointment_date`/`appointment_location` are sent as `appointment_time`/`appointment_location` dynamic variables at call start (not looked up mid-call).

**service_requests** (the ask — request details collected before a booking exists)
`id, referral_id, patient_id, service_id, request_status, requested_date, requested_start_time, requested_end_time, pickup_address, destination_address, pickup_notes, destination_notes, flexibility_minutes, mobility_requirements, companion_required, interpreter_required, accessibility_notes, contact_phone, contact_email, emergency_contact, special_instructions, request_notes` — `pickup_notes, emergency_contact, special_instructions, request_notes` are surfaced to the agent mid-call via the optional `get_service_request_details`/`GET /lookup-service-request-details` lookup (see below), keyed by `referral_id` (`case_id`).

**attempts** (previous contact attempts, all channels)
`id, referral_id, service_id, attempt_number, channel, provider, direction, purpose, status, outcome, external_id, structured_result, transcript_url, error_code, notes, started_at, completed_at`

**applications** (existing form-filled applications, if any)
`id, referral_id, service_id, form_template_id, status, populated_fields, redacted_preview, approval_required, submitted_at, external_confirmation_id, submission_receipt, error_message`

**service_bookings** (existing booking, if one exists)
`id, referral_id, patient_id, service_id, application_id, booking_status, confirmation_number, scheduled_start_at, scheduled_end_at, pickup_address, pickup_instructions, destination_address, destination_instructions, provider_contact_phone, patient_instructions, cancellation_instructions, external_booking_id, provider_response, patient_notified, patient_confirmed_details, booked_at, cancelled_at, completed_at`

**services / organizations / phones / organization_contact_preferences** (who/how to call)
- `services`: `id, organization_id, name, description, status, application_process, fees_description, eligibility_description, minimum_age, maximum_age, need_category`
- `organizations`: `id, name, alternate_name, email, website`
- `phones`: `id, number, extension, type, description, organization_id, service_id, location_id`
- `organization_contact_preferences`: `id, organization_id, contact_method, contact_value, purpose, priority, is_active, instructions, last_verified_at` — ranked, use highest-priority active row per purpose

## Writes

**attempts** — insert one row per outbound call. Note: `channel` check constraint only allows `online_form, phone, email, sms, whatsapp` (no `voice`/`call`), and `provider` check constraint only allows `twilio, retell, karthik_form, manual, internal` (never the organization name — the org is derivable via `service_id`).
`referral_id, service_id, attempt_number, channel='phone', provider='retell', purpose, status='completed' (this webhook only fires after Retell's agent completed a conversation), outcome (see mapping below), external_id (Retell call_id — idempotency key for post-call webhook, checked before insert), structured_result, notes`

**service_bookings** — update keyed on `id` (booking_id) + `referral_id`
`booking_status (see mapping below), confirmation_number, scheduled_start_at, booked_at, pickup_instructions, destination_instructions, cancellation_instructions, patient_instructions`

**Retell outcome → DB vocab mapping** (`db._OUTCOME_MAP` in `db.py` is the single source of truth — update both places together if this changes):

| Retell `status` | `attempts.outcome` | `service_bookings.booking_status` |
|---|---|---|
| `confirmed` | `scheduled` | `confirmed` |
| `ineligible` | `ineligible` | `cancelled` |
| `unavailable` | `rejected` | `cancelled` |
| `callback_required` | `needs_human_followup` | *(unchanged)* |
| `escalation_needed` | `needs_human_followup` | `rescheduling_required` |
| `alt_slot_offered` | `scheduled` | `rescheduling_required` |

`accessibility_accommodations` on `service_bookings` is not read or relayed by the call agent — Retell's transportation agent prompt only consumes patient accessibility info via the `mobility_needs` dynamic variable (sourced from `patients.mobility_needs`).

**escalations** — insert if the three-attempt protocol is exhausted or a hard blocker is hit
`referral_id, reason_code, handoff_summary, assigned_social_worker, status='open'`

**Not yet implemented in code** (documented target scope, not currently written by `main.py`/`db.py` — implement when the corresponding flow is built):
- **referrals** — update `status, current_resource_rank, escalation_reason, completed_at, completion_outcome` as the referral overall resolves
- **patient_call_notes** — insert `patient_id, referral_id, attempt_id, organization_id, note_type, note_text, call_outcome, follow_up_required, follow_up_at, patient_visible, created_by_agent='call_agent'` when a call surfaces structured info worth persisting
- **applications** reads — not queried by the current transportation-confirmation call flow (`place_referral_call` only reads an existing `service_bookings` row)

## Idempotency
Post-call webhooks must be keyed by `attempts.external_id` (Retell `call_id`) — check for an existing row with that `external_id` before inserting/updating on webhook retry.
