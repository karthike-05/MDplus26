# Patient SMS + Closed-Loop Verification

Aneesh's build stream: patient-facing consent + day3/day7 utilization
check-in over SMS, with a social-worker dashboard. Runs independent of the
org-facing (form/email/call) track Pranav/Karthik/Gyan own -- the two connect
only through `referral_id`.

## Demo quickstart (mock mode — no accounts needed)

```bash
pip install -r requirements.txt          # or: pip install ... --break-system-packages
cp .env.example .env                      # defaults are demo-ready as-is
DEMO_TIMESCALE=seconds uvicorn main:app --reload
```

Open **http://localhost:8000/** — the dashboard.

1. Fill the **New Referral** form → *Send consent request*. A consent SMS is
   "sent" (logged, since mock mode) and the case appears in the table.
2. Use the **Simulate reply** box to deliver an inbound `YES` (in live mode the
   patient texts your real number instead) → consent flips to `confirmed`.
3. The **scheduler** auto-fires day3 → day7 → nudge on the compressed timescale
   (one logical "day" = `DEMO_DAY_SECONDS`, default 5s), so the whole loop
   completes live in ~35s. Watch the thread + case status update on their own.
4. Reply `YES` to the day7 check-in → `verification_status: verified_utilized`.
   The loop is closed.

Escalations (`no_response`, `needs_review`, opt-outs) sort to the top of the
case table with a ⚠ marker.

## Going live (real SMS to a real phone)

Fill these in `.env`, then restart:

```bash
SMS_PROVIDER=twilio
TWILIO_ACCOUNT_SID=...        # Twilio Console home
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1...      # an SMS-capable number you provisioned
DEMO_TIMESCALE=real          # or keep `seconds` to demo fast on real SMS
```

No code changes — `providers.py` reads the env var. **Trial accounts can only
text numbers verified in the console**, so verify your demo phone first.

To receive replies, expose the app publicly and point your Twilio number's
**Messaging webhook** at `POST <public-url>/webhook/sms-inbound`:

```bash
ngrok http 8000              # fastest for a solo test
# webhook -> https://<id>.ngrok.io/webhook/sms-inbound
```

(Railway works too for a persistent URL; ngrok is quicker to stand up.)

## Switching to the shared Supabase DB

```bash
export DATABASE_URL=postgresql://<supabase-connection-string>
```

`models.py` defines `patient_outreach` + `messages` tables. Hand the schema to
Gyan to confirm `referral_id` matches his referrals table key, then run against
Postgres.

## Files

- `main.py` — FastAPI app, endpoints, dashboard route, read APIs for the UI
- `service.py` — shared send/log logic used by both endpoints and the scheduler
- `scheduler.py` — in-process APScheduler; auto-fires day3/day7/nudge/no-response
- `state_machine.py` — routes an inbound reply to the open stage, applies status
- `classifiers.py` — inbound reply classification: keyword (default) or LLM
- `templates.py` — the 4 fixed SMS templates (fail-loud on missing slots)
- `providers.py` — mock / Twilio SMS provider behind one interface
- `models.py` — `PatientOutreach` + `Message` (thread) tables
- `static/index.html` — the dashboard (single vanilla-JS page, no build step)

## Inbound reply classification (keyword vs LLM)

How a patient's reply becomes a status update. Two implementations behind one
interface (`classifiers.py`), switched with `CLASSIFIER`:

- **`keyword`** (default) — exact-match YES/NO/STOP. Offline, no API key, safe
  for mock demos. Anything it can't match is `unclear` → routes to a human.
- **`llm`** — Claude reads the reply and returns one structured label
  (`affirmative` / `negative` / `needs_help` / `opt_out` / `unclear`). Catches
  what keywords miss: *"I called but no one answered"* → `needs_help`,
  *"went yesterday, thanks"* → `affirmative`, *"which appointment??"* →
  `needs_help`. A `needs_help` reply surfaces the case as ⚠ in the dashboard —
  this is the completion-gap signal the project exists to catch.

```bash
CLASSIFIER=llm
ANTHROPIC_API_KEY=sk-ant-...
# CLASSIFIER_MODEL=claude-haiku-4-5   # cheaper/faster; defaults to claude-opus-4-8
```

Design guarantees (see CLAUDE.md §7):
- **The LLM only classifies inbound replies — it never writes patient-facing
  text.** Outbound is 100% templated (`templates.py`), unchanged.
- The prompt sees only the **reply text** — never name, phone, or chart data.
- Obvious YES/NO/STOP take a keyword fast-path (no API call); if the API errors,
  classification degrades to `unclear` → `needs_review` rather than dropping the
  reply. Both are verified.
- The reply text itself can carry patient-volunteered PHI → use a HIPAA/BAA
  Anthropic endpoint before real patient traffic.

### Conversational replies (responder)

Inbound replies are answered conversationally: the LLM rephrases the approved
ack template and answers logistics questions ("what time?", "where?") from a
live booking read. It is **template-anchored** — the rendered template is the
content contract and the fallback, so any model/validation failure sends the
plain template. It never changes state (consent/utilization/escalation stay
deterministic) and never sees clinical data (a code allowlist limits the prompt
to name/clinic/resource/service/booking-details). Toggle with `RESPONDER=off`.

## Still open / needs a team decision

- **No-response escalation queue**: silent-through-day7 patients get
  `verification_status = no_response` and surface as ⚠ in the case table.
  Whether they feed the *same* social-worker queue as Pranav/Karthik's
  org-contact failures is still a team call (defaulting to yes).
- **Twilio webhook signature validation**: `/webhook/sms-inbound` is currently
  unauthenticated — anyone with the public URL could POST a fake reply. Add
  `X-Twilio-Signature` validation before anything real.
- **A2P 10DLC registration**: not needed for trial/low-volume demo sends;
  required before real SMS volume. Flag for the Dr. Leung / Twilio-budget sync.
- **Dashboard is vanilla HTML**, not React (chosen for a zero-build,
  demo-reliable page). Swap for the React version post-demo if desired.
