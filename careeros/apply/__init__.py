"""Applying: channel detection, packet assembly, guardrails, submission."""

from careeros.apply.channels import (
    API_BOARDS,
    BLOCKED_HOSTS,
    FORM_BOARDS,
    Channel,
    SubmitTier,
    detect,
    find_application_email,
)
from careeros.apply.guardrails import (
    HARD_BLOCK_VERDICTS,
    AutopilotPolicy,
    Decision,
    applications_today,
    cited_evidence_ids,
    evaluate,
    in_quiet_hours,
)
from careeros.apply.packet import Answer, Packet, build
from careeros.apply.submit import (
    API_ENDPOINTS,
    Submission,
    application_email,
    prepare_assisted,
    submit,
    submit_via_api,
    submit_via_email,
)

__all__ = [
    "API_BOARDS",
    "API_ENDPOINTS",
    "Answer",
    "AutopilotPolicy",
    "BLOCKED_HOSTS",
    "Channel",
    "Decision",
    "FORM_BOARDS",
    "HARD_BLOCK_VERDICTS",
    "Packet",
    "Submission",
    "SubmitTier",
    "application_email",
    "applications_today",
    "build",
    "cited_evidence_ids",
    "detect",
    "evaluate",
    "find_application_email",
    "in_quiet_hours",
    "prepare_assisted",
    "submit",
    "submit_via_api",
    "submit_via_email",
]
