_MOCK_APPOINTMENTS = {
    "default": {
        "appointment_time": "2026-07-28T10:30:00-05:00",
        "provider_name": "Dr. Elena Martinez",
        "appointment_type": "Dialysis appointment",
    }
}


def save_call_outcome(payload: dict) -> dict:
    # TODO: replace with a real Supabase/Postgres upsert into the `cases` table,
    # keyed by case_id. Signature/return shape should stay the same.
    print(f"[mock db] save_call_outcome: {payload}")
    return {**payload, "saved": True}


def get_patient_appointment(case_id: str) -> dict:
    # TODO: replace with a real Postgres query against the appointments table.
    return _MOCK_APPOINTMENTS.get(case_id, _MOCK_APPOINTMENTS["default"])
