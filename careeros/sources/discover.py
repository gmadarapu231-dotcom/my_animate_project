"""Running a search plan: ask every ready provider, then hand the results on.

This is the join between the sourcing layer and the pipeline. Postings arrive
from a dozen places with a dozen shapes, get fingerprinted into one identity,
and then go through exactly the same classification, eligibility, match and
priority stages as a posting pasted in by hand. No provider gets a special path
downstream, which is why adding one is a YAML entry.

One provider failing is never fatal. A missing key, a refused board, a host
that answered with a challenge -- each is recorded against that provider and
the run continues, because a partial list of real jobs beats an exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from sqlalchemy.orm import Session

from careeros.sources.base import JobSource, RawJob, SourceRegistry
from careeros.sources.connectors import SearchQuery
from careeros.sources.http import HttpClient
from careeros.sources.plan import SearchPlan, build_plan, career_page_query
from careeros.sources.policy import Challenge, MissingCredentials, NotPermitted, SourceError
from careeros.sources.spec import ProviderSpec

logger = logging.getLogger(__name__)


@dataclass
class ProviderOutcome:
    provider_id: str
    label: str
    searches: int = 0
    found: int = 0
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "label": self.label,
            "searches": self.searches,
            "found": self.found,
            "skipped_reason": self.skipped_reason,
            "errors": self.errors,
        }


@dataclass
class DiscoveryResult:
    jobs: list[RawJob] = field(default_factory=list)
    outcomes: list[ProviderOutcome] = field(default_factory=list)
    plan: SearchPlan | None = None

    @property
    def by_board(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for job in self.jobs:
            board = str(job.raw.get("board") or job.source)
            counts[board] = counts.get(board, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": len(self.jobs),
            "by_board": self.by_board,
            "providers": [o.to_dict() for o in self.outcomes],
            "plan": self.plan.to_dict() if self.plan else None,
        }


class CollectedSource(JobSource):
    """Wraps already-fetched postings so the pipeline ingests them normally."""

    id = "discovery"

    def __init__(self, jobs: Iterable[RawJob]) -> None:
        self._jobs = list(jobs)

    def fetch(self, limit: int | None = None, **_kwargs: Any) -> Iterator[RawJob]:
        for index, job in enumerate(self._jobs):
            if limit is not None and index >= limit:
                return
            yield job


def _google_search_query(spec: ProviderSpec, query: SearchQuery) -> SearchQuery:
    """Plain web search needs a query aimed away from the aggregators."""
    if spec.kind != "google_cse":
        return query
    return SearchQuery(
        query=career_page_query(query.query, query.country),
        location=query.location,
        country=query.country,
        page_size=query.page_size,
        max_pages=query.max_pages,
    )


def run_plan(
    plan: SearchPlan,
    registry: SourceRegistry,
    *,
    per_provider: int = 60,
    only: Iterable[str] | None = None,
) -> DiscoveryResult:
    """Execute a plan against a built registry. Never raises for one provider."""
    wanted = {p.lower() for p in only} if only else None
    result = DiscoveryResult(plan=plan)
    grouped: dict[str, list[SearchQuery]] = {}
    labels: dict[str, str] = {}
    # Overlapping searches return the same posting repeatedly -- four terms
    # against one board will. The pipeline would dedupe on ingest, but counting
    # the copies first would make the report lie about how much was found.
    seen: set[str] = set()

    for search in plan.searches:
        if wanted and search.provider_id.lower() not in wanted:
            continue
        grouped.setdefault(search.provider_id, []).append(search.query)

    for provider_id, queries in grouped.items():
        source = registry.get(provider_id)
        if source is None:
            continue
        spec: ProviderSpec = getattr(source, "spec", None)
        label = spec.label if spec else provider_id
        labels[provider_id] = label
        outcome = ProviderOutcome(provider_id=provider_id, label=label)

        if spec is not None and not spec.access.may_fetch:
            reason = " ".join((spec.reason or "not permitted").split())
            if spec.use_instead:
                reason += f" Carried instead by: {', '.join(spec.use_instead)}."
            outcome.skipped_reason = f"not fetched - {reason}"
            result.outcomes.append(outcome)
            continue
        if spec is not None and spec.missing():
            outcome.skipped_reason = "needs " + ", ".join(spec.missing())
            result.outcomes.append(outcome)
            continue

        remaining = per_provider
        for query in queries:
            if remaining <= 0:
                break
            outcome.searches += 1
            try:
                found = list(
                    source.fetch(
                        limit=remaining,
                        query=_google_search_query(spec, query) if spec else query,
                    )
                )
            except NotPermitted as exc:
                outcome.skipped_reason = str(exc)
                break
            except MissingCredentials as exc:
                outcome.skipped_reason = str(exc)
                break
            except Challenge as exc:
                # The host said no. Stop asking it, this run.
                outcome.errors.append(str(exc))
                break
            except SourceError as exc:
                outcome.errors.append(str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - one provider must not sink a run
                outcome.errors.append(f"{type(exc).__name__}: {exc}")
                logger.exception("provider %s failed", provider_id)
                continue

            fresh = []
            for job in found:
                fingerprint = job.fingerprint()
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                fresh.append(job)

            outcome.found += len(fresh)
            remaining -= len(fresh)
            result.jobs.extend(fresh)

        result.outcomes.append(outcome)

    return result


def discover(
    session: Session,
    user: Any,
    *,
    registry: SourceRegistry | None = None,
    client: HttpClient | None = None,
    terms: list[str] | None = None,
    countries: list[str] | None = None,
    only: Iterable[str] | None = None,
    per_provider: int = 60,
    ingest: bool = True,
) -> dict[str, Any]:
    """Plan, fetch, and (by default) ingest -- the whole discovery run.

    Returns a report rather than the postings: what each provider produced,
    which boards the results came from, and what the pipeline did with them.
    """
    from careeros.pipeline import Pipeline
    from careeros.sources import build_registry, load_specs

    built = registry or build_registry(into=SourceRegistry(), client=client)
    specs = [getattr(built.get(pid), "spec", None) for pid in built.ids()]
    plan = build_plan(
        user,
        [s for s in specs if s is not None] or load_specs(),
        terms=terms,
        countries=countries,
        per_provider=per_provider,
    )
    result = run_plan(plan, built, per_provider=per_provider, only=only)

    report = result.to_dict()
    if ingest and result.jobs:
        stats = Pipeline(session).ingest([CollectedSource(result.jobs)])
        report["ingest"] = stats.to_dict()
    elif ingest:
        report["ingest"] = {"fetched": 0, "inserted": 0, "duplicates": 0, "errors": []}
    return report
