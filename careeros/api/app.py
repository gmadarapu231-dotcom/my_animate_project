"""FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from careeros.ai.provider import get_provider
from careeros.api.routers import agent, applications, email, insights, jobs, profile, resumes
from careeros.config import countries, taxonomy
from careeros.db.session import database_url, init_db

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="CareerOS",
    version="0.1.0",
    description=(
        "A universal, domain-agnostic AI career operating system: job discovery, "
        "classification, work-authorization eligibility, evidence-based resumes, "
        "applications, email and career analytics - for any profession, in any "
        "supported country."
    ),
)

app.include_router(agent.router)
app.include_router(jobs.router)
app.include_router(profile.router)
app.include_router(resumes.router)
app.include_router(applications.router)
app.include_router(email.router)
app.include_router(insights.router)


@app.get("/api/health")
def health() -> dict[str, Any]:
    provider = get_provider()
    tax = taxonomy()
    return {
        "status": "ok",
        "database": database_url().split("://", 1)[0],
        "llm_provider": provider.name,
        "llm_available": provider.available,
        "countries": [p.code for p in countries().all()],
        "seed_domains": len(tax.domains),
        "skill_nodes": len(tax.skills),
    }


@app.get("/api/config/countries")
def country_config() -> dict[str, Any]:
    """Everything the UI needs to render a country's options."""
    return {
        "countries": [
            {
                "code": p.code,
                "name": p.name,
                "currency": p.currency,
                "currency_symbol": p.currency_symbol,
                "date_format": p.date_format,
                "terminology": p.terminology,
                "work_authorization_statuses": [
                    {"id": s.id, "label": s.label, "needs_sponsorship": s.needs_sponsorship_now}
                    for s in p.statuses.values()
                ],
                "employment_types": p.employment_types,
                "job_boards": p.job_boards,
                "reminders": p.reminders,
            }
            for p in countries().all()
        ]
    }


@app.get("/api/config/taxonomy")
def taxonomy_config() -> dict[str, Any]:
    tax = taxonomy()
    return {
        "domains": [
            {"id": d["id"], "label": d["label"], "functions": d.get("functions", [])}
            for d in tax.domains
        ],
        "seniority": tax.seniority_levels,
        "work_arrangements": tax.work_arrangements,
        "skill_count": len(tax.skills),
        "note": (
            "Seeded taxonomy only. The classifier creates new domains at runtime when a "
            "posting does not fit any of these - no code change required."
        ),
    }


if DASHBOARD_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

    @app.get("/")
    def dashboard() -> FileResponse:
        return FileResponse(str(DASHBOARD_DIR / "index.html"))
