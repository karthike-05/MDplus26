"""Value mapping: deterministic copy + normalization, with ONE guarded Claude
call as an optional enhancement (CLAUDE.md §2, §11).

The deterministic path is authoritative and runs offline (no ANTHROPIC_API_KEY
needed). Claude is only ever a *fallback enrichment* for values a rule can't map,
and whatever it returns is re-validated downstream before anything is injected —
never trusted directly (§2: no live LLM in the submission path).
"""

from __future__ import annotations

import re
from datetime import datetime

from contracts.models import FormField

Sources = dict[str, dict]  # {"patient": {...}, "referral": {...}, "service_request": {...}}


def resolve(source: str | None, sources: Sources):
    """Resolve a dotted ``source`` ("patient.dob") against the record bundle."""
    if not source:
        return None
    root, _, key = source.partition(".")
    return sources.get(root, {}).get(key)


def _normalize_date(value: str) -> str:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value.strip()  # leave as-is; validation will flag it


def _normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return value.strip()  # leave as-is; validation will flag it


def _normalize_time(value: str) -> str:
    """-> `HH:MM:SS`. `requested_start_time` is a Postgres `time` column, so a reviewer's
    "2:45 PM" is a type error that fails the whole write-back statement (F2). Parse the
    forms a human or the DB actually produces; leave anything else alone for validation
    to flag rather than guessing."""
    text = value.strip().upper().replace(".", "")
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p", "%I %p"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return value.strip()  # leave as-is; validation will flag it


def normalize(value, fmt: str | None):
    if value is None:
        return None
    value = str(value)
    if fmt == "date":
        return _normalize_date(value)
    if fmt == "phone":
        return _normalize_phone(value)
    if fmt == "time":
        return _normalize_time(value)
    return value.strip()


def map_field(field: FormField, sources: Sources):
    """Return the proposed value for one field (never called for human_only)."""
    raw = resolve(field.source, sources)
    if raw in (None, ""):
        return None
    return normalize(raw, field.format)
