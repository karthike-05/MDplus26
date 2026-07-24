from state_machine import route_inbound, routing_stage, ReplyClass
from models import PatientOutreach, Stage


def _o(stage): return PatientOutreach(referral_id="r-1", patient_phone="+1", stage=stage)


def test_routing_stage_mapping():
    assert routing_stage(_o(Stage.CONSENT)) == "consent"
    assert routing_stage(_o(Stage.NOTIFIED)) == "active"
    assert routing_stage(_o(Stage.REMINDED)) == "active"
    assert routing_stage(_o(Stage.VERIFYING)) == "verification"
    assert routing_stage(_o(Stage.DONE)) == "none"


def test_consent_yes_advances():
    out = route_inbound(_o(Stage.CONSENT), ReplyClass.YES)
    assert out["writeback"] == "consent_confirmed"
    assert out["new_stage"] == Stage.AWAITING_BOOKING
    assert out["finish_action"] is True and out["ack_key"] == "ack_consent_confirmed"


def test_stop_stops_and_advances():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.STOP)
    assert out["writeback"] == "consent_declined"
    assert out["new_stage"] == Stage.ESCALATED and out["loop"] == "stop"


def test_verification_yes_utilized():
    out = route_inbound(_o(Stage.VERIFYING), ReplyClass.YES)
    assert out["writeback"] == "utilized" and out["new_stage"] == Stage.DONE


def test_problem_opens_escalation_loop_continues():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.NEEDS_HELP)
    assert out["ack_key"] == "ack_problem"
    assert out["escalation"] == "open" and out["escalation_reason"] == "patient_reported_problem"
    assert out["loop"] == "continue"


def test_problem_while_open_does_not_restack():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.NEEDS_HELP, has_open_issue=True)
    assert out["escalation"] is None and out["ack_key"] == "ack_problem"


def test_affirmative_while_open_resolves():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.YES, has_open_issue=True)
    assert out["escalation"] == "resolve" and out["ack_key"] == "ack_resolved"
    assert out["loop"] == "resume"


def test_reschedule_pauses():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.RESCHEDULE)
    assert out["ack_key"] == "ack_reschedule" and out["loop"] == "pause"
    assert out["escalation"] == "open" and out["escalation_reason"] == "reschedule_requested"


def test_cancel_pauses():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.CANCEL)
    assert out["loop"] == "pause" and out["escalation_reason"] == "cancel_requested"


def test_appointment_question_triggers_lookup():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.APPOINTMENT_QUESTION)
    assert out["needs_booking_lookup"] is True and out["ack_key"] == "answer_appointment"
    assert out["escalation"] is None and out["loop"] == "continue"


def test_channel_preference_writes_and_escalates():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.CHANNEL_PREFERENCE)
    assert out["writeback"] == "channel_preference" and out["escalation"] == "open"
    assert out["ack_key"] == "ack_channel_preference" and out["loop"] == "continue"


def test_accessibility_escalates_loop_continues():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.ACCESSIBILITY_NEED)
    assert out["ack_key"] == "ack_accessibility" and out["escalation"] == "open"
    assert out["escalation_reason"] == "accessibility_need" and out["loop"] == "continue"


def test_consent_no_declines():
    out = route_inbound(_o(Stage.CONSENT), ReplyClass.NO)
    assert out["writeback"] == "consent_declined"
    assert out["new_stage"] == Stage.ESCALATED
    assert out["finish_action"] is True and out["loop"] == "stop"


def test_verification_no_not_utilized_escalates_empathetically():
    # "didn't use it" = the unmet need -> record + escalate for human re-engage,
    # with an empathetic ack (not the flat ack_received).
    out = route_inbound(_o(Stage.VERIFYING), ReplyClass.NO)
    assert out["writeback"] == "not_utilized" and out["new_stage"] == Stage.DONE
    assert out["ack_key"] == "ack_not_utilized"
    assert out["escalation"] == "open" and out["escalation_reason"] == "service_not_utilized"


def test_verification_no_dedupes_when_issue_already_open():
    out = route_inbound(_o(Stage.VERIFYING), ReplyClass.NO, has_open_issue=True)
    assert out["writeback"] == "not_utilized" and out["new_stage"] == Stage.DONE
    assert out["escalation"] is None  # don't stack a second escalation


def test_terminal_replies_advance_stage_off_current():
    # BLOCKING INVARIANT: every terminal reply advances stage off CONSENT/VERIFYING
    # so Loop B never double-messages a responder.
    cases = [
        (Stage.CONSENT, ReplyClass.YES),
        (Stage.CONSENT, ReplyClass.NO),
        (Stage.CONSENT, ReplyClass.STOP),
        (Stage.VERIFYING, ReplyClass.YES),
        (Stage.VERIFYING, ReplyClass.NO),
        (Stage.VERIFYING, ReplyClass.STOP),
        (Stage.NOTIFIED, ReplyClass.STOP),
    ]
    for stage, ic in cases:
        out = route_inbound(_o(stage), ic)  # no open issue -> terminal
        assert out["new_stage"] is not None, (stage, ic)
        assert out["new_stage"] != stage, (stage, ic)


def test_consent_and_verification_unclear_ack():
    assert route_inbound(_o(Stage.CONSENT), ReplyClass.UNCLEAR)["ack_key"] == "ack_unclear"
    assert route_inbound(_o(Stage.VERIFYING), ReplyClass.UNCLEAR)["ack_key"] == "ack_unclear"


def test_consent_yes_while_open_still_confirms():
    # A terminal YES at CONSENT must confirm consent + advance, NOT resolve a flag.
    out = route_inbound(_o(Stage.CONSENT), ReplyClass.YES, has_open_issue=True)
    assert out["writeback"] == "consent_confirmed"
    assert out["new_stage"] == Stage.AWAITING_BOOKING
    assert out["finish_action"] is True
    assert out["escalation"] != "resolve"


def test_verification_yes_while_open_still_utilized():
    out = route_inbound(_o(Stage.VERIFYING), ReplyClass.YES, has_open_issue=True)
    assert out["writeback"] == "utilized"
    assert out["new_stage"] == Stage.DONE
    assert out["escalation"] != "resolve"


def test_active_yes_while_open_still_resolves():
    # At an active stage (NOTIFIED) a YES with an open issue DOES resolve it.
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.YES, has_open_issue=True)
    assert out["escalation"] == "resolve" and out["ack_key"] == "ack_resolved"
    assert out["loop"] == "resume"
