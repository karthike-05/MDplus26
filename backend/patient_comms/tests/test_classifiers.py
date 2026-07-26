from state_machine import ReplyClass, classify_keywords
import classifiers


def test_new_reply_classes_exist():
    for name in ("RESCHEDULE", "CANCEL", "APPOINTMENT_QUESTION",
                 "ACCESSIBILITY_NEED", "CHANNEL_PREFERENCE"):
        assert hasattr(ReplyClass, name)


def test_label_mapping_covers_new_intents():
    m = {
        "reschedule": ReplyClass.RESCHEDULE,
        "cancel": ReplyClass.CANCEL,
        "appointment_question": ReplyClass.APPOINTMENT_QUESTION,
        "accessibility_need": ReplyClass.ACCESSIBILITY_NEED,
        "channel_preference": ReplyClass.CHANNEL_PREFERENCE,
        "affirmative": ReplyClass.YES,
        "opt_out": ReplyClass.STOP,
        "unknown-label": ReplyClass.UNCLEAR,   # unknown degrades safely
    }
    for label, expected in m.items():
        assert classifiers._label_to_class(label) == expected


def test_schema_enum_lists_new_categories():
    cats = classifiers._SCHEMA["properties"]["category"]["enum"]
    for c in ("reschedule", "cancel", "appointment_question",
              "accessibility_need", "channel_preference"):
        assert c in cats


def test_keyword_fastpath_unchanged():
    assert classify_keywords("YES") == ReplyClass.YES
    assert classify_keywords("stop") == ReplyClass.STOP
    assert classify_keywords("i need to reschedule") == ReplyClass.UNCLEAR  # keyword can't tell -> LLM's job
