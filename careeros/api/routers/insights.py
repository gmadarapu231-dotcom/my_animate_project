"""Daily recommendations, analytics dashboard and the career learning loop."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.analytics import Analytics
from careeros.api.deps import current_user, get_db, pipeline
from careeros.db.models import PipelineRun, Recommendation, User
from careeros.pipeline import Pipeline
from careeros.sources.jsonfile import JsonFileSource

router = APIRouter(prefix="/api", tags=["insights"])


class DailyRunRequest(BaseModel):
    path: str | None = None
    tailor_limit: int = 5
    use_ai: bool = True


@router.get("/recommendations")
def recommendations(
    refresh: bool = False,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    pipe: Pipeline = Depends(pipeline),
) -> dict[str, Any]:
    """"What should I apply for today?" """
    today = date.today()
    row = session.scalars(
        select(Recommendation).where(
            Recommendation.user_id == user.id, Recommendation.for_date == today
        )
    ).first()
    if row is None or refresh:
        row = pipe.recommend(user, for_date=today)
        session.flush()
    return {
        "for_date": row.for_date.isoformat(),
        "top_track_id": row.top_track_id,
        "track_reasons": row.track_reasons,
        "top_jobs": row.top_jobs,
        "narrative": row.narrative,
    }


@router.get("/analytics")
def analytics(
    session: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    return Analytics(session).dashboard(user.id)


@router.get("/analytics/learning")
def learning(
    session: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    signals = Analytics(session).learning_signals(user.id)
    return {
        "count": len(signals),
        "signals": [s.to_dict() for s in signals],
        "disclaimer": (
            "Suggestions are derived from your own outcome history. CareerOS never "
            "recommends claiming a qualification you do not hold."
        ),
    }


@router.post("/run-daily")
def run_daily(
    payload: DailyRunRequest,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    pipe: Pipeline = Depends(pipeline),
) -> dict[str, Any]:
    sources = [JsonFileSource(payload.path)] if payload.path else []
    run = pipe.run_daily(
        sources, user=user, tailor_limit=payload.tailor_limit, use_ai=payload.use_ai
    )
    session.flush()
    return {
        "ok": run.ok,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "stats": run.stats,
        "errors": run.errors,
    }


@router.get("/runs")
def runs(session: Session = Depends(get_db)) -> dict[str, Any]:
    rows = session.scalars(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(20)).all()
    return {
        "runs": [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "ok": r.ok,
                "stats": r.stats,
                "errors": r.errors,
            }
            for r in rows
        ]
    }
