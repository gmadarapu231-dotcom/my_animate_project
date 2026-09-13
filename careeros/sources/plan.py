"""Turning a person into concrete searches.

The queries come from the user's own career tracks and evidence, not from a
list of job titles someone hard-coded. A track named "SAP Security" with
evidence in GRC produces SAP searches; a track named "Veterinary Practice
Management" would produce those, with no code change -- which is the same
domain-agnosticism rule the classifier follows.

Locations come from the work-authorization rows and stated preferences, so a
user authorised in both the US and India gets searched in both, and nobody is
searched in a country they cannot work in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from careeros.sources.connectors import SearchQuery

#: Cap per provider per run. Sourcing widely is the point; sourcing endlessly
#: burns quota on a free tier and buries the ranked list.
DEFAULT_PER_PROVIDER = 60


@dataclass
class PlannedSearch:
    provider_id: str
    query: SearchQuery
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "label": self.label,
            "query": self.query.query,
            "location": self.query.location,
            "country": self.query.country,
        }


@dataclass
class SearchPlan:
    searches: list[PlannedSearch] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)

    def for_provider(self, provider_id: str) -> list[PlannedSearch]:
        return [s for s in self.searches if s.provider_id == provider_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "countries": self.countries,
            "terms": self.terms,
            "searches": [s.to_dict() for s in self.searches],
        }


def _dedupe(values: Iterable[str], limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = " ".join((value or "").split())
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if limit and len(out) >= limit:
            break
    return out


def search_terms(user: Any, *, limit: int = 6) -> list[str]:
    """What to search for: the user's own tracks, strongest preference first.

    `CareerTrack.priority` is the user's ranking (lower is stronger), so it
    decides which terms survive the limit. Inactive tracks are left out -- a
    track the user switched off should not spend quota.
    """
    tracks = [t for t in (getattr(user, "tracks", None) or []) if getattr(t, "active", True)]
    tracks.sort(key=lambda t: getattr(t, "priority", 100))

    candidates: list[str] = []
    for track in tracks:
        # A preferred title is a better query than a track name: "Network
        # Security Engineer" matches postings, "Cybersecurity" matches noise.
        candidates.extend(str(t) for t in (getattr(track, "preferred_titles", None) or []))
        name = getattr(track, "name", None)
        if name:
            candidates.append(str(name))
    if getattr(user, "current_title", None):
        candidates.append(str(user.current_title))
    return _dedupe(candidates, limit)


def search_countries(user: Any) -> list[str]:
    """Only countries the user is actually authorised in, home first."""
    home = (getattr(user, "home_country", None) or "").upper()
    codes = [home] if home else []
    for row in getattr(user, "work_auth", None) or []:
        code = (getattr(row, "country_code", "") or "").upper()
        if code:
            codes.append(code)
    return _dedupe(codes)


def search_locations(user: Any, country: str) -> list[str]:
    """Stated preferences for that country, else the home city, else nothing.

    An empty location is meaningful: most providers read it as "anywhere in
    this country", which is the right default for someone open to relocation.
    """
    out: list[str] = []
    for preference in getattr(user, "preferred_locations", None) or []:
        if isinstance(preference, dict):
            if (preference.get("country") or "").upper() not in ("", country):
                continue
            city = preference.get("city") or preference.get("location") or ""
            region = preference.get("region") or preference.get("state") or ""
            joined = ", ".join(p for p in (str(city), str(region)) if p)
            if joined:
                out.append(joined)
        elif isinstance(preference, str):
            out.append(preference)
    home = (getattr(user, "home_country", "") or "").upper()
    if not out and country == home and getattr(user, "home_city", None):
        region = getattr(user, "home_region", None)
        out.append(", ".join(p for p in (user.home_city, region) if p))
    if getattr(user, "remote_preference", "") == "remote" or not out:
        out.append("")  # country-wide
    return _dedupe(out) or [""]


def build_plan(
    user: Any,
    providers: Iterable[Any],
    *,
    terms: list[str] | None = None,
    countries: list[str] | None = None,
    per_provider: int = DEFAULT_PER_PROVIDER,
    max_searches_per_provider: int = 4,
) -> SearchPlan:
    """One plan for a run: which provider to ask what, and where.

    A provider that covers no country the user can work in is skipped rather
    than queried pointlessly, and a provider with no country list at all (a
    remote board, an aggregator) is asked once per term.
    """
    chosen_terms = terms or search_terms(user)
    chosen_countries = countries or search_countries(user) or ["US"]
    plan = SearchPlan(countries=chosen_countries, terms=chosen_terms)

    for provider in providers:
        spec = getattr(provider, "spec", provider)
        relevant = [c for c in chosen_countries if spec.covers(c)] or (
            [] if spec.countries else chosen_countries[:1]
        )
        if not relevant:
            continue

        # Board APIs have no search parameter; one unfiltered pass per board is
        # the whole query, and filtering happens in the connector.
        if spec.kind == "ats_board":
            plan.searches.append(
                PlannedSearch(
                    spec.id,
                    SearchQuery(query="", country=relevant[0], page_size=per_provider, max_pages=1),
                    f"{spec.label}: every open role",
                )
            )
            continue

        made = 0
        for country in relevant:
            for location in search_locations(user, country)[:2]:
                for term in chosen_terms:
                    if made >= max_searches_per_provider:
                        break
                    plan.searches.append(
                        PlannedSearch(
                            spec.id,
                            SearchQuery(
                                query=term,
                                location=location,
                                country=country,
                                page_size=min(per_provider, 50),
                                max_pages=2,
                            ),
                            f"{spec.label}: {term}"
                            + (f" in {location}" if location else f" across {country}"),
                        )
                    )
                    made += 1
                if made >= max_searches_per_provider:
                    break
            if made >= max_searches_per_provider:
                break

    return plan


def career_page_query(term: str, country: str | None = None) -> str:
    """A Google query aimed at employers' own career pages, not aggregators.

    Excluding the big boards is the point: Google Jobs and the aggregators
    already cover them, and what plain search adds is the Workday and Taleo
    pages nothing else carries.
    """
    excluded = (
        "linkedin.com",
        "indeed.com",
        "glassdoor.com",
        "ziprecruiter.com",
        "monster.com",
        "careerbuilder.com",
        "dice.com",
        "naukri.com",
        "simplyhired.com",
    )
    parts = [f'"{term}"', "(careers OR jobs OR \"job description\" OR apply)"]
    if country:
        parts.append({"US": "(USA OR \"United States\")", "IN": "India"}.get(country.upper(), country))
    parts.extend(f"-site:{host}" for host in excluded)
    return " ".join(parts)
