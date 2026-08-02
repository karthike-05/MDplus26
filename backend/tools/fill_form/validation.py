"""Deterministic field validation (CLAUDE.md §2, G2).

Runs before any injection. A field that fails goes to the review UI's
``needs_attention`` list — malformed values are never injected.
"""

from __future__ import annotations

import re

from contracts.models import FormField

_FORMATS = {
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "phone": re.compile(r"^\(\d{3}\) \d{3}-\d{4}$"),
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    # Accepts what the DB hands us (`09:30:00`), what a reviewer types (`2:45 PM`), and
    # 24h without seconds. Deliberately permissive on INPUT and strict on OUTPUT: the
    # write-back normalises to HH:MM:SS via mapper.normalize, because
    # `service_requests.requested_start_time` is a `time` column and "2:45 PM" is a type
    # error there. Without this format the value passed validation (7 chars, under an
    # 8-char maxlength) and only blew up at the write-back, after the PDF was already
    # injected — docs/form-failure-paths.md F2.
    "time": re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:\s*[AaPp]\.?[Mm]\.?)?$"),
}


def validate_field(field: FormField, value) -> list[str]:
    """Return a list of problems with ``value`` for ``field`` (empty == valid)."""
    errors: list[str] = []
    empty = value in (None, "")

    if field.required and empty:
        errors.append("required")
    if empty:
        return errors  # nothing more to check on an empty optional field

    text = str(value)
    if field.maxlength is not None and len(text) > field.maxlength:
        errors.append(f"too long (>{field.maxlength})")
    if field.format in _FORMATS and not _FORMATS[field.format].match(text):
        errors.append(f"bad {field.format} format")
    if field.options and text not in field.options:
        errors.append("not an allowed option")

    return errors
