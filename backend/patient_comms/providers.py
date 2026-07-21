"""
SMS provider abstraction.

Build and test against MockSmsProvider today (no Twilio creds needed).
Set SMS_PROVIDER=twilio + the three TWILIO_* env vars to switch to real
sends without touching any calling code.
"""
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger("sms_provider")
logging.basicConfig(level=logging.INFO)


class SmsProvider(ABC):
    @abstractmethod
    def send_message(self, to_phone: str, body: str) -> str:
        """Send an SMS. Returns a provider-side message id."""
        raise NotImplementedError

    def send_template(self, to_phone: str, content_sid: str | None,
                      variables: dict, fallback_body: str) -> str:
        """Send a business-initiated templated message. Only WhatsApp needs a
        real (Meta-approved) template for first contact; SMS and mock have no
        such rule, so the default just sends the plain text body."""
        return self.send_message(to_phone, fallback_body)


class MockSmsProvider(SmsProvider):
    """Logs instead of sending. Use this for all local dev / demo dry-runs."""

    def __init__(self):
        self.sent_log: list[dict] = []

    def send_message(self, to_phone: str, body: str) -> str:
        message_id = f"mock-{len(self.sent_log) + 1}"
        self.sent_log.append({"id": message_id, "to": to_phone, "body": body})
        logger.info("[MOCK SMS] to=%s body=%r", to_phone, body)
        return message_id


class TwilioSmsProvider(SmsProvider):
    """Real Twilio send. Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
    TWILIO_FROM_NUMBER in the environment. Trial accounts can only send to
    phone numbers you've verified in the Twilio console."""

    def __init__(self):
        from twilio.rest import Client  # deferred import so mock mode has no hard dep

        account_sid = os.environ["TWILIO_ACCOUNT_SID"]
        auth_token = os.environ["TWILIO_AUTH_TOKEN"]
        self.from_number = os.environ["TWILIO_FROM_NUMBER"]
        self.client = Client(account_sid, auth_token)

    def send_message(self, to_phone: str, body: str) -> str:
        message = self.client.messages.create(to=to_phone, from_=self.from_number, body=body)
        logger.info("[TWILIO SMS] sid=%s to=%s", message.sid, to_phone)
        return message.sid


class TwilioWhatsAppProvider(SmsProvider):
    """Send/receive over WhatsApp via Twilio, using the SAME Twilio creds.

    Works immediately against the Twilio WhatsApp Sandbox -- no Meta business
    account, no A2P 10DLC, no review. WHATSAPP_FROM defaults to the shared
    sandbox number (whatsapp:+14155238886). Numbers are addressed with a
    'whatsapp:' prefix, which this provider adds automatically, so the rest of
    the app keeps storing plain E.164 (+1...) numbers.

    Sandbox limits: only phones that have joined your sandbox receive messages,
    and freeform sends must be within 24h of the recipient's last inbound
    (fine for interactive testing / the compressed demo)."""

    def __init__(self):
        from twilio.rest import Client

        account_sid = os.environ["TWILIO_ACCOUNT_SID"]
        auth_token = os.environ["TWILIO_AUTH_TOKEN"]
        raw_from = os.environ.get("WHATSAPP_FROM", "whatsapp:+14155238886")
        self.from_number = raw_from if raw_from.startswith("whatsapp:") else f"whatsapp:{raw_from}"
        self.client = Client(account_sid, auth_token)

    @staticmethod
    def _wa(number: str) -> str:
        return number if number.startswith("whatsapp:") else f"whatsapp:{number}"

    def send_message(self, to_phone: str, body: str) -> str:
        message = self.client.messages.create(
            to=self._wa(to_phone), from_=self.from_number, body=body
        )
        logger.info("[TWILIO WhatsApp] sid=%s to=%s", message.sid, to_phone)
        return message.sid

    def send_template(self, to_phone: str, content_sid: str | None,
                      variables: dict, fallback_body: str) -> str:
        """First contact to a number that hasn't messaged us needs an approved
        WhatsApp template (sent via content_sid + content_variables). If no
        template is configured, fall back to freeform -- which only delivers if
        we're already inside the recipient's 24h window."""
        if not content_sid:
            return self.send_message(to_phone, fallback_body)
        import json

        message = self.client.messages.create(
            to=self._wa(to_phone),
            from_=self.from_number,
            content_sid=content_sid,
            content_variables=json.dumps({str(k): str(v) for k, v in variables.items()}),
        )
        logger.info("[TWILIO WhatsApp template] sid=%s to=%s", message.sid, to_phone)
        return message.sid


_provider_singleton: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    """SMS_PROVIDER selects the channel. Defaults to mock so nothing
    accidentally messages a real patient during dev.
      mock     -> log only
      twilio   -> real SMS (needs A2P 10DLC approval to send in the US)
      whatsapp -> WhatsApp via Twilio (works now via the sandbox)"""
    global _provider_singleton
    if _provider_singleton is None:
        provider = os.environ.get("SMS_PROVIDER", "mock").lower()
        if provider == "twilio":
            _provider_singleton = TwilioSmsProvider()
        elif provider == "whatsapp":
            _provider_singleton = TwilioWhatsAppProvider()
        else:
            _provider_singleton = MockSmsProvider()
    return _provider_singleton
