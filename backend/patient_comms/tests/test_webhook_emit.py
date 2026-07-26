import main


def test_webhook_emits_mapped_event_after_commit(monkeypatch):
    calls = []
    monkeypatch.setattr(main.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append((a, k)) or True)

    class R:  # stand-in for InboundResult
        ack = "ok"; writeback = "consent_confirmed"; received_stage = "consent"; escalation_opened = False

    # emit_after_reply is the extracted pure mapper the handler calls post-commit.
    main.emit_after_reply(referral_id="r-1", result=R(), outreach_id="o-1", reply_text="YES")
    assert calls == [(("r-1", "consent_confirmed"),
                      {"outreach_id": "o-1", "reply_text": "YES"})]


def test_webhook_no_emit_when_writeback_not_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(main.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append(a) or True)

    class R:
        ack = "ok"; writeback = None; received_stage = "active"; escalation_opened = False

    main.emit_after_reply(referral_id="r-1", result=R(), outreach_id="o-1", reply_text="hi")
    assert calls == []


def test_emits_needs_review_when_escalation_opened_and_no_writeback(monkeypatch):
    calls = []
    monkeypatch.setattr(main.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append((a, k)) or True)

    class R:
        ack = "ok"; writeback = None; received_stage = "active"; escalation_opened = True

    main.emit_after_reply(referral_id="r-1", result=R(), outreach_id="o-1", reply_text="reschedule me")
    assert calls == [(("r-1", "needs_review"), {"outreach_id": "o-1", "reply_text": "reschedule me"})]


def test_writeback_takes_precedence_over_needs_review(monkeypatch):
    calls = []
    monkeypatch.setattr(main.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append(a[1]) or True)

    class R:  # verification-NO: has a writeback AND an opened escalation
        ack = "ok"; writeback = "not_utilized"; received_stage = "verification"; escalation_opened = True

    main.emit_after_reply(referral_id="r-1", result=R(), outreach_id="o-1", reply_text="NO")
    assert calls == ["verified_not_utilized"]  # not needs_review
