"""The five connector kinds every provider is built from.

`rest`                 a JSON search endpoint with paging and a field map
`ats_board`            a per-employer JSON board (Greenhouse, Lever, Ashby...)
`rss`                  an RSS/Atom feed the site publishes
`serpapi_google_jobs`  Google Jobs, via a licensed results API
`google_cse`           Google Search, then the page's own schema.org JobPosting
`prohibited`           refuses, and says what to use instead

Each one yields `RawJob`. Nothing here normalises: country resolution, salary
parsing and employment-type detection all happen once, downstream, so no
connector has to know how India writes a salary.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterator

from careeros.sources.base import JobSource, RawJob
from careeros.sources.http import HttpClient
from careeros.sources.jsonld import job_postings, strip_html, to_fields
from careeros.sources.mapping import TRANSFORMS, as_text, map_item, resolve_path
from careeros.sources.policy import (
    AccessTier,
    Challenge,
    MissingCredentials,
    NotPermitted,
    SourceError,
)
from careeros.sources.spec import CredentialSlot, ProviderSpec

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

#: Careerjet wants a locale, not a country code.
_CAREERJET_LOCALE = {"US": "en_US", "IN": "en_IN", "GB": "en_GB", "CA": "en_CA", "AU": "en_AU"}


@dataclass
class SearchQuery:
    """One concrete search. Providers that ignore a field simply drop it."""

    query: str = ""
    location: str = ""
    country: str | None = None
    page_size: int = 50
    max_pages: int = 2

    def variables(self) -> dict[str, Any]:
        code = (self.country or "").upper()
        return {
            "query": self.query,
            "location": self.location,
            "country": code,
            "country_lower": code.lower(),
            "page_size": self.page_size,
            "careerjet_locale": _CAREERJET_LOCALE.get(code, "en_US"),
        }


def _fill(template: Any, variables: dict[str, Any]) -> Any:
    """Substitute `{name}` from `variables`; leave unknown names in place."""
    if not isinstance(template, str):
        return template
    if template == "{page_size}":
        return variables.get("page_size")
    return _PLACEHOLDER.sub(
        lambda m: str(variables.get(m.group(1), m.group(0))), template
    )


def _fill_tree(node: Any, variables: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {k: _fill_tree(v, variables) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill_tree(v, variables) for v in node]
    return _fill(node, variables)


class SpecSource(JobSource):
    """Shared behaviour: credentials, auth shapes, and result assembly."""

    def __init__(self, spec: ProviderSpec, client: HttpClient | None = None) -> None:
        self.spec = spec
        self.id = spec.id
        self.countries = spec.countries
        defaults = spec.defaults
        self.client = client or HttpClient(
            user_agent=defaults.get("user_agent") or "CareerOS/0.1",
            timeout=float(defaults.get("timeout_seconds", 20)),
            rate_limit_per_minute=int(defaults.get("rate_limit_per_minute", 20)),
        )

    # -- credentials -------------------------------------------------------
    def _require_credentials(self) -> dict[str, Any]:
        missing = self.spec.missing()
        if missing:
            raise MissingCredentials(self.spec.id, missing)
        values: dict[str, Any] = {}
        for slot in self.spec.credentials:
            value = slot.read()
            if value is not None:
                values[slot.name] = value
        return values

    def _auth(self, creds: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
        """(query params, headers, path variables) for the declared auth style."""
        params: dict[str, Any] = {}
        headers: dict[str, str] = {}
        path: dict[str, Any] = {}
        style = self.spec.auth_style
        if style == "query":
            params.update({k: v for k, v in creds.items() if isinstance(v, str)})
        elif style == "header":
            headers.update({k: v for k, v in creds.items() if isinstance(v, str)})
        elif style == "bearer":
            token = creds.get("token") or next(iter(creds.values()), "")
            headers["Authorization"] = f"Bearer {token}"
        elif style == "path":
            path.update(creds)
        return params, headers, path

    def healthcheck(self) -> tuple[bool, str]:
        if not self.spec.access.may_fetch:
            return False, f"not permitted: {self.spec.reason.strip().splitlines()[0] if self.spec.reason else ''}"
        missing = self.spec.missing()
        if missing:
            return False, f"set {', '.join(missing)}"
        if not self.spec.verified:
            return True, "ready (endpoint unverified - check once keyed)"
        return True, "ready"

    # -- assembly ----------------------------------------------------------
    def _raw_job(self, item: Any, variables: dict[str, Any], index: int) -> RawJob | None:
        fields = map_item(item, self.spec.mapping, variables)
        title = as_text(fields.get("title"))
        if not title:
            return None
        external = as_text(fields.get("external_id")) or f"{self.spec.id}-{index}"
        posted = fields.get("posted_on")
        deadline = fields.get("deadline_on")
        return RawJob(
            source=self.spec.id,
            external_id=str(external),
            title=title,
            company=as_text(fields.get("company")) or "Unknown",
            description=as_text(fields.get("description")) or "",
            url=as_text(fields.get("url")),
            location=as_text(fields.get("location")),
            country=as_text(fields.get("country")) or variables.get("country") or None,
            city=as_text(fields.get("city")),
            region=as_text(fields.get("region")),
            employment_type=as_text(fields.get("employment_type")),
            salary_raw=as_text(fields.get("salary_raw")),
            posted_on=posted if isinstance(posted, date) else None,
            deadline_on=deadline if isinstance(deadline, date) else None,
            applicant_count=fields.get("applicant_count"),
            raw={
                "provider": self.spec.id,
                "board": as_text(fields.get("board")) or self.spec.label,
                "access": self.spec.access.value,
                "payload": item if isinstance(item, dict) else {"value": item},
            },
        )


class RestSource(SpecSource):
    """A JSON search endpoint. Paging is declared, not inferred."""

    def _pages(self, query: SearchQuery) -> Iterator[int]:
        resp = self.spec.response
        if resp.get("single_page"):
            yield resp.get("first_page", 1)
            return
        first = int(resp.get("first_page", 1))
        stride = int(resp.get("page_stride", 1))
        for i in range(max(1, min(query.max_pages, int(self.spec.defaults.get("max_pages", 4))))):
            yield first + i * stride

    def _items(self, payload: Any) -> list[Any]:
        path = self.spec.response.get("items", "")
        node = resolve_path(payload, path) if path else payload
        if isinstance(node, list):
            return node
        if isinstance(node, dict):
            return [node]
        return []

    def fetch(self, limit: int | None = None, **kwargs: Any) -> Iterator[RawJob]:
        query: SearchQuery = kwargs.get("query") or SearchQuery()
        creds = self._require_credentials()
        auth_params, auth_headers, path_vars = self._auth(creds)

        base_vars = {**query.variables(), **path_vars, "user_agent": self.client.user_agent}
        url_template = self.spec.request.get("url", "")
        method = str(self.spec.request.get("method", "GET")).upper()
        page_param = self.spec.response.get("page_param")
        count = 0

        for page in self._pages(query):
            variables = {**base_vars, "page": page}
            url = _fill(url_template, variables)
            params = {**_fill_tree(self.spec.request.get("params") or {}, variables), **auth_params}
            headers = {**_fill_tree(self.spec.request.get("headers") or {}, variables), **auth_headers}
            json_body = self.spec.request.get("json")
            if json_body is not None:
                json_body = _fill_tree(json_body, variables)
            # Paging goes in the path when the URL asks for it, otherwise in a
            # query parameter, otherwise in the JSON body.
            if page_param and "{page}" not in url_template and page_param not in ("{path}", "json"):
                params[page_param] = page

            try:
                response = self.client.fetch(
                    url,
                    method=method,
                    params=params if method == "GET" else params or None,
                    headers=headers,
                    json_body=json_body,
                    tier=self.spec.access,
                )
            except Challenge:
                raise
            except SourceError:
                if count:
                    return  # partial results beat none
                raise

            items = self._items(response.json())
            if not items:
                return
            for item in items:
                if limit is not None and count >= limit:
                    return
                job = self._raw_job(item, variables, count)
                if job:
                    yield job
                    count += 1


class AtsBoardSource(SpecSource):
    """One employer's board, read from the ATS's public job-board API."""

    def _tokens(self) -> list[str]:
        raw = self.spec.request.get("tokens")
        if isinstance(raw, CredentialSlot):
            value = raw.read()
            return list(value) if isinstance(value, list) else ([value] if value else [])
        if isinstance(raw, str):
            return [raw]
        return list(raw or [])

    def fetch(self, limit: int | None = None, **kwargs: Any) -> Iterator[RawJob]:
        query: SearchQuery = kwargs.get("query") or SearchQuery()
        tokens = self._tokens()
        if not tokens:
            slot = self.spec.request.get("tokens")
            env = slot.env if isinstance(slot, CredentialSlot) else "(tokens)"
            raise MissingCredentials(self.spec.id, [env])

        needle = (query.query or "").lower().strip()
        count = 0
        for token in tokens:
            variables = {**query.variables(), "token": token, "page": 1}
            url = _fill(self.spec.request.get("url", ""), variables)
            params = _fill_tree(self.spec.request.get("params") or {}, variables)
            try:
                response = self.client.fetch(url, params=params, tier=self.spec.access)
            except Challenge:
                raise
            except SourceError:
                continue  # one bad board token must not sink the rest

            payload = response.json()
            node = resolve_path(payload, self.spec.response.get("items", ""))
            items = node if isinstance(node, list) else (payload if isinstance(payload, list) else [])
            for item in items:
                if limit is not None and count >= limit:
                    return
                job = self._raw_job(item, variables, count)
                if not job:
                    continue
                # Board APIs have no search parameter, so filtering is ours.
                if needle and needle not in f"{job.title} {job.description}".lower():
                    continue
                yield job
                count += 1


class RssSource(SpecSource):
    """Any RSS/Atom job feed, including a board's own saved-search feed."""

    _NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}

    def _feeds(self) -> list[str]:
        raw = self.spec.request.get("feeds")
        if isinstance(raw, CredentialSlot):
            value = raw.read()
            return list(value) if isinstance(value, list) else ([value] if value else [])
        if isinstance(raw, str):
            return [raw]
        return list(raw or [])

    def healthcheck(self) -> tuple[bool, str]:
        feeds = self._feeds()
        if not feeds:
            slot = self.spec.request.get("feeds")
            env = slot.env if isinstance(slot, CredentialSlot) else "(feeds)"
            return False, f"set {env}"
        return True, f"{len(feeds)} feed(s)"

    @staticmethod
    def _first(entry: ET.Element, *names: str) -> str:
        for name in names:
            found = entry.find(name)
            if found is None:
                found = entry.find(f"atom:{name}", RssSource._NS)
            if found is not None:
                if name == "link" and not (found.text or "").strip():
                    return (found.get("href") or "").strip()
                return (found.text or "").strip()
        return ""

    def fetch(self, limit: int | None = None, **kwargs: Any) -> Iterator[RawJob]:
        query: SearchQuery = kwargs.get("query") or SearchQuery()
        feeds = self._feeds()
        if not feeds:
            slot = self.spec.request.get("feeds")
            raise MissingCredentials(
                self.spec.id, [slot.env if isinstance(slot, CredentialSlot) else "(feeds)"]
            )
        needle = (query.query or "").lower().strip()
        count = 0
        for feed_url in feeds:
            try:
                response = self.client.fetch(feed_url, tier=self.spec.access)
                root = ET.fromstring(response.body)
            except Challenge:
                raise
            except (SourceError, ET.ParseError):
                continue
            entries = root.findall(".//item") or root.findall(".//atom:entry", self._NS)
            for entry in entries:
                if limit is not None and count >= limit:
                    return
                title = self._first(entry, "title")
                if not title:
                    continue
                summary = strip_html(
                    self._first(entry, "description", "summary", "content")
                )
                if needle and needle not in f"{title} {summary}".lower():
                    continue
                link = self._first(entry, "link", "id")
                company = self._first(entry, "{http://purl.org/dc/elements/1.1/}creator", "author") or "Unknown"
                # Many feeds write "Title at Company" or "Title - Company".
                if company == "Unknown":
                    parted = re.split(r"\s+(?:at|@|-|–)\s+", title, maxsplit=1)
                    if len(parted) == 2 and len(parted[1]) < 60:
                        title, company = parted[0].strip(), parted[1].strip()
                posted = TRANSFORMS["date"](self._first(entry, "pubDate", "published", "updated", "date"))
                yield RawJob(
                    source=self.spec.id,
                    external_id=link or f"{self.spec.id}-{count}",
                    title=title,
                    company=company,
                    description=summary,
                    url=link or None,
                    location=self._first(entry, "location") or None,
                    country=query.country,
                    posted_on=posted if isinstance(posted, date) else None,
                    raw={
                        "provider": self.spec.id,
                        "board": self.spec.label,
                        "access": self.spec.access.value,
                        "feed": feed_url,
                    },
                )
                count += 1


class GoogleJobsSource(RestSource):
    """Google Jobs through a licensed results API.

    This is the answer to "I want LinkedIn, Indeed, ZipRecruiter, Dice,
    Glassdoor, Monster and CareerBuilder": Google Jobs indexes all of them with
    their participation, and each result names its origin in `via`, which is
    kept as the job's board so per-board analytics still mean something.
    """

    _VIA = re.compile(r"(?i)^\s*(?:via\s+)?(.+?)\s*$")

    def _raw_job(self, item: Any, variables: dict[str, Any], index: int) -> RawJob | None:
        job = super()._raw_job(item, variables, index)
        if job is None:
            return None
        via = as_text(resolve_path(item, "via")) or ""
        match = self._VIA.match(via)
        if match:
            job.raw["board"] = match.group(1)
        return job


class GoogleCseSource(SpecSource):
    """Google Search, then read the posting from the page's own structured data.

    Search finds candidate URLs; the posting itself comes from the schema.org
    `JobPosting` the employer publishes on that page. A page without one is
    skipped rather than guessed at, and every page fetch is robots-checked.
    """

    def fetch(self, limit: int | None = None, **kwargs: Any) -> Iterator[RawJob]:
        query: SearchQuery = kwargs.get("query") or SearchQuery()
        creds = self._require_credentials()
        auth_params, auth_headers, _ = self._auth(creds)

        link_field = self.spec.resolve.get("link", "link")
        first = int(self.spec.response.get("first_page", 1))
        stride = int(self.spec.response.get("page_stride", 10))
        page_param = self.spec.response.get("page_param", "start")
        seen: set[str] = set()
        count = 0

        for i in range(max(1, min(query.max_pages, int(self.spec.defaults.get("max_pages", 4))))):
            variables = {**query.variables(), "page": first + i * stride}
            params = {
                **_fill_tree(self.spec.request.get("params") or {}, variables),
                **auth_params,
                page_param: variables["page"],
            }
            try:
                response = self.client.fetch(
                    _fill(self.spec.request.get("url", ""), variables),
                    params=params,
                    headers=auth_headers,
                    tier=self.spec.access,
                )
            except Challenge:
                raise
            except SourceError:
                if count:
                    return
                raise

            results = resolve_path(response.json(), self.spec.response.get("items", "items")) or []
            if not results:
                return
            for result in results:
                if limit is not None and count >= limit:
                    return
                link = as_text(resolve_path(result, link_field))
                if not link or link in seen:
                    continue
                seen.add(link)
                for job in self._read_page(link, query, count):
                    yield job
                    count += 1
                    if limit is not None and count >= limit:
                        return

    def _read_page(self, url: str, query: SearchQuery, index: int) -> list[RawJob]:
        """One page fetch, robots-checked, structured data only."""
        try:
            page = self.client.fetch(url, tier=AccessTier.PUBLIC_PAGE)
        except Challenge:
            return []  # the site is protecting itself; that answer is respected
        except SourceError:
            return []

        out: list[RawJob] = []
        for node in job_postings(page.body):
            fields = to_fields(node)
            if not fields.get("title"):
                continue
            out.append(
                RawJob(
                    source=self.spec.id,
                    external_id=str(fields.get("external_id") or url),
                    title=fields["title"],
                    company=fields.get("company") or "Unknown",
                    description=fields.get("description") or "",
                    url=fields.get("url") or url,
                    location=fields.get("location"),
                    country=fields.get("country") or query.country,
                    city=fields.get("city"),
                    region=fields.get("region"),
                    employment_type=fields.get("employment_type"),
                    salary_raw=fields.get("salary_raw"),
                    posted_on=fields.get("posted_on"),
                    deadline_on=fields.get("deadline_on"),
                    raw={
                        "provider": self.spec.id,
                        "board": "Company career site",
                        "access": AccessTier.PUBLIC_PAGE.value,
                        "found_at": url,
                    },
                )
            )
        return out


class ProhibitedSource(SpecSource):
    """Registered so the system can explain itself, and refuses to fetch.

    Keeping these as first-class entries is the point: the dashboard can list
    LinkedIn and Naukri, say exactly why they are not being scraped, and name
    the providers that carry their postings instead.
    """

    def fetch(self, limit: int | None = None, **kwargs: Any) -> Iterator[RawJob]:
        raise NotPermitted(self.spec.id, self.spec.reason, list(self.spec.use_instead))
        yield  # pragma: no cover - makes this a generator for callers


KINDS: dict[str, type[SpecSource]] = {
    "rest": RestSource,
    "ats_board": AtsBoardSource,
    "rss": RssSource,
    "serpapi_google_jobs": GoogleJobsSource,
    "google_cse": GoogleCseSource,
    "prohibited": ProhibitedSource,
}


def build(spec: ProviderSpec, client: HttpClient | None = None) -> SpecSource:
    cls = KINDS.get(spec.kind)
    if cls is None:
        raise ValueError(f"provider {spec.id!r}: unknown kind {spec.kind!r}")
    return cls(spec, client)
