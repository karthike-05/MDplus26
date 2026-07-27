import inspect
import repo


def test_utilization_action_consumed_as_notify_alias():
    # 07-21 retired `confirm_service_utilization` as a distinct "verify-send"
    # (verification is our own scheduled step). But the live advance_referral()
    # still emits it at `enrolled` (002_utilization_milestone.sql, never removed),
    # so we re-consume it — routed to the notify handler (post-enrollment touch +
    # schedule our own verify), NOT as a VERIFY_ACTION that sends a check-in itself.
    assert repo.OUR_ACTION_TYPES == (
        "confirm_consent", "notify_patient", "confirm_service_utilization")
    assert repo.UTILIZATION_ACTION == "confirm_service_utilization"
    assert not hasattr(repo, "VERIFY_ACTION")


def test_write_functions_accept_conn():
    for name in ("set_consent", "mark_booking_notified", "set_utilization",
                 "log_attempt", "create_escalation", "finish_action", "start_action"):
        sig = inspect.signature(getattr(repo, name))
        assert "conn" in sig.parameters, f"{name} must accept conn="


def test_new_escalation_and_pref_fns_accept_conn():
    import inspect, repo
    for name in ("resolve_escalation", "set_preferred_contact_method"):
        assert "conn" in inspect.signature(getattr(repo, name)).parameters, f"{name} must accept conn="
    assert hasattr(repo, "find_open_escalation")
