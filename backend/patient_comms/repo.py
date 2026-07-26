"""
Supabase (Postgres) data access for the patient-comms service.

The org-facing orchestrator coordinates work by inserting rows into
`referral_actions`; this service polls the ones assigned to `twilio`, does the
patient messaging, and writes outcomes back to the shared tables:

  - consent      -> patients.consent_status + referrals.consent_confirmed_at
  - booking msg  -> patient_service_booking_details.patient_notified(_at)
  - utilization  -> referrals.patient_confirmed_utilization + patient_confirmed_at
  - escalation   -> escalations

Our own comms-lifecycle state (scheduler timings, message thread) lives in our
own tables (models.py); this module only touches the shared HSDS/referral tables.

All queries are parameterized. Reads/writes run through short transactions.
"""
import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text

_engine = None

# action_types this service handles, and the component name we poll for.
OUR_COMPONENT = "twilio"
CONSENT_ACTION = "confirm_consent"
NOTIFY_ACTION = "notify_patient"
OUR_ACTION_TYPES = (CONSENT_ACTION, NOTIFY_ACTION)


def get_engine():
    """Lazily build the Supabase engine from DATABASE_URL. pool_pre_ping keeps
    the pooled connections healthy across idle periods on Railway."""
    global _engine
    if _engine is None:
        url = os.environ["DATABASE_URL"]
        _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=2)
    return _engine


@contextmanager
def _tx(conn):
    """Yield a connection to run writes on. If `conn` is provided, reuse it
    (caller owns commit/rollback); otherwise open our own transaction."""
    if conn is not None:
        yield conn
    else:
        with get_engine().begin() as own:
            yield own


# ---------- referral_actions (the work queue) ----------

def poll_actions() -> list[dict]:
    """Pending/ready actions assigned to us whose scheduled_for has arrived."""
    sql = text("""
        SELECT id, referral_id, service_id, action_type, input_payload
        FROM referral_actions
        WHERE assigned_component = :comp
          AND action_status IN ('pending', 'ready')
          AND action_type = ANY(:types)
          AND (scheduled_for IS NULL OR scheduled_for <= now())
        ORDER BY created_at ASC
        LIMIT 50
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"comp": OUR_COMPONENT, "types": list(OUR_ACTION_TYPES)}).mappings().all()
        # Stringify the uuid columns. psycopg2 returns uuid cols as uuid.UUID
        # objects; our local patient_outreach.referral_id is VARCHAR, so an ORM
        # comparison (varchar = uuid) errors in Postgres. As strings, the local
        # comparison is varchar=text and the shared-table raw SQL coerces
        # text->uuid, so both sides work. (referral_id is the cross-track key.)
        out = []
        for r in rows:
            d = dict(r)
            for k in ("id", "referral_id", "service_id"):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            out.append(d)
        return out


def start_action(action_id, *, conn=None) -> bool:
    """Atomically claim a ready/pending action -> in_progress. Returns False if
    another worker already claimed it (so polling is safe to run concurrently)."""
    sql = text("""
        UPDATE referral_actions
        SET action_status = 'in_progress', started_at = now(), updated_at = now()
        WHERE id = :id AND action_status IN ('pending', 'ready')
    """)
    with _tx(conn) as c:
        return c.execute(sql, {"id": action_id}).rowcount == 1


def finish_action(action_id, result: dict, ok: bool = True, error: str | None = None, *, conn=None) -> None:
    import json

    sql = text("""
        UPDATE referral_actions
        SET action_status = :status, result = CAST(:result AS jsonb),
            error_message = :err, completed_at = now(), updated_at = now()
        WHERE id = :id
    """)
    with _tx(conn) as c:
        c.execute(sql, {
            "id": action_id,
            "status": "completed" if ok else "failed",
            "result": json.dumps(result or {}),
            "err": error,
        })


# ---------- reads ----------

def get_patient_for_referral(referral_id) -> dict | None:
    """Patient + referral fields needed to message them. referring_clinic_name
    is the '{clinic}' in the consent line; need_category is the service type."""
    sql = text("""
        SELECT p.id AS patient_id, p.name, p.phone, p.referring_clinic_name,
               p.consent_status, p.preferred_contact_method,
               r.id AS referral_id, r.service_id, r.need_category, r.status
        FROM referrals r JOIN patients p ON r.patient_id = p.id
        WHERE r.id = :rid
    """)
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"rid": referral_id}).mappings().first()
        return dict(row) if row else None


def get_booking_details(referral_id) -> dict | None:
    # Read from the denormalized VIEW (joins service/org names). Writes go to
    # the base table service_bookings (see mark_booking_notified/set_utilization).
    # NOTE: the live Supabase view is named v_patient_service_booking_details
    # (verified 2026-07-23 against Gyan's schema).
    sql = text("""
        SELECT referral_id, patient_id, service_id, patient_name,
               service_name, organization_name, booking_status, confirmation_number,
               scheduled_start_at, scheduled_end_at,
               pickup_address, pickup_instructions, destination_address,
               patient_instructions, provider_contact_phone
        FROM v_patient_service_booking_details
        WHERE referral_id = :rid
        ORDER BY booked_at DESC NULLS LAST
        LIMIT 1
    """)
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"rid": referral_id}).mappings().first()
        return dict(row) if row else None


# ---------- write-backs to shared tables ----------

def set_consent(patient_id, referral_id, confirmed: bool, *, conn=None) -> None:
    """patients.consent_status is the source of truth the booking agent gates on;
    also stamp referrals.consent_confirmed_at when confirmed."""
    status = "confirmed" if confirmed else "declined"
    with _tx(conn) as c:
        c.execute(text("""
            UPDATE patients SET consent_status = :s, updated_at = now() WHERE id = :pid
        """), {"s": status, "pid": patient_id})
        if confirmed:
            c.execute(text("""
                UPDATE referrals SET consent_confirmed_at = now(), updated_at = now()
                WHERE id = :rid
            """), {"rid": referral_id})


def mark_booking_notified(referral_id, *, conn=None) -> None:
    # service_bookings is the base table; the view isn't updatable.
    with _tx(conn) as c:
        c.execute(text("""
            UPDATE service_bookings
            SET patient_notified = true, patient_notified_at = now(), updated_at = now()
            WHERE referral_id = :rid
        """), {"rid": referral_id})


def set_utilization(referral_id, used: bool, *, conn=None) -> None:
    with _tx(conn) as c:
        c.execute(text("""
            UPDATE referrals
            SET patient_confirmed_utilization = :u, patient_confirmed_at = now(), updated_at = now()
            WHERE id = :rid
        """), {"u": used, "rid": referral_id})
        c.execute(text("""
            UPDATE service_bookings
            SET patient_confirmed_details = :u, patient_confirmed_at = now(), updated_at = now()
            WHERE referral_id = :rid
        """), {"u": used, "rid": referral_id})


def log_attempt(referral_id, *, channel: str, direction: str, purpose: str,
                status: str, service_id=None, provider: str = "twilio",
                outcome: str | None = None, external_id: str | None = None,
                attempt_number: int = 1, structured_result: dict | None = None,
                conn=None) -> None:
    """Record one patient-contact attempt in the shared `attempts` log.
    service_id is None for consent-stage contacts (no service selected yet)."""
    import json
    import uuid

    sql = text("""
        INSERT INTO attempts
          (id, referral_id, service_id, attempt_number, channel, provider,
           direction, purpose, status, outcome, external_id, structured_result,
           created_at, updated_at)
        VALUES
          (:id, :rid, :sid, :n, :channel, :provider, :direction, :purpose,
           :status, :outcome, :ext, CAST(:sr AS jsonb), now(), now())
    """)
    with _tx(conn) as c:
        c.execute(sql, {
            "id": str(uuid.uuid4()), "rid": referral_id, "sid": service_id,
            "n": max(1, min(3, attempt_number)), "channel": channel, "provider": provider,
            "direction": direction, "purpose": purpose, "status": status,
            "outcome": outcome, "ext": external_id,
            "sr": json.dumps(structured_result or {}),
        })


def create_escalation(referral_id, reason_code: str, handoff_summary: str, *, conn=None) -> None:
    import uuid

    with _tx(conn) as c:
        c.execute(text("""
            INSERT INTO escalations (id, referral_id, reason_code, handoff_summary, status, created_at)
            VALUES (:id, :rid, :reason, :summary, 'open', now())
        """), {"id": str(uuid.uuid4()), "rid": referral_id, "reason": reason_code, "summary": handoff_summary})


def find_open_escalation(referral_id) -> dict | None:
    """The newest still-open escalation for a referral, or None. Used to dedupe
    (don't stack a second) and to resolve on a positive follow-up."""
    sql = text("""
        SELECT id, referral_id, reason_code, status, created_at
        FROM escalations
        WHERE referral_id = :rid AND status = 'open'
        ORDER BY created_at DESC
        LIMIT 1
    """)
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"rid": referral_id}).mappings().first()
        return {**row, "id": str(row["id"]), "referral_id": str(row["referral_id"])} if row else None


def resolve_escalation(escalation_id, *, conn=None) -> None:
    sql = text("""
        UPDATE escalations SET status = 'resolved', resolved_at = now()
        WHERE id = :id AND status <> 'resolved'
    """)
    with _tx(conn) as c:
        c.execute(sql, {"id": escalation_id})


def set_preferred_contact_method(patient_id, method: str, *, conn=None) -> None:
    sql = text("""
        UPDATE patients SET preferred_contact_method = :m, updated_at = now()
        WHERE id = :pid
    """)
    with _tx(conn) as c:
        c.execute(sql, {"m": method, "pid": patient_id})
