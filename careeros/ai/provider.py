"""LLM provider abstraction.

Two implementations ship:

* `AnthropicProvider` -- Claude (`claude-opus-5`), used when credentials are
  present. Structured calls go through `client.messages.parse()` so responses
  are schema-validated; free-text calls go through the beta endpoint with
  server-side refusal fallbacks enabled.
* `HeuristicProvider` -- returns `None` from every structured call and
  templated strings from text calls.

Every engine treats a `None` structured result as "the deterministic path
stands". That is what lets the whole system -- ingest, classify, match, score,
rank -- run offline and be unit-tested without a key, while getting sharper
when a key is configured.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16000


class LLMUnavailable(RuntimeError):
    """Raised only where a caller explicitly requires the model."""


class LLMProvider(Protocol):
    name: str
    available: bool

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[TModel],
        cacheable_context: str | None = None,
        effort: str = "medium",
    ) -> TModel | None:
        """Return a validated object, or None when no model is configured."""

    def text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 2000,
        effort: str = "medium",
    ) -> str | None:
        """Return generated prose, or None when no model is configured."""


class HeuristicProvider:
    """No-model provider. Keeps the product fully functional without an API key."""

    name = "heuristic"
    available = False
    client = None

    def structured(self, **_kwargs: Any) -> None:
        return None

    def text(self, **_kwargs: Any) -> None:
        return None


class AnthropicProvider:
    """Claude-backed provider.

    Notes on the call shapes used here:

    * Adaptive thinking (`{"type": "adaptive"}`) is on for text generation;
      depth is steered with `output_config.effort` rather than a token budget.
    * `stop_reason == "refusal"` is checked before reading content, and
      server-side fallbacks are enabled on the text path so a declined request
      is rerouted instead of failing the daily run.
    * The stable half of each prompt (the taxonomy, the user's evidence set) is
      passed as `cacheable_context` and marked with `cache_control`, since the
      pipeline re-sends it for every job in a batch.
    """

    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.getenv("CAREEROS_LLM_MODEL", DEFAULT_MODEL)
        self._client: Any = None
        self.available = False
        try:
            import anthropic  # noqa: PLC0415 - optional dependency
        except ImportError:
            logger.info("anthropic SDK not installed; falling back to heuristics")
            return
        key = api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
        try:
            # A bare constructor also resolves an `ant auth login` profile, so
            # an unset ANTHROPIC_API_KEY does not by itself mean "no creds".
            self._client = anthropic.Anthropic() if not key else anthropic.Anthropic(api_key=key)
            self.available = self._has_credentials(self._client)
            if not self.available:
                logger.info(
                    "No Anthropic credentials resolved (set ANTHROPIC_API_KEY or run "
                    "`ant auth login`); using heuristics"
                )
        except Exception as exc:  # pragma: no cover - depends on local creds
            logger.info("Anthropic client unavailable (%s); falling back to heuristics", exc)

    @property
    def client(self) -> Any:
        """The raw SDK client.

        Needed by the agent loop, which drives tool use directly rather than
        through the `structured`/`text` primitives. Returns None when no
        credentials resolved -- there is no heuristic substitute for an
        open-ended reasoning loop, and callers must say so rather than
        silently degrading.
        """
        return self._client if self.available else None

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _has_credentials(client: Any) -> bool:
        """Whether the SDK actually resolved a credential.

        The constructor succeeds with no credentials and only fails at call
        time, which would mean one failed request (and one warning) per job in
        the daily run. Checking up front keeps the heuristic path silent.
        """
        if getattr(client, "api_key", None) or getattr(client, "auth_token", None):
            return True
        for attr in ("credentials", "custom_auth"):
            if getattr(client, attr, None):
                return True
        try:
            return bool(client.auth_headers)
        except Exception:  # noqa: BLE001 - absence of creds is the answer
            return False

    def _disable_on_auth_failure(self, exc: Exception) -> None:
        """Stop retrying once it is clear no credential will ever work.

        Without this, a misconfigured key costs one failed request per job,
        per resume and per email in every run.
        """
        text = str(exc).lower()
        if any(
            marker in text
            for marker in ("authentication", "api_key", "auth_token", "401", "credential")
        ):
            logger.warning("Disabling the Anthropic provider for this run: %s", exc)
            self.available = False

    def _system_blocks(self, system: str, cacheable_context: str | None) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cacheable_context:
            # Stable content last in the system array, with the breakpoint on
            # it: the volatile job text lives in `messages` after the prefix.
            blocks.append(
                {
                    "type": "text",
                    "text": cacheable_context,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        return blocks

    @staticmethod
    def _refused(response: Any) -> bool:
        if getattr(response, "stop_reason", None) != "refusal":
            return False
        details = getattr(response, "stop_details", None)
        logger.warning("Model declined request (category=%s)", getattr(details, "category", None))
        return True

    # -- API ---------------------------------------------------------------
    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[TModel],
        cacheable_context: str | None = None,
        effort: str = "medium",
    ) -> TModel | None:
        if not self.available:
            return None
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=self._system_blocks(system, cacheable_context),
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
            )
            if self._refused(response):
                return None
            return response.parsed_output
        except Exception as exc:  # noqa: BLE001 - never let the pipeline die
            logger.warning("structured call failed (%s: %s); using heuristics", type(exc).__name__, exc)
            self._disable_on_auth_failure(exc)
            return None

    def text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 2000,
        effort: str = "medium",
    ) -> str | None:
        if not self.available:
            return None
        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=self._system_blocks(system, None),
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if self._refused(response):
                return None
            return "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("text call failed (%s: %s); using templates", type(exc).__name__, exc)
            self._disable_on_auth_failure(exc)
            return None


_provider: LLMProvider | None = None


def get_provider(force: str | None = None) -> LLMProvider:
    """Resolve the configured provider.

    `CAREEROS_LLM=off` pins the heuristic path -- used by the test suite so
    results stay deterministic even on a machine that has credentials.
    """
    global _provider
    choice = (force or os.getenv("CAREEROS_LLM", "auto")).lower()
    if force is not None or _provider is None:
        if choice in {"off", "none", "heuristic"}:
            provider: LLMProvider = HeuristicProvider()
        else:
            candidate = AnthropicProvider()
            provider = candidate if candidate.available else HeuristicProvider()
        if force is None:
            _provider = provider
        return provider
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None
