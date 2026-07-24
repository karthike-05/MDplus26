from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

import db
import ranking

app = FastAPI()


@app.post("/rank-referral/{referral_id}")
def rank_referral(referral_id: str):
    """Runs all three ranking layers for a referral (hard filter, objective
    scorer, subjective LLM scorer), upserts the results into ranking_results,
    and returns the SW-facing ranked list (survivors only, ordered by rank).
    """
    return {"results": ranking.rank_referral(referral_id)}


@app.get("/ranking-results/{referral_id}")
def get_ranking_results(referral_id: str):
    """Returns the already-computed ranked list for a referral, without
    re-running the ranking pipeline. Empty if rank_referral hasn't been
    called for this referral_id yet.
    """
    return {"results": db.get_ranking_results(referral_id)}


class SwFeedbackRequest(BaseModel):
    referral_id: str
    service_id: str
    label: str
    label_notes: Optional[str] = None


@app.post("/sw-feedback")
def sw_feedback(request: SwFeedbackRequest):
    """Records the social worker's chosen service for a referral plus a label
    (good_fit, wrong_service, too_far, insurance_mismatch, other), closing the
    loop that will eventually feed the Layer 3 few-shot learning loop.
    """
    return {"result": ranking.record_sw_feedback(
        request.referral_id, request.service_id, request.label, request.label_notes
    )}
