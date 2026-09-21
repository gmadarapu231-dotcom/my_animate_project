"""The FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from taxvault.api.routers import (
    auth,
    billing,
    documents,
    estimates,
    filings,
    payments,
    reference,
)
from taxvault.api.security import install_security
from taxvault.auth import development_mode
from taxvault.config import latest_year, states, supported_years
from taxvault.db.session import database_url, init_db

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
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
app.include_router(reference.router)
app.include_router(documents.router)
app.include_router(estimates.router)
app.include_router(filings.router)
app.include_router(payments.router)
app.include_router(billing.router)


@app.get("/api/health")
def health() -> dict[str, Any]:
    jurisdictions = states()
    return {
        "status": "ok",
        "database": database_url().split("://", 1)[0],
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
