"""Synthetic cohort — no real PHI (CLAUDE.md §2).

Patients + referrals used by the mock DB and the demo. One referral (ref_1001) is
missing an appointment time on purpose, so the review screen has a genuine
"Check this" beat: the reviewer fills it before submit.
"""

from __future__ import annotations

# Field keys match the schema `source` paths: patient.<key> / referral.<key>. The trip
# fields (address, appointment_*) are read via the `service_request` root instead — the
# mock derives that row from these fixtures (backend/db/mock.py).

PATIENTS: dict[str, dict] = {
    "pat_001": {
        "id": "pat_001",
        "name": "Maria Gonzalez",
        "dob": "03/12/1958",                       # non-ISO on purpose; mapper normalizes
        "referring_clinic": "CommUnityCare Hancock",  # live: patients.referring_clinic_name
        "phone": "5127654321",                     # raw digits; mapper formats
        "address": "1420 E Cesar Chavez St, Austin, TX 78702",
        "medicaid_id": "TX-4471-9920",
        "mobility_needs": "Wheelchair accessible vehicle required",
        "household_size": "3",
    },
    "pat_002": {
        "id": "pat_002",
        "name": "James Whitfield",
        "dob": "1971-11-02",
        "referring_clinic": "People's Community Clinic",
        "phone": "(512) 900-1188",
        "address": "907 W 21st St, Austin, TX 78705",
        "medicaid_id": "TX-8830-1145",
        "mobility_needs": "",
        "household_size": "1",
    },
}

# `service_id` -> backend/seed/services.py; `outreach_channel` picks the submission
# method (defaults to the service's preferred_channel at creation). Seed states are
# varied so the dashboard looks alive on first load.
REFERRALS: dict[str, dict] = {
    "ref_1001": {
        "id": "ref_1001",
        "patient_id": "pat_001",
        "service_id": "svc_capmetro",
        "form_id": "transport_intake",
        "outreach_channel": "form",
        "current_state": "consent_granted",       # ready to place -> review
        "service_name": "CapMetro Access NEMT",
        "referring_clinic": "CommUnityCare Hancock",
        "appointment_date": "08/05/2026",
        "appointment_time": "",                    # missing -> needs_attention on review
    },
    "ref_1002": {
        "id": "ref_1002",
        "patient_id": "pat_002",
        "service_id": "svc_capmetro",
        "form_id": "transport_intake",
        "outreach_channel": "form",
        "current_state": "completed",              # loop-closed example
        "service_name": "CapMetro Access NEMT",
        "referring_clinic": "People's Community Clinic",
        "appointment_date": "08/11/2026",
        "appointment_time": "9:30 AM",
    },
    "ref_1003": {
        "id": "ref_1003",
        "patient_id": "pat_002",
        "service_id": "svc_food_bank",
        "form_id": "food_assistance",
        "outreach_channel": "form",
        "current_state": "submitted",              # awaiting the service's response
        "service_name": "Central Texas Food Bank",
        "referring_clinic": "People's Community Clinic",
    },
}
