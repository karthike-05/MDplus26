-- Patient-outreach loop: LOCAL tables (patient_outreach, messages)
--
-- Generated from models.py (Base.metadata) via SQLAlchemy's postgresql DDL
-- compiler -- see the command at the bottom of this file to regenerate.
--
-- IMPORTANT: these two tables are ours (loop-owned comms state only -- stage
-- cursor, scheduling timestamps, attempt counters, message thread). They must
-- be applied to the SAME Supabase database/schema as Gyan's shared tables
-- (patients, referrals, referral_actions, service_bookings, attempts,
-- escalations), because main.py's inbound webhook writes to both our tables
-- and his in a single DB transaction (see repo.py / outreach_repo.py). If
-- these land in a different database, that transaction cannot span both and
-- the webhook's atomicity guarantee breaks.
--
-- `stage` is emitted as a native Postgres enum type (CREATE TYPE stage AS ENUM ('consent', 'awaiting_booking', 'notified', 'reminded', 'verifying', 'done', 'escalated');

CREATE TABLE patient_outreach (
	id VARCHAR NOT NULL, 
	referral_id VARCHAR NOT NULL, 
	patient_phone VARCHAR NOT NULL, 
	stage stage NOT NULL, 
	active_action_id VARCHAR, 
	paused BOOLEAN NOT NULL, 
	next_consent_retry_at TIMESTAMP WITHOUT TIME ZONE, 
	next_reminder_at TIMESTAMP WITHOUT TIME ZONE, 
	next_verify_at TIMESTAMP WITHOUT TIME ZONE, 
	next_nudge_at TIMESTAMP WITHOUT TIME ZONE, 
	consent_retry_sent_at TIMESTAMP WITHOUT TIME ZONE, 
	reminder_sent_at TIMESTAMP WITHOUT TIME ZONE, 
	verification_sent_at TIMESTAMP WITHOUT TIME ZONE, 
	nudge_sent_at TIMESTAMP WITHOUT TIME ZONE, 
	consent_attempts INTEGER NOT NULL, 
	verification_attempts INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_patient_outreach_patient_phone ON patient_outreach (patient_phone);

CREATE INDEX ix_patient_outreach_referral_id ON patient_outreach (referral_id);

CREATE TABLE messages (
	id VARCHAR NOT NULL, 
	outreach_id VARCHAR NOT NULL, 
	direction VARCHAR NOT NULL, 
	stage VARCHAR, 
	body VARCHAR NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_messages_outreach_id ON messages (outreach_id);

