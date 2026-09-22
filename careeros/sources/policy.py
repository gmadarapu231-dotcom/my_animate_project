"""What a connector is allowed to do, enforced rather than documented.

The user's constraint is explicit: do not bypass CAPTCHA, MFA, bot protection
or authentication controls, and follow applicable site terms. That turns into
three mechanisms here.

`AccessTier` ranks how a provider obtains data. A provider declared
`prohibited` cannot fetch at all -- it exists so the dashboard can explain why
a board is missing and name the route that carries it instead.

`RobotsPolicy` reads robots.txt before any `public_page` fetch and caches the
answer per host.

`Challenge` is raised when a response looks like bot protection (403, 429, a
CAPTCHA interstitial, a login redirect). The host is then disabled for the rest
of the run. There is deliberately no retry-with-different-headers path: that
would be the circumvention the constraint forbids.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit, urlunsplit


class AccessTier(str, Enum):
    OFFICIAL_API = "official_api"
    LICENSED_AGGREGATOR = "licensed_aggregator"
    PUBLIC_FEED = "public_feed"
    PUBLIC_PAGE = "public_page"
    PROHIBITED = "prohibited"

    @property
    def may_fetch(self) -> bool:
        return self is not AccessTier.PROHIBITED

    @property
    def checks_robots(self) -> bool:
        """Only free-web page fetches consult robots.txt.

        An API called with the user's own credentials is a sanctioned client,
        not a crawler, and robots.txt does not govern it.
        """
        return self is AccessTier.PUBLIC_PAGE

    @property
    def label(self) -> str:
        return {
            AccessTier.OFFICIAL_API: "official API",
            AccessTier.LICENSED_AGGREGATOR: "licensed aggregator",
            AccessTier.PUBLIC_FEED: "published feed",
            AccessTier.PUBLIC_PAGE: "public page (robots-checked)",
            AccessTier.PROHIBITED: "not permitted",
        }[self]


class SourceError(RuntimeError):
    """A provider could not produce results. Never fatal to a run."""


class MissingCredentials(SourceError):
    def __init__(self, provider: str, variables: list[str]) -> None:
        self.provider = provider
        self.variables = variables
        super().__init__(f"{provider}: set {', '.join(variables)}")


class NotPermitted(SourceError):
    """The provider is declared prohibited and refuses to fetch."""

    def __init__(self, provider: str, reason: str, use_instead: list[str]) -> None:
        self.provider = provider
        self.reason = reason.strip()
        self.use_instead = use_instead
        alt = f" Use instead: {', '.join(use_instead)}." if use_instead else ""
        super().__init__(f"{provider} is not fetched.{alt}")


class Challenge(SourceError):
    """The host answered with bot protection, a rate limit or a login wall.

    Raising is the whole response. Nothing here tries to look more like a
    browser, rotate anything, or solve anything.
    """

    def __init__(self, host: str, status: int, hint: str = "") -> None:
        self.host = host
        self.status = status
        super().__init__(f"{host} returned {status}{': ' + hint if hint else ''} - not retried")


#: Markers that mean "a human check", not "a posting". Most specific first, so
#: the reported hint names the actual control ("recaptcha", not "captcha") --
#: that is the difference between a message the user can act on and one they
#: cannot.
_CHALLENGE_MARKERS = (
    "recaptcha",
    "hcaptcha",
    "px-captcha",
    "captcha",
    "are you a robot",
    "unusual traffic",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "please enable javascript and cookies",
    "access to this page has been denied",
)


def looks_like_challenge(status: int, body: str) -> tuple[bool, str]:
    if status in (401, 403, 429, 503):
        return True, f"status {status}"
    lowered = body[:4000].lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in lowered:
            return True, marker
    return False, ""


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


@dataclass
class RateLimiter:
    """One shared clock per host. Simple, and enough to be a good citizen."""

    per_minute: int = 20
    _last: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _sleep: object = None  # injectable for tests

    def wait(self, host: str) -> float:
        """Block until the next request to `host` is due. Returns seconds slept."""
        if self.per_minute <= 0:
            return 0.0
        interval = 60.0 / self.per_minute
        with self._lock:
            now = time.monotonic()
            due = self._last.get(host, 0.0) + interval
            delay = max(0.0, due - now)
            self._last[host] = max(now, due)
        if delay:
            (self._sleep or time.sleep)(delay)  # type: ignore[operator]
        return delay


class RobotsPolicy:
    """robots.txt, fetched once per host and cached.

    A host that will not serve robots.txt is treated as *allowing* the fetch,
    which is the conventional reading; a host that serves one and disallows the
    path is not fetched.
    """

    def __init__(self, fetch_text=None, user_agent: str = "CareerOS") -> None:
        self._fetch_text = fetch_text
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _parser(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host in self._cache:
            return self._cache[host]
        robots_url = urlunsplit((parts.scheme or "https", parts.netloc, "/robots.txt", "", ""))
        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            text = self._fetch_text(robots_url) if self._fetch_text else None
            if text is not None:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(text.splitlines())
        except Exception:
            parser = None
        self._cache[host] = parser
        return parser

    def allows(self, url: str) -> bool:
        parser = self._parser(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def crawl_delay(self, url: str) -> float | None:
        parser = self._parser(url)
        if parser is None:
            return None
        try:
            value = parser.crawl_delay(self.user_agent)
        except Exception:
            return None
        return float(value) if value is not None else None


_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|app[_-]?key|app[_-]?id|token|secret|password|authorization|key)"
    r"(=|:\s*|\"\s*:\s*\")([^&\s\"]+)"
)


def redact(text: str) -> str:
    """Keys must not reach logs, error messages, or the agent's audit trail."""
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
