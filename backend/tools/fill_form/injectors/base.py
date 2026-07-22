"""Injector seam (CLAUDE.md §6).

fill_form is target-agnostic: it maps + validates, then hands clean values to an
Injector chosen by ``schema.target_type``. Adding an API-based submission later is
one more Injector — mapping, validation, and the review UI never change.
"""

from __future__ import annotations

from typing import Protocol

from contracts.models import FormSchema


class Injector(Protocol):
    async def inject(self, schema: FormSchema, values: dict, **opts) -> dict:
        """Write ``values`` onto the target, leaving ``human_only`` blank.

        Returns confirmation data (goes into ``ToolOutcome.data``). Must NOT fill
        ``human_only`` fields — use ``schema.fillable_fields()`` (§2).
        """
        ...


def get_injector(target_type: str) -> Injector:
    if target_type == "pdf":
        from backend.tools.fill_form.injectors.pdf_injector import PdfInjector
        return PdfInjector()
    if target_type == "web":
        from backend.tools.fill_form.injectors.web_injector import WebInjector
        return WebInjector()
    raise ValueError(f"no injector for target_type {target_type!r}")
