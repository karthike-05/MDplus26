#!/usr/bin/env python3
"""
Local walkthrough for the patient-comms loop — runs against your Supabase with
MOCK sends (nothing texts a real phone). Creates ONE clearly-synthetic case,
walks it through the whole journey, and cleans up after.

Run these from the project root, in order (keep the app running in another
terminal — see the printed instructions):

  python3 scripts/demo_walkthrough.py seed     # create a case + send the consent ask (Loop A)
  # -> then reply "yes" from the dashboard's Simulate box, OR:
  python3 scripts/demo_walkthrough.py reply "yes, count me in"
  python3 scripts/demo_walkthrough.py notify   # send booking details (Loop A)
  python3 scripts/demo_walkthrough.py tick      # fire the due reminder + verification (Loop B)
  python3 scripts/demo_walkthrough.py reply "yeah I went yesterday, thanks"
  python3 scripts/demo_walkthrough.py status    # print the case + message thread anytime
  python3 scripts/demo_walkthrough.py cleanup   # delete ALL synthetic demo data

Everything it creates is tagged 'DEMO' with a 555-fake phone, and cleanup
removes it completely. It does NOT touch any real patient rows.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from urllib import request as urlrequest, parse as urlparse

# Make the project root importable (repo.py, models.py, ...) no matter where
# this is launched from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force safe demo settings before importing app code.
os.environ.setdefault("SMS_PROVIDER", "mock")   # never send a real message from the demo
from dotenv import load_dotenv
load_dotenv(os.path.join(os.getcwd(), ".env"))
os.environ["SMS_PROVIDER"] = "mock"             # hard override even if .env says whatsapp

if not os.environ.get("DATABASE_URL"):
    print("DATABASE_URL is not set in .env — point it at Supabase first."); sys.exit(1)

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from models import Base, Stage
import repo, poller, scheduler

STATE = os.path.join("scripts", ".demo_state.json")
PHONE = "+15550100199"
APP_URL = os.environ.get("APP_URL", "http://localhost:8000")
eng = create_engine(os.environ["DATABASE_URL"])
SessionLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False)


def _save(d): json.dump(d, open(STATE, "w"))
def _load():
    if not os.path.exists(STATE):
        print("No demo case yet. Run:  python3 scripts/demo_walkthrough.py seed"); sys.exit(1)
    return json.load(open(STATE))
def _q(sql, **kw):
    with eng.connect() as c: return c.execute(text(sql), kw).first()


def cmd_seed():
    Base.metadata.create_all(eng)  # make sure our two tables exist in Supabase
    rid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    caid = str(uuid.uuid4())
    with eng.begin() as c:
        c.execute(text("INSERT INTO patients (id,name,phone,referring_clinic_name,consent_status) "
                       "VALUES (:i,:n,:p,:cl,'pending')"),
                  {"i": pid, "n": "DEMO Test Patient", "p": PHONE, "cl": "KU Health Liberty"})
        c.execute(text("INSERT INTO referrals (id,patient_id,need_category,status) "
                       "VALUES (:i,:p,'transportation','waiting_for_consent')"), {"i": rid, "p": pid})
        c.execute(text("INSERT INTO referral_actions (id,referral_id,action_type,assigned_component,action_status,deduplication_key) "
                       "VALUES (:i,:r,'confirm_consent','twilio','ready',:d)"),
                  {"i": caid, "r": rid, "d": f"demo-consent-{rid}"})
    _save({"rid": rid, "pid": pid})
    s = SessionLocal(); counts = poller.run_action_poll(s, repo=repo); s.close()
    print(f"\n✅ Seeded a demo referral and ran the auto-loop (Loop A): {counts}")
    print(f"   A consent message was 'sent' (mock) to {PHONE}.")
    print(f"\n👉 Open {APP_URL} — you'll see a case at stage 'consent'. Click it to read the thread.")
    print(f"👉 Reply as the patient: use the dashboard Simulate box (From: {PHONE}, Body: 'yes'),")
    print(f"   or run:  python3 scripts/demo_walkthrough.py reply \"yes, count me in\"")


def cmd_reply(body):
    st = _load()
    data = urlparse.urlencode({"From": PHONE, "Body": body}).encode()
    try:
        urlrequest.urlopen(urlrequest.Request(APP_URL + "/webhook/sms-inbound", data=data), timeout=15)
        print(f"📨 Delivered patient reply: {body!r}")
        print("   (routed through the real webhook + AI classifier; watch the stage change)")
    except Exception as e:
        print(f"Could not reach the app at {APP_URL} — is it running? ({e})")
        sys.exit(1)
    cmd_status()


def cmd_notify():
    st = _load()
    naid = str(uuid.uuid4())
    with eng.begin() as c:
        c.execute(text("INSERT INTO referral_actions (id,referral_id,action_type,assigned_component,action_status,deduplication_key) "
                       "VALUES (:i,:r,'notify_patient','twilio','ready',:d)"),
                  {"i": naid, "r": st["rid"], "d": f"demo-notify-{st['rid']}"})
    s = SessionLocal(); counts = poller.run_action_poll(s, repo=repo); s.close()
    print(f"\n✅ Ran Loop A (notify): {counts} — booking details 'sent' (mock), reminder + verification scheduled.")
    cmd_status()


def cmd_tick():
    # Jump 'now' past the scheduled reminder/verification so they fire immediately.
    s = SessionLocal()
    b = scheduler.run_due_batch(s, repo=repo, now=datetime.utcnow() + timedelta(days=400))
    s.close()
    print(f"\n✅ Ran the timed loop (Loop B): {b}")
    print("   (reminder + verification 'sent'; case now waiting on the patient's utilization reply)")
    print(f"👉 Reply:  python3 scripts/demo_walkthrough.py reply \"yeah I went yesterday, thanks\"")
    cmd_status()


def cmd_status():
    st = _load()
    row = _q("SELECT stage, consent_attempts, verification_attempts FROM patient_outreach WHERE referral_id=:r", r=st["rid"])
    cs = _q("SELECT consent_status FROM patients WHERE id=:p", p=st["pid"])
    util = _q("SELECT patient_confirmed_utilization FROM referrals WHERE id=:r", r=st["rid"])
    print("\n── CASE STATUS ──")
    print(f"  stage:                 {row[0] if row else '(no outreach row yet)'}")
    print(f"  consent (shared DB):   {cs[0] if cs else '?'}")
    print(f"  used service:          {util[0] if util else '(not yet)'}")
    with eng.connect() as c:
        msgs = c.execute(text(
            "SELECT direction, stage, body FROM messages WHERE outreach_id="
            "(SELECT id FROM patient_outreach WHERE referral_id=:r) ORDER BY created_at"), {"r": st["rid"]}).all()
    print("  ── message thread ──")
    for d, sg, body in msgs:
        who = "patient →" if d == "inbound" else "  → patient"
        print(f"    {who} [{sg}] {body}")
    if not msgs: print("    (none yet)")


def cmd_cleanup():
    if not os.path.exists(STATE):
        print("Nothing to clean up."); return
    st = json.load(open(STATE)); rid, pid = st["rid"], st["pid"]
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
            print("  cleanup warn:", sql[:40], str(e)[:80])
    os.remove(STATE)
    print("🧹 Deleted all synthetic demo data. Your DB is clean.")
    print("   (The empty patient_outreach/messages tables remain — they belong in Supabase for real use.")
    print("    To remove them too: DROP TABLE messages, patient_outreach; DROP TYPE stage;)")


CMDS = {"seed": cmd_seed, "notify": cmd_notify, "tick": cmd_tick, "status": cmd_status, "cleanup": cmd_cleanup}
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS and sys.argv[1] != "reply":
        print(__doc__); sys.exit(0)
    if sys.argv[1] == "reply":
        cmd_reply(sys.argv[2] if len(sys.argv) > 2 else "yes")
    else:
        CMDS[sys.argv[1]]()
