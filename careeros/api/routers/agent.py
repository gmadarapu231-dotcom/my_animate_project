"""The agentic loop over HTTP."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.agent import AgentUnavailable, CareerAgent
from careeros.agent.guards import FORBIDDEN_CAPABILITIES
from careeros.api.deps import current_user, get_db
from careeros.db.models import AgentRun, AgentToolCall, User

router = APIRouter(prefix="/api/agent", tags=["agent"])


class AskRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    max_turns: int = Field(default=12, ge=1, le=30)
    effort: str = Field(default="high", pattern="^(low|medium|high|xhigh|max)$")
    use_ai_in_tools: bool = True


@router.post("/ask")
def ask(
    payload: AskRequest,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Run the agent. Claude decides which engines to call and in what order."""
    agent = CareerAgent(
        session,
        user,
        max_turns=payload.max_turns,
        effort=payload.effort,
        use_ai_inside_tools=payload.use_ai_in_tools,
    )
    try:
        result = agent.run(payload.prompt)
    except AgentUnavailable as exc:
        # 503: the deterministic pipeline still works, this path does not.
        raise HTTPException(503, str(exc)) from exc
    session.flush()
    return result.to_dict()


@router.get("/tools")
def tools(
    session: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    """The agent's tool surface, and the capabilities deliberately withheld."""
    agent = CareerAgent(session, user, client=object())   # no call is made
    return {
        "count": len(agent.tools),
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "mutating": spec.mutating,
                "parameters": sorted(spec.input_schema.get("properties", {})),
            }
            for spec in sorted(agent.tools.values(), key=lambda s: (s.mutating, s.name))
        ],
        "withheld_capabilities": [
            {"name": name, "reason": reason} for name, reason in FORBIDDEN_CAPABILITIES.items()
        ],
        "note": (
            "The agent has autonomy over strategy, not over truthfulness. The withheld "
            "capabilities do not exist as tools, so no prompt reaches them."
        ),
    }


@router.get("/runs")
def runs(
    limit: int = 20,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Audit trail: every agent run and every tool call it made."""
    rows = session.scalars(
        select(AgentRun).where(AgentRun.user_id == user.id)
        .order_by(AgentRun.id.desc()).limit(min(limit, 100))
    ).all()
    out = []
    for run in rows:
        calls = session.scalars(
            select(AgentToolCall).where(AgentToolCall.run_id == run.id)
            .order_by(AgentToolCall.id)
        ).all()
        out.append(
            {
                "run_id": run.id,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "prompt": run.prompt,
                "answer": run.answer,
                "turns": run.turns,
                "stop_reason": run.stop_reason,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "ok": run.ok,
                "error": run.error,
                "tool_calls": [
                    {
                        "turn": c.turn,
                        "name": c.name,
                        "arguments": c.arguments,
                        "mutating": c.mutating,
                        "is_error": c.is_error,
                    }
                    for c in calls
                ],
            }
        )
    return {"count": len(out), "runs": out}
