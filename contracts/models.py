"""Shared source of truth — freeze early. See CLAUDE.md §5.

Every module codes against these types. Whoever touches this file announces it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FillPolicy = Literal["auto", "review", "human_only"]
TargetType = Literal["pdf", "web"]


class FormField(BaseModel):
    """One field on a form. Serves both web and pdf targets (§5c).

    The only per-target difference:
      - web fields carry ``selector``
      - pdf fields carry ``page`` + ``rect``
    """

    name: str
    fill_policy: FillPolicy = "review"
    source: str | None = None            # dotted path into the record bundle the mapper
                                         # resolves: patient.* / referral.* / service_request.*
    required: bool = False
    maxlength: int | None = None
    format: str | None = None            # e.g. "date", "phone", "email"
    options: list[str] | None = None     # for enumerated fields

    # web target
    selector: str | None = None

    # pdf target
    page: int | None = None
    rect: tuple[float, float, float, float] | None = None


class FormSchema(BaseModel):
    """A verified schema for a single form (web XOR pdf)."""

    form_id: str
    target_type: TargetType
    source_ref: str                      # URL (web) or file path (pdf)
    fields: list[FormField] = Field(default_factory=list)

    def fillable_fields(self) -> list[FormField]:
        """Fields the *agent* may fill. NEVER returns ``human_only`` fields (§2).

        Use this in the mapper/injector only. The review UI renders ALL fields
        (``self.fields``) — it must still *display* ``human_only`` fields so the
        reviewer knows to sign them by hand (§5c).
        """
        return [f for f in self.fields if f.fill_policy != "human_only"]


class ToolOutcome(BaseModel):
    """Uniform tool result (§5b). Every tool returns this and writes an ``attempts``
    row (the shared outreach log). The scheduler only ever sees this.

    Also used to record *inbound* signals (an email back from the service, a
    patient "Y" reply): the webhook handler writes a ToolOutcome and the scheduler
    applies the transition on its next pass — so "the scheduler owns transitions"
    (§7) stays true even for events it didn't dispatch.
    """

    referral_id: str
    channel: str                         # "form" | "email" | "phone" | "whatsapp" | "escalation"
    status: str                          # "success" | "needs_human" | "failed"

    # Idempotency (§10). The scheduler generates one key per dispatch, deterministic
    # per (referral_id, from_state, attempt_no), and record_attempt upserts on it —
    # so a re-run does not create a duplicate `attempts` row.
    attempt_id: str

    # The state this outcome was produced *for*. Lets the dashboard and the
    # transition table read an attempt without re-deriving context.
    from_state: str | None = None

    data: dict = Field(default_factory=dict)
    error: str | None = None


class ReviewPayload(BaseModel):
    """The review-UI contract (§6). Produced by ``fill_form.prepare()`` in Python,
    rendered by ``ReviewUI.jsx`` in React — the same JSON shape on both sides (§10).

    ``values`` never contains a ``human_only`` field value; those are listed in
    ``pending_human`` for the person to complete by hand.
    """

    referral_id: str
    form_id: str
    values: dict[str, str | None]        # field name -> proposed value (auto/review fields)
    needs_attention: list[str]           # field names that failed validation / low-confidence map
    pending_human: list[str]             # human_only field names awaiting the person
    provenance: dict[str, str]           # field name -> where the value came from


class DashboardRow(BaseModel):
    """The social-worker dashboard's read-contract (§7, §12).

    A read-only projection the SW dashboard renders (via supabase-js realtime).
    The visual layout is deferred; these are the fields it depends on, frozen now
    so backend and frontend don't diverge. ``confirmation_source`` distinguishes
    the two closing signals: the service accepted vs. the patient used the resource.
    """

    referral_id: str
    patient_name: str                    # synthetic
    service_name: str
    current_state: str                   # drives the status column / badge
    confirmation_source: str | None = None   # "org_email" | "patient_reply" | None
    needs_attention: bool = False        # True in needs_human / escalated
    updated_at: str | None = None        # ISO timestamp of the latest attempt
