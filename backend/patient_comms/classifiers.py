"""
Inbound reply classification -- two implementations behind one interface.

  - LLMClassifier (default): Claude reads the reply and returns a structured
    label, catching the cases keywords miss -- "I called but no one answered",
    "went yesterday, thanks", "which appointment??". Keeps a keyword fast-path
    for the obvious YES/NO/STOP so those never hit the API.
  - KeywordClassifier: exact-match keyword logic only. Zero deps, offline.
    Opt in with CLASSIFIER=keyword (e.g. an env with no API key).

WHY the LLM is on the INBOUND side only (see CLAUDE.md Section 7):
  - It classifies the patient's reply into ONE enum label. That label is all
    that flows into the state machine -- the model never generates text shown
    to a patient. Outbound messages stay 100% templated (templates.py).
  - The prompt sees only the reply TEXT -- never name, phone, or chart data.
    The reply can still contain patient-volunteered PHI, so run this through a
    HIPAA-eligible (BAA) model endpoint. Anthropic offers a BAA.

The LLM path keeps a keyword fast-path: obvious YES/NO/STOP never hit the API
(cheaper, and it means the demo still works if the API key is absent -- it
degrades to the keyword result rather than erroring).
"""
import json
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger("classifier")

# Default model. NOTE: for a high-volume reply classifier, claude-haiku-4-5 is
# the cost/speed-appropriate pick ($1/$5 per 1M vs $5/$25) and is plenty capable
# for a 5-way label. Left at opus by default; flip with CLASSIFIER_MODEL.
DEFAULT_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-opus-4-8")

# Fixed, PHI-free system prompt. Stage-neutral categories -- the state machine
# gives them meaning per stage (consent / day3 / day7).
_SYSTEM_PROMPT = (
    "You classify a single inbound SMS reply from a patient who received an "
    "automated message from a healthcare social-services outreach program. "
    "Classify the reply into exactly one category:\n"
    "- affirmative: confirms/agrees or says something was done "
    '(e.g. "yes", "already went", "all set").\n'
    "- negative: declines or says something was NOT done "
    '(e.g. "no", "not yet").\n'
    "- reschedule: wants a different date/time "
    '(e.g. "can we move it", "Tuesday doesn\'t work").\n'
    "- cancel: wants to cancel the service entirely "
    '(e.g. "I don\'t need it anymore", "cancel my ride").\n'
    "- appointment_question: asks for details about the appointment "
    '(e.g. "what time?", "where do I go?", "who\'s picking me up?").\n'
    "- accessibility_need: mentions a disability/accommodation need "
    '(e.g. "I use a wheelchair", "I\'m hard of hearing").\n'
    "- channel_preference: asks to be contacted a different way "
    '(e.g. "call me instead", "email me").\n'
    "- needs_help: a problem/confusion not covered above "
    '(e.g. "I called but no one answered", "I don\'t have a photo ID").\n'
    "- opt_out: wants to stop messages "
    '(e.g. "stop", "unsubscribe", "leave me alone").\n'
    "- unclear: none of the above, or ambiguous.\n"
    "Respond only with the structured category."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["affirmative", "negative", "reschedule", "cancel",
                     "appointment_question", "accessibility_need",
                     "channel_preference", "needs_help", "opt_out", "unclear"],
        }
    },
    "required": ["category"],
    "additionalProperties": False,
}


def _label_to_class(label: str):
    # Imported here to avoid a circular import at module load (state_machine
    # imports get_classifier lazily).
    from state_machine import ReplyClass

    return {
        "affirmative": ReplyClass.YES,
        "negative": ReplyClass.NO,
        "needs_help": ReplyClass.NEEDS_HELP,
        "opt_out": ReplyClass.STOP,
        "unclear": ReplyClass.UNCLEAR,
        "reschedule": ReplyClass.RESCHEDULE,
        "cancel": ReplyClass.CANCEL,
        "appointment_question": ReplyClass.APPOINTMENT_QUESTION,
        "accessibility_need": ReplyClass.ACCESSIBILITY_NEED,
        "channel_preference": ReplyClass.CHANNEL_PREFERENCE,
    }.get(label, ReplyClass.UNCLEAR)


class ReplyClassifier(ABC):
    @abstractmethod
    def classify(self, text: str):
        """Return a ReplyClass for the given inbound reply text."""
        raise NotImplementedError


class KeywordClassifier(ReplyClassifier):
    def classify(self, text: str):
        from state_machine import classify_keywords

        return classify_keywords(text)


class LLMClassifier(ReplyClassifier):
    """Keyword fast-path for the obvious cases; Claude for everything else."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic  # deferred so keyword mode needs no anthropic dep

            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        return self._client

    def classify(self, text: str):
        from state_machine import ReplyClass, classify_keywords

        # Fast-path: unambiguous keywords skip the API entirely.
        keyword_result = classify_keywords(text)
        if keyword_result != ReplyClass.UNCLEAR:
            return keyword_result

        # Ambiguous -> ask Claude. Fall back to UNCLEAR (-> needs_review) on any
        # error so a flaky API never drops a reply on the floor.
        try:
            resp = self._get_client().messages.create(
                model=self.model,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
                # No thinking/effort: a 5-way label needs neither, and effort
                # 400s on Haiku 4.5. Structured output enforces the enum.
                output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            )
            body = next(b.text for b in resp.content if b.type == "text")
            return _label_to_class(json.loads(body)["category"])
        except Exception as e:  # noqa: BLE001 -- degrade gracefully, never raise into the webhook
            logger.warning("LLM classify failed (%s); falling back to UNCLEAR", e)
            return ReplyClass.UNCLEAR


_classifier_singleton: ReplyClassifier | None = None


def get_classifier() -> ReplyClassifier:
    """CLASSIFIER env var selects the inbound classifier. Defaults to 'llm';
    set CLASSIFIER=keyword to force the offline keyword-only path (e.g. a
    dev/CI environment with no ANTHROPIC_API_KEY). The LLM path already keeps
    a keyword fast-path and degrades to it if the API is unavailable, so 'llm'
    is a safe default even without a key."""
    global _classifier_singleton
    if _classifier_singleton is None:
        if os.environ.get("CLASSIFIER", "llm").lower() == "keyword":
            _classifier_singleton = KeywordClassifier()
            logger.info("using keyword classifier")
        else:
            _classifier_singleton = LLMClassifier()
            logger.info("using LLM classifier (model=%s)", DEFAULT_MODEL)
    return _classifier_singleton
