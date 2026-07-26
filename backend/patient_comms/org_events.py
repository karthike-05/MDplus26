"""Emit patient-comms events to the org-facing scheduler (spec §5b).

Fire-and-forget: a failed POST never raises — the patient ack and the DB commit
must not depend on the org backend being reachable (spec §9). Uses stdlib urllib
so the messaging service adds no new dependency."""
import json
import logging
import os
import urllib.request

logger = logging.getLogger("org_events")

# our route_inbound `writeback` -> their PATIENT_COMMS_EVENT_MAP key (spec §5b).
# This map covers only the terminal consent/utilization writebacks. `no_response`
# is emitted from scheduler.py; `needs_review` is emitted from the webhook
# (main.emit_after_reply) when a reply opens a NEW human-review escalation --
# neither goes through this map.
WRITEBACK_TO_EVENT = {
    "consent_confirmed": "consent_confirmed",
    "consent_declined": "consent_declined",
    "utilized": "verified_utilized",
    "not_utilized": "verified_not_utilized",
}


def _post_json(url: str, payload: dict, timeout: float) -> bool:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return 200 <= resp.status < 300


def emit_patient_comms_event(referral_id, event, *, outreach_id=None,
                             reply_text=None, attempt_no=1) -> bool:
    base = os.environ.get("ORG_BACKEND_URL")
    if not base:
        return False
    payload = {"referral_id": referral_id, "event": event, "attempt_no": attempt_no}
    if outreach_id is not None:
        payload["outreach_id"] = outreach_id
    if reply_text is not None:
        payload["reply_text"] = reply_text
    try:
        return _post_json(f"{base.rstrip('/')}/api/patient-comms/event", payload, timeout=3.0)
    except Exception:  # noqa: BLE001 — fire-and-forget (spec §9)
        logger.warning("patient-comms event emit failed (referral=%s event=%s)",
                       referral_id, event, exc_info=True)
        return False
