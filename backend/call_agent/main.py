from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

import db

app = FastAPI()

Status = Literal[
    "confirmed",
    "ineligible",
    "unavailable",
    "callback_required",
    "escalation_needed",
    "alt_slot_offered",
]

# Fields each status requires beyond case_id/status, per transport.md's
# escalation and alt-slot subflows.
REQUIRED_FIELDS_BY_STATUS: dict[str, tuple[str, ...]] = {
    "escalation_needed": ("escalation_reason", "social_worker_note", "patient_message"),
    "alt_slot_offered": ("offered_datetime", "patient_message"),
}


class LogOutcomeRequest(BaseModel):
    case_id: str
    status: Status
    confirmation_id: Optional[str] = None
    pickup_window: Optional[str] = None
    escalation_reason: Optional[Literal["patient_required", "verification_required"]] = None
    offered_datetime: Optional[str] = None
    notes: Optional[str] = None
    social_worker_note: Optional[str] = None
    patient_message: Optional[str] = None


@app.post("/log-call-outcome")
async def log_outcome(request: Request):
    raw_body = await request.json()
    print(f"[log_outcome] raw request body: {raw_body}")

    # Retell wraps custom-function args as {"call": {...}, "name": ..., "args": {...}}
    # rather than sending them flat — accept either shape.
    args = raw_body.get("args", raw_body)
    try:
        body = LogOutcomeRequest(**args)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    required = REQUIRED_FIELDS_BY_STATUS.get(body.status, ())
    missing = [field for field in required if getattr(body, field) is None]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"status '{body.status}' requires: {', '.join(missing)}",
        )

    result = db.save_call_outcome(body.model_dump(exclude_none=True))
    return {
        "success": True,
        "case_id": body.case_id,
        "status": body.status,
        "result": result,
    }


@app.get("/lookup-patient-appointment")
def lookup_patient_appointment(case_id: str = Query(...)):
    appointment = db.get_patient_appointment(case_id)
    return {"data": appointment}
