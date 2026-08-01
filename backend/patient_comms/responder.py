"""Template-anchored conversational responder (spec 2026-08-01).

Turns an already-rendered ack template into a warm, natural reply that also
answers the patient's logistics question -- using ONLY the facts passed in. The
rendered template is both the content contract and the fallback: any error or
validation failure returns it unchanged, so RESPONDER=on can never make a reply
worse than today's templated one.

PHI: this module never touches the DB. It only sees the allowlisted logistics
facts handed to it (_ALLOWED_KEYS). Clinical data is structurally excluded. Run
against a BAA model endpoint (Anthropic offers a BAA) since a reply can be
phrased around patient-volunteered text.
"""
import logging
import os
import re

logger = logging.getLogger("responder")
_audit = logging.getLogger("responder_audit")

DEFAULT_MODEL = os.environ.get("RESPONDER_MODEL", "claude-haiku-4-5")
MAX_REPLY_CHARS = 320

# Logistics-only allowlist -- the PHI gate. Nothing else reaches the prompt.
_ALLOWED_KEYS = ("patient_name", "clinic_name", "resource_name", "service_type", "details")

_SYSTEM_PROMPT = (
    "You rephrase an approved outbound message from a healthcare social-services "
    "outreach program to sound warm and human, and answer the patient's question "
    "using ONLY the facts provided. Rules: use only the given facts; never invent "
    "times, addresses, names, or eligibility; never give medical advice; if asked "
    "something the facts don't cover, don't guess -- say a coordinator will follow "
    "up; at most 2 short sentences, SMS-style, no markdown, no emoji, no links."
)

_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_]+\}")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_MARKDOWN_RE = re.compile(r"[*_#`]")


def is_enabled() -> bool:
    return os.environ.get("RESPONDER", "on").lower() == "on"


def _build_allowed_context(facts: dict) -> dict:
    """Copy ONLY allowlisted logistics keys. Clinical fields cannot reach the
    prompt even if a caller passes them in `facts`."""
    return {k: facts[k] for k in _ALLOWED_KEYS if facts.get(k)}


def _validate(reply: str) -> str | None:
    """Return a clean reply, or None if it must fall back to the template."""
    reply = (reply or "").strip()
    if not reply or len(reply) > MAX_REPLY_CHARS:
        return None
    if _PLACEHOLDER_RE.search(reply) or _URL_RE.search(reply) or _MARKDOWN_RE.search(reply):
        return None
    return reply


def _render_user_prompt(template_body: str, allowed: dict, patient_question: str,
                        history: list[dict]) -> str:
    lines = ["Approved message to rephrase:", template_body, "",
             "Facts you may use (and NOTHING else):"]
    for k, v in allowed.items():
        lines.append(f"- {k}: {v}")
    if history:
        lines.append("")
        lines.append("Recent conversation (oldest first):")
        for m in history:
            who = "patient" if m.get("direction") == "inbound" else "us"
            lines.append(f"- {who}: {m.get('body', '')}")
    lines += ["", f"The patient just said: {patient_question}", "", "Write the reply."]
    return "\n".join(lines)


def _get_client():
    import anthropic  # deferred so RESPONDER=off needs no anthropic dep

    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


def compose_reply(template_body: str, *, facts: dict, patient_question: str,
                  history: list[dict]) -> str:
    if not is_enabled():
        return template_body

    allowed = _build_allowed_context(facts)
    try:
        resp = _get_client().messages.create(
            model=DEFAULT_MODEL,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": _render_user_prompt(template_body, allowed,
                                                       patient_question, history)}],
        )
        raw = next(b.text for b in resp.content if b.type == "text")
    except Exception as e:  # noqa: BLE001 -- never raise into the webhook
        logger.warning("responder failed (%s); using template", e)
        _audit.info("model=%s keys=%s q=%r decision=fallback:error",
                    DEFAULT_MODEL, sorted(allowed), patient_question)
        return template_body

    clean = _validate(raw)
    if clean is None:
        _audit.info("model=%s keys=%s q=%r completion=%r decision=fallback:validation",
                    DEFAULT_MODEL, sorted(allowed), patient_question, raw)
        return template_body

    _audit.info("model=%s keys=%s q=%r completion=%r decision=accepted",
                DEFAULT_MODEL, sorted(allowed), patient_question, clean)
    return clean
