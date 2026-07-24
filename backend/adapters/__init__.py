"""Inbound seams to the teammate channel services (CLAUDE.md §7, docs/integration-plan.md).

Voice (Retell) and Messaging (Twilio) run as their OWN services in their own
directories (``backend/call_agent`` / ``backend/patient_comms``). They *execute*
their channel and emit events into our loop — they never advance our state
themselves. This package is the translation layer at that seam: it maps each
service's status vocabulary into our frozen ``{success, needs_human, failed}`` set
and calls ``scheduler.apply_inbound``, keeping our scheduler the sole owner of
``referrals.current_state``.

We import nothing from the teammate services and mutate none of their files.
"""
