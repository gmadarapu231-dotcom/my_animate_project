"""FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from careeros.ai.provider import get_provider
from careeros.api.routers import (
    agent,
    applications,
    auth,
    autopilot,
    email,
    insights,
    jobs,
    profile,
    resumes,
    sources,
)
from careeros.api.security import auth_required, install_security
from careeros.config import countries, taxonomy
from careeros.db.session import database_url, init_db

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
#: `npm --prefix clients/app run export:web` writes the universal app's web
#: build here. When present it is served at `/`; the built-in single-file
#: dashboard stays available at `/classic` and needs no build step.
EXPO_WEB_DIR = Path(__file__).resolve().parents[2] / "clients" / "app" / "dist"

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

install_security(app)

app.include_router(auth.router)
app.include_router(autopilot.router)
app.include_router(sources.router)
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
        "auth_required": auth_required(),
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

    @app.get("/classic")
    def classic_dashboard() -> FileResponse:
        """The zero-build dashboard. Always available, no npm required."""
        return FileResponse(str(DASHBOARD_DIR / "index.html"))


if EXPO_WEB_DIR.exists():
    # The universal app exports as a single-page bundle, so hashed assets are
    # served from disk and every other path falls back to index.html for
    # client-side routing. Without the fallback a deep link or a page refresh
    # on /insights or /job/1 would 404.
    app.mount("/_expo", StaticFiles(directory=str(EXPO_WEB_DIR / "_expo")), name="app-assets")

    #: Prefixes the SPA fallback must never answer for. An unmatched API path
    #: has to stay a JSON 404 -- serving index.html with status 200 would turn
    #: every client typo into a silent "success" returning HTML.
    _RESERVED_PREFIXES = ("api/", "docs", "redoc", "openapi.json")

    @app.get("/{full_path:path}", include_in_schema=False)
    def universal_app(full_path: str) -> Response:
        if full_path.startswith(_RESERVED_PREFIXES):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        root = EXPO_WEB_DIR.resolve()
        candidate = (root / full_path).resolve()
        # Serve real files, but only ones genuinely inside the build directory.
        if full_path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(str(candidate))
        return FileResponse(str(root / "index.html"))

elif DASHBOARD_DIR.exists():

    @app.get("/")
    def dashboard() -> FileResponse:
        return FileResponse(str(DASHBOARD_DIR / "index.html"))
