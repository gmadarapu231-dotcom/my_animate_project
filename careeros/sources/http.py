"""The one way connectors reach the network.

Deliberately stdlib-only: `urllib.request`, so the sourcing layer adds no
dependency to a project whose whole point is that it degrades rather than
breaks. `Transport` is a protocol so tests inject recorded payloads and the
suite never opens a socket.

Every request goes through the rate limiter, and every response through
`looks_like_challenge`. A challenge disables the host for the life of the
client -- it is not retried with different headers, because that is the
circumvention the project forbids.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from careeros.sources.policy import (
    AccessTier,
    Challenge,
    RateLimiter,
    RobotsPolicy,
    SourceError,
    host_of,
    looks_like_challenge,
    redact,
)

DEFAULT_USER_AGENT = "CareerOS/0.1 (+https://github.com/gmadarapu231-dotcom/my_animate_project)"


@dataclass
class Response:
    status: int
    body: str
    url: str
    headers: dict[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except json.JSONDecodeError as exc:
            raise SourceError(f"{self.url}: response was not JSON ({exc})") from exc


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response: ...


class UrllibTransport:
    """The real one. No redirect chasing beyond urllib's default."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response:
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed schemes
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                charset = resp.headers.get_content_charset() or "utf-8"
                return Response(
                    status=resp.status,
                    body=raw.decode(charset, errors="replace"),
                    url=resp.geturl(),
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            return Response(
                status=exc.code,
                body=raw.decode("utf-8", errors="replace"),
                url=url,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            )
        except urllib.error.URLError as exc:
            raise SourceError(f"{host_of(url)}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise SourceError(f"{host_of(url)}: timed out after {timeout}s") from exc


class HttpClient:
    def __init__(
        self,
        transport: Transport | None = None,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        rate_limit_per_minute: int = 20,
        allowed_schemes: tuple[str, ...] = ("https", "http"),
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.user_agent = user_agent
        self.timeout = timeout
        self.limiter = RateLimiter(per_minute=rate_limit_per_minute)
        self.allowed_schemes = allowed_schemes
        self.robots = RobotsPolicy(fetch_text=self._robots_text, user_agent=user_agent)
        #: Hosts that answered with a challenge. Never retried this run.
        self.blocked: dict[str, str] = {}

    # -- robots ------------------------------------------------------------
    def _robots_text(self, url: str) -> str | None:
        try:
            resp = self._raw("GET", url, headers={}, body=None)
        except SourceError:
            return None
        return resp.body if resp.status == 200 else None

    # -- core --------------------------------------------------------------
    def _raw(self, method: str, url: str, *, headers: dict[str, str], body: bytes | None) -> Response:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in self.allowed_schemes:
            raise SourceError(f"refusing scheme {scheme!r} in {redact(url)}")
        merged = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip", **headers}
        self.limiter.wait(host_of(url))
        return self.transport.request(
            method, url, headers=merged, body=body, timeout=self.timeout
        )

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        tier: AccessTier = AccessTier.OFFICIAL_API,
    ) -> Response:
        if not tier.may_fetch:
            raise SourceError(f"tier {tier.value} may not fetch")
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(clean)}"

        host = host_of(url)
        if host in self.blocked:
            raise Challenge(host, 0, self.blocked[host])

        if tier.checks_robots:
            if not self.robots.allows(url):
                raise SourceError(f"{host}: robots.txt disallows {urllib.parse.urlsplit(url).path}")
            delay = self.robots.crawl_delay(url)
            if delay and delay > 60.0 / max(self.limiter.per_minute, 1):
                time.sleep(delay)

        sent_headers = dict(headers or {})
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            sent_headers.setdefault("Content-Type", "application/json")
            sent_headers.setdefault("Accept", "application/json")

        resp = self._raw(method, url, headers=sent_headers, body=body)

        challenged, hint = looks_like_challenge(resp.status, resp.body)
        if challenged:
            self.blocked[host] = hint
            raise Challenge(host, resp.status, hint)
        if resp.status >= 400:
            raise SourceError(f"{host}: HTTP {resp.status}")
        return resp
