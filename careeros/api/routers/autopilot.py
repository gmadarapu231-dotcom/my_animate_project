"""The autopilot: its rules, a single pass, and the history.

`POST /api/autopilot/run` is deliberately not a fire-and-forget: it runs one
pass and returns the report, because the thing a user needs after turning this
on is to see exactly what it decided and why.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.api.deps import current_user, get_db
from careeros.apply.guardrails import AutopilotPolicy
from careeros.autopilot import AlreadyRunning, RunLock, load_policy, run_once, save_policy
from careeros.db.models import PipelineRun, User

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


class PolicyUpdate(BaseModel):
    """Every field optional: a PATCH over the stored policy."""

    enabled: bool | None = None
    dry_run: bool | None = None
    interval_hours: float | None = Field(default=None, ge=0.25, le=168)
    max_per_run: int | None = Field(default=None, ge=1, le=50)
    max_per_day: int | None = Field(default=None, ge=1, le=200)
    min_match_score: float | None = Field(default=None, ge=0, le=100)
    min_priority_score: float | None = Field(default=None, ge=0, le=100)
    channels: list[str] | None = None
    allow_unknown_verdict: bool | None = None
    require_deadline_open: bool | None = None
    company_blocklist: list[str] | None = None
    company_allowlist: list[str] | None = None
    domains: list[str] | None = None
    countries: list[str] | None = None
    min_salary: float | None = Field(default=None, ge=0)
    skip_if_applicants_over: int | None = Field(default=None, ge=1)
    quiet_hours: list[str] | None = None


class RunRequest(BaseModel):
    discover: bool = True
    use_ai: bool = True


def _describe(policy: AutopilotPolicy) -> dict[str, Any]:
    return {
        "policy": policy.to_dict(),
        "summary": policy.summary(),
        "submits_for_real": policy.submits_for_real,
        "never_configurable": [
            "The tailored résumé must pass the factuality check.",
            "It must cite at least one verified evidence item.",
            "A work-authorization verdict of not_compatible stops the application.",
            "No submission bypasses a CAPTCHA, a login wall or bot protection.",
        ],
    }


@router.get("")
def get_policy(user: User = Depends(current_user)) -> dict[str, Any]:
    return _describe(load_policy(user))


@router.post("")
def update_policy(
    body: PolicyUpdate,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    policy = load_policy(user)
    changes = body.model_dump(exclude_none=True)

    for field, value in changes.items():
        current = getattr(policy, field)
        setattr(policy, field, tuple(value) if isinstance(current, tuple) else value)

    if policy.channels and not set(policy.channels) <= {"api", "email"}:
        raise HTTPException(
            status_code=422,
            detail=(
                "Only 'api' and 'email' can be automated. A web form needs a human, "
                "and a login wall is never automated at all."
            ),
        )

    save_policy(session, user, policy)
    return {**_describe(policy), "changed": sorted(changes)}


@router.post("/run")
def run(
    body: RunRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """One pass, now. Returns the full report rather than a job id."""
    try:
        with RunLock():
            report = run_once(
                session, user, discover_jobs=body.discover, use_ai=body.use_ai
            )
    except AlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return report.to_dict()


@router.get("/runs")
def history(
    limit: int = 10,
    _user: User = Depends(current_user),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = session.scalars(
        select(PipelineRun).order_by(PipelineRun.id.desc()).limit(max(1, min(limit, 100)))
    ).all()
    return {
        "count": len(rows),
        "runs": [
            {
                "id": row.id,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "ok": row.ok,
                "stats": row.stats,
                "errors": row.errors,
            }
            for row in rows
        ],
    }
