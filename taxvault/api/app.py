"""The FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import logging
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from taxvault.api.routers import (
    agent,
    auth,
    billing,
    documents,
    estimates,
    filings,
    payments,
    reference,
)
from taxvault.api.security import configure_logging, install_security
from taxvault.auth import development_mode
from taxvault.config import latest_year, states, supported_years
from taxvault.db.session import database_url, init_db, ping
from taxvault.settings import environment, enforce, readiness

logger = logging.getLogger("taxvault")

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Refuse to serve a single request if this process is not configured
    # safely for the environment it claims to be. A deploy that fails loudly
    # here is cheap; one that succeeds and cannot decrypt an SSN in January
    # is not.
    enforce()
    configure_logging()
    init_db()
    logger.info("TaxVault starting: env=%s database=%s years=%s",
                environment(), database_url().split("://", 1)[0], supported_years())
    yield


app = FastAPI(
    lifespan=lifespan,
    title="TaxVault",
    version="0.1.0",
    description=(
        "Tax estimation and filing preparation: W-2 ingest, federal and state "
        "estimates in regular or planning mode, prior-year filing checks, and "
        "payment planning. An estimation and preparation system, not an IRS "
        "e-file transmitter."
    ),
)

install_security(app)

app.include_router(auth.router)
app.include_router(agent.router)
app.include_router(reference.router)
app.include_router(documents.router)
app.include_router(estimates.router)
app.include_router(filings.router)
app.include_router(payments.router)
app.include_router(billing.router)


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness and a description of what this build can do.

    Always 200 while the process is alive. Orchestrators restart on a failing
    liveness probe, and restarting will not fix an unreachable database --
    that is what `/api/ready` is for.
    """
    jurisdictions = states()
    reachable, detail = ping()
    return {
        "status": "ok",
        "database": database_url().split("://", 1)[0],
        "database_reachable": reachable,
        "database_detail": detail if not reachable else "",
        "environment": environment(),
        "tax_years": supported_years(),
        "current_year": latest_year(),
        "jurisdictions": len(jurisdictions.codes()),
        "no_income_tax_states": jurisdictions.no_tax_states(),
        "development_codes": development_mode(),
        "e_file": False,
        "note": (
            "Estimates only. Transmitting a return to the IRS requires an EFIN and a "
            "Modernized e-File connection, which this server does not have."
        ),
    }


@app.get("/api/ready")
def ready() -> JSONResponse:
    """Readiness: should this process be sent traffic right now?

    503 while the database is unreachable or a required setting is missing, so
    a load balancer takes the process out of rotation instead of serving
    errors to clients. Separate from `/api/health` because the two answer
    different questions and a restart only helps one of them.
    """
    report = readiness()
    reachable, detail = ping()
    body = {
        "ready": report.ok and reachable,
        "database": {"reachable": reachable, "detail": detail},
        **report.to_dict(),
    }
    return JSONResponse(body, status_code=200 if body["ready"] else 503)


# --- the client -------------------------------------------------------------
if WEBAPP_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> Any:
        page = WEBAPP_DIR / "index.html"
        if not page.exists():  # pragma: no cover - only if the build is missing
            return JSONResponse({"detail": "Client not installed."}, status_code=404)
        return FileResponse(page)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> Any:
        return FileResponse(WEBAPP_DIR / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Any:
        # Browsers still ask for this path; answer with the vector mark rather
        # than letting it 404 into the log on every page load.
        return FileResponse(WEBAPP_DIR / "mark.svg", media_type="image/svg+xml")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> Any:
        # Served from the root so its scope covers the whole app.
        return FileResponse(WEBAPP_DIR / "sw.js", media_type="text/javascript")
