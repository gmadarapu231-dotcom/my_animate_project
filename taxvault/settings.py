"""One place that decides whether this process is safe to serve real clients.

The defaults in this system are deliberately friendly: SQLite in a home
directory, a key generated on first use, one-time codes handed back in the API
response. All three are right for someone running it on a laptop to see what it
does, and all three are catastrophic in production:

  * A **generated master key** on an ephemeral disk means the next container
    restart cannot decrypt a single stored SSN or bank account. Not "logged
    out" -- gone. There is no recovery, because the key was never anywhere
    else.
  * A **generated session secret** means two replicas sign tokens the other
    rejects, and every deploy signs everyone out.
  * **Development codes** in the response means anyone who knows an email
    address can sign in as that person.

So this module refuses. Set `TAXVAULT_ENV=production` and the process will not
start until the things that must be supplied from outside have been supplied
from outside. It is better to fail at boot, loudly, in a deploy log, than to
serve one request and find out in January.

Nothing here is clever. It is a list of ways to lose a client's data, each one
turned into a question asked before the first request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

#: Values that count as "yes" in an environment variable.
_TRUE = {"1", "true", "yes", "on"}

ENV_VAR = "TAXVAULT_ENV"
PRODUCTION = "production"
STAGING = "staging"
DEVELOPMENT = "development"


class ConfigurationError(RuntimeError):
    """The process is not configured safely for the environment it claims."""


def flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE


def environment() -> str:
    """development unless told otherwise. Nothing is assumed to be production."""
    value = os.getenv(ENV_VAR, "").strip().lower()
    if value in (PRODUCTION, "prod"):
        return PRODUCTION
    if value in (STAGING, "stage", "uat"):
        return STAGING
    return DEVELOPMENT


def is_production() -> bool:
    return environment() == PRODUCTION


def is_real_deployment() -> bool:
    """Production or staging: anywhere a real person might sign in.

    Staging is included on purpose. A UAT environment with real client data in
    it is production as far as that data is concerned, and UAT is exactly where
    people put a real W-2 to "just check something".
    """
    return environment() in (PRODUCTION, STAGING)


# ===========================================================================
# What must be supplied from outside
# ===========================================================================
@dataclass
class Check:
    """One thing that has to be true, and what to do when it is not."""

    name: str
    ok: bool
    detail: str
    remedy: str = ""
    severity: str = "error"      # error blocks the boot; warning does not

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "ok": self.ok, "severity": self.severity,
            "detail": self.detail, "remedy": self.remedy,
        }


@dataclass
class Readiness:
    environment: str
    checks: list[Check] = field(default_factory=list)

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "ready": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "errors": [c.name for c in self.errors],
            "warnings": [c.name for c in self.warnings],
        }


def _check(name: str, ok: bool, detail: str, remedy: str = "",
           severity: str = "error") -> Check:
    return Check(name=name, ok=ok, detail=detail, remedy=remedy, severity=severity)


def _relaxed(detail: str) -> str:
    """How a development run should describe a setting it does not need.

    Reporting "ok" beside a detail that says something is missing reads as a
    contradiction and teaches people to ignore the output. In development the
    setting genuinely is not required, so say that instead.
    """
    return f"{detail} Not required outside a real deployment."


def readiness() -> Readiness:
    """Everything that decides whether this process should take real traffic."""
    env = environment()
    real = is_real_deployment()
    checks: list[Check] = []

    # --- the master key -----------------------------------------------------
    master = os.getenv("TAXVAULT_MASTER_KEY", "").strip()
    checks.append(_check(
        "master_key",
        bool(master) or not real,
        "The field-encryption key is supplied from the environment." if master
        else ("No TAXVAULT_MASTER_KEY; a key is generated on local disk."
              if not real else
              "No TAXVAULT_MASTER_KEY. A key would be generated on local disk."),
        remedy=(
            "Generate one with `python -m taxvault.cli newkey`, put it in your "
            "secret manager, and set TAXVAULT_MASTER_KEY. If this container's "
            "disk is replaced, a generated key is lost and every stored SSN "
            "and bank account becomes permanently undecryptable."
        ),
    ))

    # --- the session secret -------------------------------------------------
    secret = os.getenv("TAXVAULT_SESSION_SECRET", "").strip()
    checks.append(_check(
        "session_secret",
        (bool(secret) and len(secret) >= 32) or not real,
        "The token-signing secret is supplied from the environment." if secret
        else ("No TAXVAULT_SESSION_SECRET; one is generated on local disk."
              if not real else
              "No TAXVAULT_SESSION_SECRET. A secret would be generated on local disk."),
        remedy=(
            "Set TAXVAULT_SESSION_SECRET to at least 32 random characters, the "
            "same value on every replica. Without it, replicas reject each "
            "other's tokens and every deploy signs all clients out."
        ),
    ))
    if secret and len(secret) < 32:
        checks[-1] = _check(
            "session_secret", False,
            f"TAXVAULT_SESSION_SECRET is only {len(secret)} characters.",
            remedy="Use at least 32 random characters.",
        )

    # --- the database -------------------------------------------------------
    url = os.getenv("TAXVAULT_DATABASE_URL", "").strip()
    sqlite = (not url) or url.startswith("sqlite")
    checks.append(_check(
        "database",
        not (real and sqlite),
        f"Database is {url.split('://', 1)[0] if url else 'sqlite (default)'}."
        + ("" if not sqlite else
           " Fine for a local run; not for client data." if not real else ""),
        remedy=(
            "Point TAXVAULT_DATABASE_URL at managed PostgreSQL. SQLite has no "
            "concurrent writer, no point-in-time restore and no replication, "
            "and a tax practice cannot lose a filing season to a corrupt file."
        ),
    ))

    # --- one-time codes -----------------------------------------------------
    dev_codes = flag("TAXVAULT_DEV_CODES")
    checks.append(_check(
        "development_codes",
        not (real and dev_codes),
        "Development codes are off." if not dev_codes else
        "TAXVAULT_DEV_CODES is on: sign-in codes come back in the API response.",
        remedy=(
            "Unset TAXVAULT_DEV_CODES. With it on, anyone who knows a client's "
            "email address can sign in as them."
        ),
    ))

    # --- delivery -----------------------------------------------------------
    from taxvault.auth import codes as code_delivery

    email_missing = code_delivery.email_settings_missing()
    checks.append(_check(
        "email_delivery",
        not email_missing or not real,
        "Email delivery is configured." if not email_missing
        else _relaxed("Email is not configured: " + ", ".join(email_missing))
        if not real else "Email is not configured: " + ", ".join(email_missing),
        remedy=(
            "Set TAXVAULT_SMTP_HOST and TAXVAULT_SMTP_FROM (plus user and "
            "password if your relay needs them). Without email, nobody can "
            "sign in at all."
        ),
    ))

    sms_missing = code_delivery.sms_settings_missing()
    checks.append(_check(
        "sms_delivery",
        not sms_missing or not real,
        "SMS delivery is configured." if not sms_missing
        else _relaxed("SMS is not configured: " + ", ".join(sms_missing))
        if not real else "SMS is not configured: " + ", ".join(sms_missing),
        remedy=(
            "Set TAXVAULT_SMS_ACCOUNT_SID, TAXVAULT_SMS_AUTH_TOKEN and "
            "TAXVAULT_SMS_FROM. Identity verification needs a verified mobile "
            "number, so without SMS no client can finish signing up."
        ),
    ))

    # --- transport ----------------------------------------------------------
    checks.append(_check(
        "https",
        flag("TAXVAULT_FORCE_HSTS") or not real,
        "HSTS is on." if flag("TAXVAULT_FORCE_HSTS")
        else _relaxed("TAXVAULT_FORCE_HSTS is not set.") if not real
        else "TAXVAULT_FORCE_HSTS is not set.",
        remedy=(
            "Terminate TLS in front of this process and set "
            "TAXVAULT_FORCE_HSTS=1 so the Strict-Transport-Security header is "
            "sent even though this process sees plain HTTP from the proxy."
        ),
        severity="warning",
    ))

    checks.append(_check(
        "trusted_proxy",
        bool(os.getenv("TAXVAULT_TRUSTED_PROXIES", "").strip()) or not real,
        "Trusted proxy hops are declared." if os.getenv("TAXVAULT_TRUSTED_PROXIES")
        else _relaxed("TAXVAULT_TRUSTED_PROXIES is not set; forwarded addresses "
                      "are ignored.") if not real
        else "TAXVAULT_TRUSTED_PROXIES is not set.",
        remedy=(
            "Set TAXVAULT_TRUSTED_PROXIES to your load balancer's address, or "
            "to `*` if it is the only way in. Until it is set, "
            "X-Forwarded-For is ignored and rate limits are counted against "
            "the proxy's own address -- which lumps every client together."
        ),
        severity="warning",
    ))

    # --- shared rate limiting ----------------------------------------------
    redis_url = os.getenv("TAXVAULT_REDIS_URL", "").strip()
    workers = int(os.getenv("WEB_CONCURRENCY", "1") or 1)
    checks.append(_check(
        "shared_rate_limit",
        bool(redis_url) or workers <= 1 or not real,
        "Rate limiting is shared." if redis_url
        else f"Rate limiting is per-process; WEB_CONCURRENCY is {workers}"
             + (", so one process counts everything." if workers <= 1
                else f", so the real limit is {workers}x the configured one."),
        remedy=(
            "Set TAXVAULT_REDIS_URL. With more than one worker and no shared "
            "store, each worker counts separately, so the real limit is the "
            "configured one multiplied by the worker count."
        ),
        severity="warning",
    ))

    # --- things no code can satisfy ----------------------------------------
    checks.append(_check(
        "efile_authorisation",
        flag("TAXVAULT_EFILE_ENABLED") is False,
        "E-filing is off, which is correct: this system is not an IRS "
        "transmitter and submits nothing.",
        remedy="",
        severity="warning" if flag("TAXVAULT_EFILE_ENABLED") else "error",
    ))
    if flag("TAXVAULT_EFILE_ENABLED"):
        checks[-1] = _check(
            "efile_authorisation", False,
            "TAXVAULT_EFILE_ENABLED is set but no transmitter is implemented.",
            remedy=(
                "This system prepares and estimates; it does not transmit. "
                "E-filing needs an EFIN from the IRS e-Services application, "
                "acceptance testing against the MeF system, and a signed "
                "Form 8879 per client. Unset this flag."
            ),
        )

    return Readiness(environment=env, checks=checks)


def enforce() -> Readiness:
    """Called at startup. Raises rather than serving a request misconfigured."""
    report = readiness()
    if report.ok:
        return report
    lines = [
        f"TaxVault refuses to start in {report.environment}: "
        f"{len(report.errors)} setting(s) would put client data at risk.",
        "",
    ]
    for check in report.errors:
        lines.append(f"  [{check.name}] {check.detail}")
        if check.remedy:
            lines.append(f"      -> {check.remedy}")
        lines.append("")
    lines.append(
        f"Set {ENV_VAR}=development to run locally with generated keys and a "
        "SQLite file. Never do that with a real client's W-2."
    )
    raise ConfigurationError("\n".join(lines))
