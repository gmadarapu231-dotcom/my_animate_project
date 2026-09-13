"""Where jobs come from, and running a discovery pass."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from careeros.api.deps import current_user, get_db
from careeros.db.models import User
from careeros.sources import build_registry, load_specs, provider_status
from careeros.sources.base import SourceRegistry
from careeros.sources.discover import discover
from careeros.sources.plan import build_plan

router = APIRouter(prefix="/api/sources", tags=["sources"])


class DiscoverRequest(BaseModel):
    terms: list[str] | None = Field(default=None, description="Override the search terms.")
    countries: list[str] | None = Field(default=None, description="Override the countries.")
    only: list[str] | None = Field(default=None, description="Restrict to these provider ids.")
    per_provider: int = Field(default=60, ge=1, le=200)
    ingest: bool = True


@router.get("")
def list_sources(country: str | None = Query(default=None)) -> dict[str, Any]:
    """Every provider, its access tier, and what it still needs.

    Includes the boards that are deliberately not scraped, each with the reason
    and the providers that carry their postings instead -- a board missing from
    the results should never be a mystery.
    """
    return provider_status(country=country)


@router.get("/plan")
def plan(
    user: User = Depends(current_user),
    _session: Session = Depends(get_db),
) -> dict[str, Any]:
    """What would be searched for this account, without searching anything."""
    specs = load_specs()
    built = build_plan(user, [s for s in specs if s.available])
    return {
        **built.to_dict(),
        "ready_providers": [s.id for s in specs if s.available],
        "note": (
            "Terms come from your career tracks and current title; countries "
            "from your work-authorization rows. Nothing is hard-coded."
        ),
    }


@router.post("/discover")
def run_discovery(
    body: DiscoverRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Fetch from every ready provider and ingest what comes back."""
    return discover(
        session,
        user,
        registry=build_registry(into=SourceRegistry()),
        terms=body.terms,
        countries=body.countries,
        only=body.only,
        per_provider=body.per_provider,
        ingest=body.ingest,
    )
