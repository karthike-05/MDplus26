import inspect
import repo


def test_verify_action_retired():
    assert repo.OUR_ACTION_TYPES == ("confirm_consent", "notify_patient")
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
