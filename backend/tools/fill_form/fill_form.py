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


def build_sources(patient: dict, referral: dict) -> mapper.Sources:
    """The record bundle the mapper resolves `patient.*` / `referral.*` against."""
    return {"patient": patient, "referral": referral}


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
    return prepare_from_records(referral_id, schema, build_sources(patient, referral))


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
