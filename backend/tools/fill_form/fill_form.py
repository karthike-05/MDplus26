"""fill_form — the target-agnostic form tool (CLAUDE.md §6).

``prepare()`` produces the review-UI payload (map -> validate). ``submit()`` injects
the human-reviewed values and records a ``ToolOutcome``. Both are target-agnostic;
the Injector chosen by ``schema.target_type`` does the per-target work.
"""

from __future__ import annotations

from pathlib import Path

from contracts.models import FormSchema, ReviewPayload, ToolOutcome
from backend.db.interface import ReferralDB
from backend.mapping import mapper
from backend.tools.fill_form.injectors.base import get_injector
from backend.tools.fill_form.validation import validate_field


def build_sources(
    patient: dict, referral: dict, service_request: dict | None = None
) -> mapper.Sources:
    """The record bundle the mapper resolves a field's dotted ``source`` against:
    `patient.*`, `referral.*`, `service_request.*`.

    Request-specific trip values (pickup/destination, requested date + time, mobility
    requirements) come from `service_request` — the shared `service_requests` row that
    Voice reads too — rather than being duplicated onto the referral. Optional so
    fixture-only L1 tests can pass just the two records."""
    return {
        "patient": patient,
        "referral": referral,
        "service_request": service_request or {},
    }


def service_request_writeback(schema: FormSchema, values: dict) -> dict:
    """Reviewed form values -> `service_requests` columns.

    Each field's own ``source`` is the mapping, used in reverse: a field sourced from
    ``service_request.pickup_address`` writes back to that column. So the schema stays
    the single place the correspondence is declared, and adding a field needs no change
    here. Skips blanks — never overwrite a stored value with an empty one — and skips
    ``human_only`` fields, which the agent may not fill at all (§2).

    Values are normalised through the field's own ``format`` on the way out. The PDF gets
    what the reviewer typed ("2:45 PM", which is what a human should read on a form); the
    DB gets ``14:45:00``, because ``requested_start_time`` is a Postgres ``time`` column
    and the reviewer's spelling is a type error there (F2).
    """
    out: dict = {}
    for field in schema.fillable_fields():
        root, _, column = (field.source or "").partition(".")
        if root == "service_request" and column and values.get(field.name) not in (None, ""):
            out[column] = mapper.normalize(values[field.name], field.format)
    return out


def prepare_from_records(
    referral_id: str, schema: FormSchema, sources: mapper.Sources
) -> ReviewPayload:
    """Map + validate every field into the review payload. Pure (no I/O) — the
    correctness core, testable with fixtures alone (§9 L1)."""
    values: dict[str, str | None] = {}
    needs_attention: list[str] = []
    pending_human: list[str] = []
    provenance: dict[str, str] = {}

    for field in schema.fields:
        if field.fill_policy == "human_only":
            pending_human.append(field.name)  # never auto-filled (§2)
            continue

        value = mapper.map_field(field, sources)
        values[field.name] = value
        if field.source:
            provenance[field.name] = field.source
        if validate_field(field, value):
            needs_attention.append(field.name)

    return ReviewPayload(
        referral_id=referral_id,
        form_id=schema.form_id,
        values=values,
        needs_attention=needs_attention,
        pending_human=pending_human,
        provenance=provenance,
    )


async def prepare(referral_id: str, db: ReferralDB) -> ReviewPayload:
    """Load records via the DB seam and build the review payload."""
    referral = await db.get_referral(referral_id)
    patient = await db.get_patient(referral["patient_id"])
    schema = await db.get_form_schema(referral["form_id"])
    service_request = await db.get_service_request(referral_id)
    return prepare_from_records(
        referral_id, schema, build_sources(patient, referral, service_request)
    )


async def submit(
    referral_id: str,
    reviewed_values: dict,
    db: ReferralDB,
    *,
    attempt_id: str,
    from_state: str | None = None,
    out_path: str | Path | None = None,
    **inject_opts,
) -> ToolOutcome:
    """Inject the human-reviewed values and record a ``ToolOutcome`` (§5b, §6).

    ``reviewed_values`` are what the reviewer confirmed in the UI. We re-validate
    them deterministically *before any injection* (G: malformed values are never
    injected) and strip ``human_only`` fields defensively (§2), then hand the clean
    set to the Injector chosen by ``schema.target_type``. The scheduler owns the
    ``attempt_id`` (idempotency, §10) and the ``from_state``; this tool never
    mutates ``referrals.current_state`` — it just records and returns.
    """
    referral = await db.get_referral(referral_id)
    schema = await db.get_form_schema(referral["form_id"])

    # Defensively drop human_only values even if the UI sent one (§2).
    human_only = {f.name for f in schema.fields if f.fill_policy == "human_only"}
    clean = {k: v for k, v in reviewed_values.items() if k not in human_only}

    # Re-validate before injection — the review UI could be stale or hand-edited.
    problems: dict[str, list[str]] = {}
    for field in schema.fillable_fields():  # excludes human_only (§2)
        errs = validate_field(field, clean.get(field.name))
        if errs:
            problems[field.name] = errs

    if problems:
        outcome = ToolOutcome(
            referral_id=referral_id,
            channel="form",
            status="needs_human",
            attempt_id=attempt_id,
            from_state=from_state,
            data={"problems": problems},
            error="validation failed before injection",
        )
        await db.record_attempt(outcome)
        return outcome

    injector = get_injector(schema.target_type)
    try:
        confirmation = await injector.inject(schema, clean, out_path=out_path, **inject_opts)
        status, error = "success", None
    except Exception as exc:  # a broken selector / unwritable path is a real failure
        confirmation, status, error = {}, "failed", f"{type(exc).__name__}: {exc}"

    # Persist what the reviewer confirmed back onto the shared service_requests row,
    # so Voice and the dashboard see the corrected trip details rather than only the
    # PDF holding them. Only on success: a failed injection must not leave the shared
    # row claiming values that were never submitted.
    #
    # GUARDED, AND NEVER ALLOWED TO RAISE. The injection has already happened by this
    # point — the PDF is written, the application is out. If this throws (a type error on
    # a `time`/`date` column, a value too long for it, a network blip) and the exception
    # escapes, the ToolOutcome below is never recorded, the caller never closes the
    # action, `advance_referral`'s open-action guard freezes the referral, and the
    # reviewer is told their submit failed — for a submission that really happened.
    #
    # That is the `save_call_outcome` failure shape (changes-2026-07-31 §2) where one bad
    # field killed the whole post-call chain. Bookkeeping must not roll back a side effect
    # that already left the building, so a write-back failure is REPORTED in
    # `outcome.data` and the submit still succeeds.
    if status == "success":
        writeback = service_request_writeback(schema, clean)
        if writeback:
            try:
                await db.save_service_request(referral_id, writeback)
            except Exception as exc:  # noqa: BLE001 — see above
                confirmation = {
                    **confirmation,
                    "writeback_failed": f"{type(exc).__name__}: {exc}",
                    "writeback_values": writeback,
                }

    outcome = ToolOutcome(
        referral_id=referral_id,
        channel="form",
        status=status,
        attempt_id=attempt_id,
        from_state=from_state,
        data=confirmation,
        error=error,
    )
    await db.record_attempt(outcome)
    return outcome
