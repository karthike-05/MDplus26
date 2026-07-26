#!/usr/bin/env python3
"""
Add a mock patient to Supabase so the DEPLOYED agent (Railway) texts them over
real WhatsApp — no join code needed (uses the approved consent template).

  python3 scripts/add_mock_patient.py "Full Name" "+1XXXXXXXXXX" [service_type]

What it does: inserts a patient + referral + a `confirm_consent` action
(assigned to 'twilio'). Within ~2 seconds the deployed Loop A picks it up and
sends the consent WhatsApp. The person replies YES on WhatsApp -> the loop
advances -> booking/reminder/verification follow on the compressed schedule.

Watch progress at:  https://ptcomm-outreach-production.up.railway.app

After they reply YES, simulate the org side booking the service (sends the
booking details, then reminder + verification follow automatically):
  python3 scripts/add_mock_patient.py --notify "+1XXXXXXXXXX"

Remove everything this tool added (all mock patients + their data):
  python3 scripts/add_mock_patient.py --cleanup
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.getcwd(), ".env"))
if not os.environ.get("DATABASE_URL"):
    print("DATABASE_URL not set in .env"); sys.exit(1)
from sqlalchemy import create_engine, text

eng = create_engine(os.environ["DATABASE_URL"])
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mock_patients.json")


def _load(): return json.load(open(STATE)) if os.path.exists(STATE) else []
def _save(d): json.dump(d, open(STATE, "w"))


def add(name, phone, service):
    if not phone.startswith("+"):
        print("Phone must be E.164, e.g. +18165551234"); sys.exit(1)
    rid, pid, aid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    with eng.begin() as c:
        c.execute(text("INSERT INTO patients (id,name,phone,referring_clinic_name,consent_status) "
                       "VALUES (:i,:n,:p,:cl,'pending')"),
                  {"i": pid, "n": name, "p": phone, "cl": "KU Health Liberty"})
        c.execute(text("INSERT INTO referrals (id,patient_id,need_category,status) "
                       "VALUES (:i,:p,:nc,'waiting_for_consent')"),
                  {"i": rid, "p": pid, "nc": service})
        c.execute(text("INSERT INTO referral_actions (id,referral_id,action_type,assigned_component,action_status,deduplication_key) "
                       "VALUES (:i,:r,'confirm_consent','twilio','ready',:d)"),
                  {"i": aid, "r": rid, "d": f"mockadd-{rid}"})
    st = _load(); st.append({"rid": rid, "pid": pid, "name": name, "phone": phone}); _save(st)
    print(f"✅ Added {name} ({phone}) for '{service}'.")
    print(f"   The deployed agent will send the consent WhatsApp within ~2 seconds.")
    print(f"   Watch: https://ptcomm-outreach-production.up.railway.app")


def notify(phone):
    st = _load()
    match = [r for r in st if r["phone"] == phone]
    if not match:
        print(f"No mock patient with phone {phone} — add one first."); sys.exit(1)
    rid, pid = match[-1]["rid"], match[-1]["pid"]
    aid = str(uuid.uuid4())
    with eng.begin() as c:
        # Seed a booking with mock details so the booking-details + reminder
        # messages carry real content (otherwise get_booking_details returns
        # nothing and the messages say "Details to follow.").
        svc = c.execute(text("SELECT id FROM services LIMIT 1")).scalar()
        if svc is not None:
            c.execute(text("""
                INSERT INTO service_bookings
                  (id, referral_id, patient_id, service_id, booking_status,
                   scheduled_start_at, pickup_address, patient_instructions, confirmation_number)
                VALUES (:i,:r,:p,:s,'booked',:sched,:addr,:instr,:conf)
            """), {
                "i": str(uuid.uuid4()), "r": rid, "p": pid, "s": svc,
                # Far enough out that the reminder (service - 2 "days") lands a
                # couple minutes AFTER the booking message, not on top of it.
                "sched": datetime.utcnow() + timedelta(minutes=4),
                "addr": "5th & Main St, Kansas City, MO 64106",
                "instr": "Bring your Medicaid card and a photo ID; your driver arrives in a blue van.",
                "conf": "TR-" + rid[:6].upper(),
            })
        else:
            print("  (no services row found — booking details will be generic)")
        c.execute(text("INSERT INTO referral_actions (id,referral_id,action_type,assigned_component,action_status,deduplication_key) "
                       "VALUES (:i,:r,'notify_patient','twilio','ready',:d)"),
                  {"i": aid, "r": rid, "d": f"mocknotify-{aid}"})
    print(f"✅ Queued booking (with mock details) + notification for {phone}.")
    print("   The agent sends booking details within ~2s, then reminder + verification")
    print("   fire automatically on the compressed schedule. Reply YES to the verification")
    print("   to close the loop as 'used the service'.")


def cleanup():
    st = _load()
    if not st:
        print("No mock patients to remove."); return
    for row in st:
        rid, pid = row["rid"], row["pid"]
        for sql, kw in [
            ("DELETE FROM attempts WHERE referral_id=:r", {"r": rid}),
            ("DELETE FROM escalations WHERE referral_id=:r", {"r": rid}),
            ("DELETE FROM messages WHERE outreach_id IN (SELECT id FROM patient_outreach WHERE referral_id=:r)", {"r": rid}),
            ("DELETE FROM patient_outreach WHERE referral_id=:r", {"r": rid}),
            ("DELETE FROM referral_actions WHERE referral_id=:r", {"r": rid}),
            ("DELETE FROM service_bookings WHERE referral_id=:r", {"r": rid}),
            ("DELETE FROM referrals WHERE id=:r", {"r": rid}),
            ("DELETE FROM patients WHERE id=:p", {"p": pid}),
        ]:
            try:
                with eng.begin() as c: c.execute(text(sql), kw)
            except Exception as e:
                print("  warn:", sql[:38], str(e)[:70])
        print(f"  removed {row['name']} ({row['phone']})")
    os.remove(STATE)
    print("🧹 All mock patients removed.")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--cleanup":
        cleanup()
    elif len(sys.argv) == 3 and sys.argv[1] == "--notify":
        notify(sys.argv[2])
    elif len(sys.argv) >= 3 and not sys.argv[1].startswith("--"):
        add(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "transportation")
    else:
        print(__doc__)
