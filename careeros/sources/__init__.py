"""The source registry, built from the provider specs.

`build_registry()` turns every entry in `config/sources/providers.yaml` into a
live connector and registers it, including the ones that refuse to fetch --
they are how the system explains which boards it will not scrape and what
carries their postings instead.

Nothing here fetches. Building the registry is free and offline, so the CLI,
the API and the dashboard can all list what is available and what each
provider still needs before anything touches the network.
"""

from __future__ import annotations

from typing import Any, Iterable

from careeros.sources.base import JobSource, RawJob, SourceRegistry, registry
from careeros.sources.connectors import SearchQuery, build
from careeros.sources.http import HttpClient
from careeros.sources.jsonfile import JsonFileSource, register_default
from careeros.sources.plan import SearchPlan, build_plan, career_page_query
from careeros.sources.policy import AccessTier, SourceError
from careeros.sources.spec import ProviderSpec, load_specs

__all__ = [
    "AccessTier",
    "HttpClient",
    "JobSource",
    "JsonFileSource",
    "ProviderSpec",
    "RawJob",
    "SearchPlan",
    "SearchQuery",
    "SourceError",
    "SourceRegistry",
    "build_plan",
    "build_registry",
    "career_page_query",
    "load_specs",
    "provider_status",
    "ready_providers",
    "register_default",
    "registry",
]


def build_registry(
    *,
    client: HttpClient | None = None,
    into: SourceRegistry | None = None,
    specs: Iterable[ProviderSpec] | None = None,
) -> SourceRegistry:
    """Register every declared provider. Offline and side-effect free."""
    target = into or registry
    for spec in specs if specs is not None else load_specs():
        target.register(build(spec, client))
    return target


def ready_providers(
    *, country: str | None = None, specs: Iterable[ProviderSpec] | None = None
) -> list[ProviderSpec]:
    """Providers that could run right now: permitted, credentialled, in scope."""
    return [
        spec
        for spec in (specs if specs is not None else load_specs())
        if spec.available and spec.covers(country)
    ]


def provider_status(
    *, country: str | None = None, specs: Iterable[ProviderSpec] | None = None
) -> dict[str, Any]:
    """The whole sourcing picture for a UI, with no secrets in it."""
    all_specs = list(specs if specs is not None else load_specs())
    scoped = [s for s in all_specs if s.covers(country)]
    rows = [s.status() for s in scoped]
    return {
        "country": country,
        "count": len(rows),
        "ready": sum(1 for r in rows if r["state"] == "ready"),
        "needs_credentials": sum(1 for r in rows if r["state"] == "needs_credentials"),
        "not_permitted": sum(1 for r in rows if r["state"] == "not_permitted"),
        "providers": rows,
    }
