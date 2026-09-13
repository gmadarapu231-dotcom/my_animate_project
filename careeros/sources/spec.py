"""Provider specs, loaded from `config/sources/providers.yaml`.

A spec carries everything except the knowledge of how to speak a given shape
of endpoint, which is what `kind` selects. Credentials are read from the
environment at fetch time and never stored on the spec, so a spec is safe to
serialise into an API response or the agent's audit trail.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from careeros.sources.policy import AccessTier

SOURCES_DIR = Path(__file__).resolve().parents[1] / "config" / "sources"
PROVIDERS_FILE = "providers.yaml"


@dataclass(frozen=True)
class CredentialSlot:
    """One value a provider needs, and where it comes from."""

    name: str
    env: str
    optional: bool = False
    prefix: str = ""
    split: str | None = None

    def read(self) -> str | list[str] | None:
        raw = os.getenv(self.env, "").strip()
        if not raw:
            return None
        if self.split:
            return [part.strip() for part in raw.split(self.split) if part.strip()]
        return f"{self.prefix}{raw}" if self.prefix else raw


@dataclass
class ProviderSpec:
    id: str
    label: str
    kind: str
    access: AccessTier
    countries: tuple[str, ...] = ()
    requires_key: bool = False
    auth_style: str = "none"
    credentials: tuple[CredentialSlot, ...] = ()
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    mapping: dict[str, Any] = field(default_factory=dict)
    resolve: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    docs: str = ""
    reason: str = ""
    use_instead: tuple[str, ...] = ()
    partner_route: str = ""
    verified: bool = True
    defaults: dict[str, Any] = field(default_factory=dict)

    # -- credentials -------------------------------------------------------
    @property
    def required_slots(self) -> list[CredentialSlot]:
        return [slot for slot in self.credentials if not slot.optional]

    def missing(self) -> list[str]:
        """Environment variables this provider needs and does not have."""
        out = [slot.env for slot in self.required_slots if slot.read() in (None, [])]
        feeds = self.request.get("feeds")
        if isinstance(feeds, CredentialSlot) and feeds.read() in (None, []):
            out.append(feeds.env)
        tokens = self.request.get("tokens")
        if isinstance(tokens, CredentialSlot) and tokens.read() in (None, []):
            out.append(tokens.env)
        return out

    @property
    def available(self) -> bool:
        return self.access.may_fetch and not self.missing()

    def covers(self, country: str | None) -> bool:
        return not country or not self.countries or country.upper() in self.countries

    # -- presentation ------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Safe to hand to the API, the dashboard and the agent: no secrets."""
        missing = self.missing()
        if not self.access.may_fetch:
            state = "not_permitted"
        elif missing:
            state = "needs_credentials"
        else:
            state = "ready"
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "access": self.access.value,
            "access_label": self.access.label,
            "state": state,
            "countries": list(self.countries) or ["any"],
            "requires_key": self.requires_key,
            "missing_env": missing,
            "notes": (self.notes or "").strip(),
            "docs": self.docs,
            "reason": (self.reason or "").strip(),
            "use_instead": list(self.use_instead),
            "partner_route": self.partner_route,
            "endpoint_verified": self.verified,
        }


def _slot(name: str, raw: Any) -> CredentialSlot:
    if isinstance(raw, dict):
        return CredentialSlot(
            name=name,
            env=raw["env"],
            optional=bool(raw.get("optional", False)),
            prefix=raw.get("prefix", ""),
            split=raw.get("split"),
        )
    return CredentialSlot(name=name, env=str(raw))


def _parse(entry: dict[str, Any], defaults: dict[str, Any]) -> ProviderSpec:
    auth = entry.get("auth") or {}
    credentials = tuple(_slot(name, raw) for name, raw in (auth.get("params") or {}).items())

    request = dict(entry.get("request") or {})
    # `tokens` and `feeds` are lists of targets supplied by the user, not keys,
    # but they arrive the same way.
    for key in ("tokens", "feeds"):
        if isinstance(request.get(key), dict) and "env" in request[key]:
            request[key] = _slot(key, request[key])

    return ProviderSpec(
        id=entry["id"],
        label=entry.get("label", entry["id"]),
        kind=entry["kind"],
        access=AccessTier(entry.get("access", "official_api")),
        countries=tuple(c.upper() for c in (entry.get("countries") or ())),
        requires_key=bool(entry.get("requires_key", False)),
        auth_style=auth.get("style", "none"),
        credentials=credentials,
        request=request,
        response=dict(entry.get("response") or {}),
        mapping=dict(entry.get("map") or {}),
        resolve=dict(entry.get("resolve") or {}),
        notes=entry.get("notes", "") or "",
        docs=entry.get("docs", "") or "",
        reason=entry.get("reason", "") or "",
        use_instead=tuple(entry.get("use_instead") or ()),
        partner_route=entry.get("partner_route", "") or "",
        verified=bool(entry.get("verified", True)),
        defaults=defaults,
    )


def load_specs(directory: str | Path | None = None) -> list[ProviderSpec]:
    base = Path(directory or os.getenv("CAREEROS_SOURCES_DIR", SOURCES_DIR))
    path = base / PROVIDERS_FILE
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    defaults = dict(doc.get("defaults") or {})
    specs = [_parse(entry, defaults) for entry in doc.get("providers") or []]
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise ValueError(f"duplicate provider id {spec.id!r} in {path}")
        seen.add(spec.id)
    return specs
