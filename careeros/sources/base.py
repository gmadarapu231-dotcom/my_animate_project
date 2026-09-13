"""Job-source abstraction.

A source is anything that yields `RawJob`s: a board API, a careers-page
scraper, an email alert parser, or a JSON file. The pipeline only knows this
interface, so adding LinkedIn/Naukri/Greenhouse in Phase 2 means adding a class
and a registry entry -- nothing downstream changes.

Country packs declare which boards exist per country (`job_boards`), so the
source layer is configured per country rather than hard-coded.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterator

from careeros.engines.textutil import normalize


@dataclass
class RawJob:
    """A posting as the source saw it. Normalisation happens in the pipeline."""

    source: str
    external_id: str
    title: str
    company: str
    description: str
    url: str | None = None
    location: str | None = None
    country: str | None = None
    city: str | None = None
    region: str | None = None
    employment_type: str | None = None
    salary_raw: str | None = None
    posted_on: date | None = None
    deadline_on: date | None = None
    applicant_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        """Cross-source identity.

        Same role, same company, same place = same job, even when LinkedIn and
        Dice give it different ids. Title is reduced to its significant words
        so "Sr. Network Engineer" and "Senior Network Engineer" collide.
        """
        title = normalize(self.title)
        title = re.sub(r"\b(sr|snr)\b", "senior", title)
        title = re.sub(r"\b(jr)\b", "junior", title)
        title = re.sub(r"[^a-z0-9 ]", " ", title)
        words = sorted({w for w in title.split() if len(w) > 2})
        company = re.sub(r"[^a-z0-9]", "", normalize(self.company))
        place = re.sub(r"[^a-z0-9]", "", normalize(self.city or self.location or ""))
        key = f"{company}|{' '.join(words)}|{place}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


class JobSource(ABC):
    """Base class for every connector."""

    #: Stable identifier stored on each job row.
    id: str = "base"
    #: Country codes this source covers; empty means "any".
    countries: tuple[str, ...] = ()

    @abstractmethod
    def fetch(self, limit: int | None = None, **kwargs: Any) -> Iterator[RawJob]:
        """Yield postings. Implementations must be side-effect free."""

    def healthcheck(self) -> tuple[bool, str]:
        return True, "ok"


class SourceRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, JobSource] = {}

    def register(self, source: JobSource) -> JobSource:
        self._sources[source.id] = source
        return source

    def get(self, source_id: str) -> JobSource | None:
        return self._sources.get(source_id)

    def all(self, country: str | None = None) -> list[JobSource]:
        out = list(self._sources.values())
        if country:
            out = [s for s in out if not s.countries or country.upper() in s.countries]
        return out

    def ids(self) -> list[str]:
        return sorted(self._sources)


registry = SourceRegistry()
