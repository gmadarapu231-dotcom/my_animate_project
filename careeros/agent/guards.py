"""The constraints the agent cannot reason its way around.

The agent chooses which jobs to investigate, which angle to take, when to dig
into a rejection pattern and when to give up on a career track. It does not get
a say in whether a resume claim is supported, whether a visa verdict is
favourable, or whether a sensitive email may be sent.

Two mechanisms enforce that, and the first matters more than the second:

**1. Absent tools.** The strongest guarantee is a capability that does not
exist. There is no tool to add evidence, override a verdict, mark a resume
final, or send mail -- so no amount of clever prompting reaches those actions.
`assert_tool_surface_is_safe()` re-checks this at registry build time, and a
test asserts it, so a future tool cannot quietly reintroduce one.

**2. Gates inside the tools that do exist.** `tailor_resume` always runs the
factuality checker and returns its verdict; `draft_email_reply` always runs
high-impact detection. The agent cannot call the action without the check,
because the check is part of the action.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Capabilities the agent must never have, and the reason each is withheld.
#: Read this as the security model, not as a to-do list.
FORBIDDEN_CAPABILITIES: dict[str, str] = {
    "add_evidence": (
        "Evidence is the user's verified truth and the sole support for every "
        "resume claim. An agent that can write evidence can manufacture the "
        "support for anything it wants to say."
    ),
    "verify_evidence": (
        "Verification is the user asserting a fact is true. The agent cannot "
        "promote its own inputs from unverified to verified."
    ),
    "edit_evidence": "Same reason as add_evidence: the substrate is not the agent's to edit.",
    "delete_evidence": "Destructive, and would let the agent hide a contradiction.",
    "set_eligibility": (
        "Work-authorization verdicts come from the country pack's rules applied "
        "to the posting's own words. They are not a model output."
    ),
    "override_verdict": "Same reason as set_eligibility.",
    "mark_resume_final": (
        "`is_final` is set by the factuality checker and by nothing else. It is "
        "the gate, so it cannot be a tool."
    ),
    "set_factuality_passed": "Would defeat the entire resume safety model.",
    "send_email": (
        "Phase 1 never auto-sends. Replies go DRAFT -> human approval -> send, "
        "and high-impact topics never leave draft state at all."
    ),
    "approve_draft": "Approval is the human's act; it is what the gate is for.",
    "submit_application": (
        "ASSISTED mode: CareerOS assembles the packet, the human submits. "
        "Nothing here bypasses CAPTCHA, MFA or bot protection."
    ),
    "execute_sql": "Arbitrary database access would route around every constraint above.",
    "run_shell": "Not a capability a career agent needs.",
}


class ToolSurfaceViolation(RuntimeError):
    """Raised when a forbidden capability appears in the tool registry."""


def assert_tool_surface_is_safe(tool_names: Iterable[str]) -> None:
    """Fail loudly if a forbidden capability ever gets registered."""
    offenders = sorted(set(tool_names) & set(FORBIDDEN_CAPABILITIES))
    if offenders:
        reasons = "; ".join(f"{name}: {FORBIDDEN_CAPABILITIES[name]}" for name in offenders)
        raise ToolSurfaceViolation(f"Forbidden capability exposed to the agent -> {reasons}")


# ---------------------------------------------------------------------------
# Gates inside the tools that do exist
# ---------------------------------------------------------------------------
def enforce_resume_gate(resume_doc: Any, report: Any) -> dict[str, Any]:
    """A resume is final only if the factuality check passed.

    Called by the `tailor_resume` tool on every invocation, so there is no code
    path that produces a resume without a verdict attached.
    """
    resume_doc.is_final = bool(report.passed)
    payload: dict[str, Any] = {
        "resume_id": resume_doc.id,
        "name": resume_doc.name,
        "storage_path": resume_doc.storage_path,
        "is_final": resume_doc.is_final,
        "factuality_passed": report.passed,
        "factuality_summary": report.summary,
        "notes": list(resume_doc.tailoring_notes or []),
    }
    if not report.passed:
        payload["unsupported_claims"] = [
            {"text": claim.text, "issues": claim.issues} for claim in report.unsupported
        ]
        payload["instruction"] = (
            "This resume CANNOT be sent. Do not describe it to the user as ready. "
            "Report the unsupported claims and the gap they came from. Never "
            "suggest adding evidence to make a claim pass."
        )
    return payload


def enforce_draft_gate(draft: Any) -> dict[str, Any]:
    """High-impact replies never leave draft state, whatever the agent decides."""
    state = draft.state.value if hasattr(draft.state, "value") else draft.state
    payload: dict[str, Any] = {
        "intent": draft.intent,
        "subject": draft.subject,
        "body": draft.body,
        "state": state,
        "high_impact_topics": list(draft.high_impact_topics),
        "may_auto_send": False,
    }
    if draft.high_impact_topics:
        payload["instruction"] = (
            "This reply touches "
            + ", ".join(draft.high_impact_topics)
            + ". It is held for manual review and cannot be approved or sent by "
            "CareerOS. Tell the user it needs their personal attention, and do "
            "not paraphrase it as ready to go."
        )
    else:
        payload["instruction"] = (
            "Draft saved. It still requires the user's approval before sending; "
            "CareerOS does not send on its own."
        )
    return payload
