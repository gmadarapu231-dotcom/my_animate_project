"""How an application can actually be submitted, and whether that can be automated.

This mirrors the tiering in `careeros.sources.policy`, for the same reason: the
honest answer differs per destination, and pretending otherwise either fails
silently or does something the user did not agree to.

    API       the ATS publishes an application endpoint. Automatable.
    EMAIL     the posting names an address to apply to. Automatable.
    ASSISTED  a web form. The packet is assembled, the human submits.
    BLOCKED   needs the user's own login and defeating bot protection.

The last tier is the one that matters. "Easy Apply" on LinkedIn, Indeed's
on-site apply, and Naukri all require an authenticated session as the user and
sit behind bot protection. Driving those would mean holding the user's
credentials and defeating a control the site put there deliberately -- and it
reliably gets accounts banned, which costs the user far more than the time it
saves. So they are detected, reported, and routed to ASSISTED with the reason,
never attempted.

A tier is a *ceiling*, not a promise. A board in the API tier that turns out to
require a human-verification token downgrades itself to ASSISTED at submit
time rather than trying to get around it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlsplit


class SubmitTier(str, Enum):
    API = "api"
    EMAIL = "email"
    ASSISTED = "assisted"
    BLOCKED = "blocked"

    @property
    def automatable(self) -> bool:
        return self in (SubmitTier.API, SubmitTier.EMAIL)

    @property
    def label(self) -> str:
        return {
            SubmitTier.API: "ATS application API",
            SubmitTier.EMAIL: "apply by email",
            SubmitTier.ASSISTED: "web form — you submit",
            SubmitTier.BLOCKED: "login wall — cannot be automated",
        }[self]


@dataclass
class Channel:
    """The route for one posting, and what is known about it."""

    tier: SubmitTier
    provider: str
    apply_url: str | None = None
    email: str | None = None
    #: Identifiers the submitter needs, e.g. {"board": "acme", "job_id": "5550001"}
    parameters: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    #: False where the endpoint is documented but not exercised here.
    verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "tier_label": self.tier.label,
            "automatable": self.tier.automatable,
            "provider": self.provider,
            "apply_url": self.apply_url,
            "email": self.email,
            "parameters": self.parameters,
            "reason": self.reason,
            "endpoint_verified": self.verified,
        }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
#: Hosts that need the user's own session plus bot-protection bypass.
BLOCKED_HOSTS: dict[str, str] = {
    "linkedin.com": (
        "LinkedIn Easy Apply needs an authenticated session as you, and job "
        "pages sit behind bot protection. Automating it would mean holding your "
        "LinkedIn credentials and defeating that control — and it gets accounts "
        "restricted. Apply through the link instead; the packet is ready."
    ),
    "indeed.com": (
        "Indeed's on-site apply requires an Indeed account session and is "
        "Cloudflare-protected. The packet is assembled for you to submit."
    ),
    "naukri.com": (
        "Naukri requires a logged-in session and blocks automated posts. Apply "
        "through the link; the tailored résumé is ready to attach."
    ),
    "glassdoor.com": (
        "Glassdoor proxies to the employer behind a login wall."
    ),
    "ziprecruiter.com": (
        "ZipRecruiter's 1-Click Apply is tied to a ZipRecruiter account session."
    ),
    "dice.com": (
        "Dice apply requires a Dice account session."
    ),
    "monster.com": ("Monster apply requires a Monster account session."),
    "simplyhired.com": ("SimplyHired proxies to an account-gated apply flow."),
}

#: ATS platforms with a documented application endpoint.
#: (host fragment, provider id, whether this project has exercised the endpoint)
API_BOARDS: tuple[tuple[str, str, bool], ...] = (
    ("boards.greenhouse.io", "greenhouse", True),
    ("boards-api.greenhouse.io", "greenhouse", True),
    ("job-boards.greenhouse.io", "greenhouse", True),
    ("jobs.lever.co", "lever", True),
    ("api.lever.co", "lever", True),
    ("jobs.ashbyhq.com", "ashby", False),
    ("jobs.smartrecruiters.com", "smartrecruiters", False),
    ("apply.workable.com", "workable", False),
)

#: Platforms that are plain web forms: no public submission endpoint, but no
#: login wall either. The packet plus the link is genuinely useful here.
FORM_BOARDS: tuple[tuple[str, str], ...] = (
    ("myworkdayjobs.com", "workday"),
    ("myworkday.com", "workday"),
    ("taleo.net", "taleo"),
    ("icims.com", "icims"),
    ("successfactors.com", "successfactors"),
    ("oraclecloud.com", "oracle_recruiting"),
    ("brassring.com", "brassring"),
    ("jobvite.com", "jobvite"),
    ("bamboohr.com", "bamboohr"),
    ("recruitee.com", "recruitee"),
    ("teamtailor.com", "teamtailor"),
    ("personio.de", "personio"),
    ("pinpointhq.com", "pinpoint"),
)

_MAILTO = re.compile(r"mailto:([\w.+-]+@[\w-]+\.[\w.]+)", re.IGNORECASE)
_APPLY_EMAIL = re.compile(
    r"(?:send|email|forward|share|submit|mail)\b[^.\n]{0,80}?"
    r"(?:to|at|:)\s*[<(]?([\w.+-]+@[\w-]+\.[\w.]+)",
    re.IGNORECASE,
)
_ANY_EMAIL = re.compile(r"\b([\w.+-]+@[\w-]+\.[\w.]+)\b")
#: Addresses that are plainly not an application inbox.
_NON_APPLY = re.compile(
    r"(?i)^(?:no-?reply|donotreply|privacy|legal|press|media|support|help|info|"
    r"sales|marketing|webmaster|abuse|security)@"
)

_GREENHOUSE_URL = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)(?:/jobs/(\d+))?", re.IGNORECASE)
_LEVER_URL = re.compile(r"lever\.co/([\w-]+)/([\w-]+)", re.IGNORECASE)


def _host(url: str | None) -> str:
    if not url:
        return ""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _blocked_for(host: str) -> tuple[str, str] | None:
    for fragment, reason in BLOCKED_HOSTS.items():
        if host == fragment or host.endswith("." + fragment):
            return fragment, reason
    return None


def find_application_email(*texts: str | None) -> str | None:
    """An address the posting asks applications to be sent to.

    A `mailto:` link is unambiguous. "Send your CV to careers@…" is nearly so.
    A bare address anywhere in the description is not -- it is as likely to be
    a recruiter's signature or a privacy contact -- so it only counts when the
    local part says it is for applications.
    """
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return None

    for pattern in (_MAILTO, _APPLY_EMAIL):
        match = pattern.search(blob)
        if match and not _NON_APPLY.match(match.group(1)):
            return match.group(1).lower()

    for candidate in _ANY_EMAIL.findall(blob):
        local = candidate.split("@", 1)[0].lower()
        if _NON_APPLY.match(candidate):
            continue
        if local in ("careers", "career", "jobs", "job", "recruiting", "recruitment",
                     "hiring", "hr", "apply", "applications", "talent", "cv", "resume"):
            return candidate.lower()
    return None


def detect(
    *,
    url: str | None,
    description: str | None = None,
    source: str | None = None,
    company: str | None = None,
) -> Channel:
    """Work out how this posting can be applied to.

    Order is deliberate: a blocked host is blocked no matter what else the
    description says, because that is where the submission would actually have
    to happen.
    """
    host = _host(url)

    blocked = _blocked_for(host)
    if blocked:
        fragment, reason = blocked
        # An application email in the description is a real way round a board's
        # login wall, and a legitimate one -- the employer published it.
        email = find_application_email(description)
        if email:
            return Channel(
                tier=SubmitTier.EMAIL,
                provider=fragment,
                apply_url=url,
                email=email,
                reason=(
                    f"{fragment} cannot be automated, but the posting names "
                    f"{email} to apply to."
                ),
            )
        return Channel(tier=SubmitTier.BLOCKED, provider=fragment, apply_url=url, reason=reason)

    for fragment, provider, verified in API_BOARDS:
        if host == fragment or host.endswith("." + fragment):
            parameters = _board_parameters(provider, url or "")
            if not parameters:
                return Channel(
                    tier=SubmitTier.ASSISTED,
                    provider=provider,
                    apply_url=url,
                    reason=(
                        f"{provider} board recognised, but the posting id could not be read "
                        f"from the URL, so there is nothing to submit to."
                    ),
                )
            return Channel(
                tier=SubmitTier.API,
                provider=provider,
                apply_url=url,
                parameters=parameters,
                verified=verified,
                reason=f"{provider} publishes an application endpoint for this board.",
            )

    email = find_application_email(description)
    if email:
        return Channel(
            tier=SubmitTier.EMAIL,
            provider="email",
            apply_url=url,
            email=email,
            reason=f"The posting asks for applications at {email}.",
        )

    for fragment, provider in FORM_BOARDS:
        if host == fragment or host.endswith("." + fragment):
            return Channel(
                tier=SubmitTier.ASSISTED,
                provider=provider,
                apply_url=url,
                reason=(
                    f"{provider} is a web form with no public submission endpoint. "
                    "The packet is assembled; the form is yours to submit."
                ),
            )

    return Channel(
        tier=SubmitTier.ASSISTED,
        provider=host or (source or "unknown"),
        apply_url=url,
        reason=(
            "No application endpoint or address found for this posting, so it is "
            "assembled for you to submit."
        ),
    )


def _board_parameters(provider: str, url: str) -> dict[str, str]:
    if provider == "greenhouse":
        match = _GREENHOUSE_URL.search(url)
        if match and match.group(2):
            return {"board": match.group(1), "job_id": match.group(2)}
        # /embed/job_app?for=acme&token=5550001
        token = re.search(r"[?&]token=(\d+)", url)
        if match and token:
            return {"board": match.group(1), "job_id": token.group(1)}
        return {}
    if provider == "lever":
        match = _LEVER_URL.search(url)
        return {"site": match.group(1), "posting_id": match.group(2)} if match else {}
    if provider == "ashby":
        match = re.search(r"ashbyhq\.com/([\w-]+)/([\w-]+)", url, re.IGNORECASE)
        return {"board": match.group(1), "job_id": match.group(2)} if match else {}
    if provider == "smartrecruiters":
        match = re.search(r"smartrecruiters\.com/([\w-]+)/(\d+)", url, re.IGNORECASE)
        return {"company": match.group(1), "posting_id": match.group(2)} if match else {}
    if provider == "workable":
        match = re.search(r"workable\.com/(?:j/)?([\w-]+)(?:/j/([\w]+))?", url, re.IGNORECASE)
        if match and match.group(2):
            return {"account": match.group(1), "shortcode": match.group(2)}
        return {}
    return {}
