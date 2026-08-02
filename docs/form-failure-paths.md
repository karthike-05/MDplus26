# Form component — failure paths to harden

**Written 2026-08-01, for work *after* the Aug-2 recording.** The happy path is proven end
to end on live data (`changes-2026-08-01.md` §1). This is the list of ways it can go wrong
that nobody has exercised.

> **F1 and F2 were fixed the same day** — both were one-line-ish, both caused a silently
> stalled or abandoned referral, and F1 was a regression from that morning's work. F3–F8
> remain open. Each entry below keeps its original diagnosis so the reasoning survives,
> with the fix appended.

Ordered by **what it costs when it happens**, not by how likely it is. The expensive ones
are the silent ones: a referral that stalls or abandons a service reads exactly like a
referral that's fine, which is the failure mode this whole product exists to eliminate.

Nothing here is a bug that's currently biting. It's the ugly half of a component whose
pretty half now works.

---

## ✅ F1. A validation failure on submit abandons the service — FIXED 2026-08-01

**`backend/main.py:611`** — `_close_open_form_action(referral_id, outcome.status)` closes
the action as `"completed" if status == "success" else "failed"`.

`submit()` returns **`needs_human`** when re-validation catches a bad value
(`fill_form.py:128-141`) — which is correct and deliberate: malformed values are never
injected. But the route then marks the action **`failed`**, and:

1. A failed action **permanently poisons** `attempt:<referral>:<service>:online_form`
   (§7c), so `advance_referral` can never re-queue that channel.
2. The `attempts` row is written with `outcome='needs_human_followup'` and
   `status='completed'`, so the channel counts as *used* and not *pending* → step 9 of
   `advance_referral` sees no unused channel → **`try_next_resource`**.

So a reviewer typing one bad date makes the referral give up on the service it was about
to apply to, and the retry path is dead. The reviewer can still `POST /api/submit` again
directly (the route doesn't need the action), but the bus has already moved on.

> **I introduced this today** as part of the fix for "submit never closed its action."
> Closing on success was right; closing on `needs_human` was not.

**Fixed.** `post_submit` now short-circuits on `needs_human` and does *none* of the three
things: no shared `attempts` row (a validation bounce never reached the service, and
recording one spends a real outreach attempt), no action close (the reviewer isn't
finished — `blocked` is exactly that state), no `advance_referral` (nothing changed).

Verified live: submitting `appointment_time: "quarter to three"` returns `needs_human`
with the problems listed, leaves the action `blocked`, and writes **zero** attempts. The
corrected resubmit then succeeds as `attempt_number: 1` — the bounce cost nothing.

---

## ✅ F2. The `service_requests` write-back can throw *after* the PDF is written — FIXED 2026-08-01

**`backend/tools/fill_form/fill_form.py:152-156`** — unguarded:

```python
if status == "success":
    writeback = service_request_writeback(schema, clean)
    if writeback:
        await db.save_service_request(referral_id, writeback)   # can raise
```

The injection already happened. If this raises — a network blip, a type mismatch on a
`time`/`date` column, a value too long for its column — the exception propagates out of
`submit()`, so:

- no `ToolOutcome` is recorded (the code that does it is *below* this),
- the action is never closed → open-action guard → **referral frozen**,
- the route 500s, so the SW is told their submit failed,
- **but the PDF was written.** The submission is real and nothing knows.

This is precisely the shape of the `save_call_outcome` bug (07-31 §2), where one bad field
killed everything after it in the chain. We fixed it there and left the same pattern here.

A concrete trigger already exists: `appointment_time` maps to
`service_request.requested_start_time`, a `time` column. A reviewer typing `2:45 PM`
instead of `14:45` produces a type error — and `format` isn't set on that field, so
validation won't catch it first.

**Fixed, in both directions.**

*Containment* — the write-back is wrapped; a failure is reported as
`outcome.data["writeback_failed"]` (with the values it tried) and the submit still
succeeds, because the injection really happened.

*Prevention* — the trigger named above is gone. `appointment_time` now carries
`"format": "time"`, and a new `time` validator accepts what the DB emits (`09:30:00`),
what a reviewer types (`2:45 PM`), and 24h without seconds — while
`service_request_writeback` normalises through each field's own `format` on the way out.
So the PDF carries `2:45 PM` (what a human should read on a form) and the `time` column
gets `14:45:00`. Verified live end to end.

---

## 🟠 F3. A missing PDF file 500s the review screen

**`backend/main.py:526-530`** (`get_page_size`) and **`:540`** (`render_page_png`) resolve
`ROOT / schema.source_ref` and hand it to PyMuPDF with no existence check. Same in
`PdfInjector.inject` (`pdf_injector.py:26`).

`source_ref` is a repo-relative path baked into the schema JSON
(`sample_forms/transport_intake_blank.pdf`). It exists today because it's committed — but
a schema seeded into `form_templates` pointing at a path that isn't in the deployed image
gives a raw `fitz` traceback as a 500, and the reviewer sees a blank screen with no
explanation.

**Fix:** check `.exists()` and 404 with the resolved path and the `form_id`, the way the
missing-`form_id` branch already does (`main.py:515-520`). Cheap, and it turns a mystery
into a sentence.

---

## 🟠 F4. Double submit burns an outreach attempt

`POST /api/submit` is not idempotent live. `record_attempt` is a documented no-op on the
live adapters, and `record_shared_attempt` takes its number from
`next_attempt_number(referral_id, service_id)` — so a second submit writes
`attempt_number=2` rather than upserting.

Two clicks (or one double-click, or a reviewer who reloads and resubmits) therefore spend
two of the **three** attempts `advance_referral` allows per service. Three clicks exhausts
the service entirely and moves the referral on.

Offline this is safe — `attempt_id` is deterministic per `(referral, from_state, attempt)`
and `record_attempt` upserts. The guarantee simply doesn't survive the live adapter.

**Fix options:** disable the submit button while in flight (cosmetic, doesn't fix the API);
or key the shared attempt on the action id, which is already unique per dispatch and is
what `actions.py:176` uses (`f"{referral_id}:{action_id}"`). The second is the real one.

---

## 🟡 F5. A schema field with no `rect` fills nothing, silently

**`pdf_injector.py:31-32`** — `if not field.rect: continue`.

An authoring mistake (or a future extracted schema with a field it couldn't locate)
produces a PDF with that box simply empty. No error, no `needs_attention`, nothing in
`ToolOutcome.data`. The field *is* listed in `filled_fields` only if it was actually
written, so the information exists — but nobody compares the two lists.

**Fix:** collect skipped fields into the confirmation dict as `skipped_no_rect`, and have
the review UI refuse to submit (or at least warn) when a non-empty value maps to a field
with no rect.

---

## 🟡 F6. Values that don't fit, and values PyMuPDF can't draw

Two related gaps in `insert_text`:

- **No `maxlength` means no overflow check.** `validate_field` only compares length when
  `maxlength` is set (`validation.py:31`). A long value on a field without one renders off
  the edge of the box. The `destination` field had exactly this shape until today (it
  carried `maxlength: 60` against a rect sized like an 80-char field — now aligned).
- **Non-Latin text and emoji** silently render as blank or tofu with the default font. A
  patient named `José` is fine; Cyrillic, Arabic or CJK is not. Worth knowing before
  someone demos an internationalised name.

**Fix:** derive a soft max from `rect` width when `maxlength` is absent, and either embed a
Unicode font or validate the character range and flag it for review.

---

## 🟡 F7. Deleted or renamed dependencies mid-flight

`get_form_schema` is a dict lookup on schemas loaded from disk
(`supabase_api.py:153-154`), keyed by `form_templates.name`. Three ways to break it:

| What | Where it lands |
| --- | --- |
| `form_templates` row names a schema not in `contracts/schemas/` | route: clean 404. **Worker: `actions.py:155` doesn't catch → action `failed` → poisoned key** |
| The patient row is deleted after the referral | `fill_form.py:88` / `main.py:378` → unhandled `KeyError` → 500 |
| `service_id` cleared while a form action is open (`try_next_resource` does this) | `prepare` reads a stale `form_id`; the reviewer fills a form for a service no longer selected |

The third is the interesting one and I haven't traced it fully — worth a deliberate look
rather than a guess.

**Fix:** catch `KeyError` in the worker's `_service` the way the route does, and re-read
`service_id` at submit time rather than trusting the one `prepare` saw.

---

## 🟢 F9. "Patient used it ✓" needs a beat after "Org accepted ✓"

Not a defect — a race worth knowing before a demo. Accepting on behalf of the org promotes
the referral to `enrolled` and `advance_referral` queues `complete_referral` to `backend`.
Clicking **Patient used it ✓** in the next second hits the open-action guard, so the
response is `{"state": "waiting", "reason": "An action is already open"}` and the board
doesn't move.

The utilization *is* recorded (`patient_confirmed_utilization = true`); only the
transition waits. Our worker drains `complete_referral` within one poll (15s) and the
sweep advances the referral within 60s, after which the row lands in **Closed the loop**
on its own. Observed and confirmed to self-heal, 2026-08-01.

**On camera: pause a few seconds between the two clicks.** If a fix is wanted later, the
honest one is for `POST /api/patient/utilization` to also close `complete_referral` —
`backend` is our component, so it's ours to close — but racing our own worker for it needs
care, which is why it wasn't done under time pressure.

---

## 🟢 F8. Two reviewers on the same referral

Nothing serialises `POST /api/submit`. Two SWs with the review screen open both submit;
both injections run, both write back to the same `service_requests` row, last-write-wins,
and F4's attempt-burn doubles.

Low priority — one social worker per referral is the realistic case — but it's the kind of
thing that only ever shows up in a live demo with two laptops.

---

## What's already handled (don't re-do these)

- **Injection failure** — `fill_form.py:143-147` catches everything from the injector,
  records `status="failed"` with the exception text.
- **`human_only` leaking in** — stripped defensively at `fill_form.py:120` even if the UI
  sends one, and again in `fillable_fields()` inside the injector.
- **Stale/hand-edited review payload** — everything is re-validated before injection
  (`fill_form.py:124-141`), so the UI is never trusted.
- **A missing `form_id`** — clean 404 naming the seeder (`main.py:515-520`).
- **A crashed worker mid-action** — the stale sweep returns `in_progress` to `ready`
  (`worker.py`), and `blocked` is deliberately never reclaimed.
- **Action-closing failures** — `_close_open_form_action` swallows everything, because the
  injection already happened and failing the request would misreport it.

---

## Suggested order

~~F1 and F2~~ — **done 2026-08-01.**

**F3** next: five minutes, and it turns a 500 into a sentence. **F4** matters the moment
anyone double-clicks in front of an audience, and the fix is known (key the shared attempt
on the action id, as `actions.py` already does). F5–F8 are real but need someone to decide
the behaviour, not just write the fix.
