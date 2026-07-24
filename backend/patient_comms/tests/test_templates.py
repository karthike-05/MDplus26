import pytest
from templates import render_template


def test_consent_uses_clinic():
    msg = render_template("consent", patient_name="Sam", clinic_name="KU Liberty",
                          service_type="transportation")
    assert "KU Liberty" in msg


def test_booking_uses_resource():
    msg = render_template("booking_details", patient_name="Sam",
                          resource_name="ModivCare", service_type="transportation",
                          details="Scheduled for Tue.")
    assert "ModivCare" in msg and "Scheduled for Tue." in msg


def test_missing_slot_raises():
    with pytest.raises(ValueError):
        render_template("consent", patient_name="Sam")  # no clinic_name


def test_new_templates_render():
    from templates import render_template
    assert "Tue 2pm" in render_template("answer_appointment", patient_name="Sam", details="Tue 2pm")
    for key in ("ack_problem", "ack_resolved", "ack_reschedule", "ack_cancel",
                "ack_channel_preference", "ack_accessibility", "ack_not_utilized"):
        msg = render_template(key, patient_name="Sam", service_type="transportation",
                              resource_name="ModivCare")
        assert "Sam" in msg


def test_answer_appointment_requires_details():
    import pytest
    from templates import render_template
    with pytest.raises(ValueError):
        render_template("answer_appointment", patient_name="Sam")  # no details
