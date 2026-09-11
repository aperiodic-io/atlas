"""Minimal OpenAI-compatible chat client for CI-side matching helpers.

The default endpoint is GitHub Models, which every GitHub Actions run can reach
for free with the workflow's own ``GITHUB_TOKEN`` and a ``models: read``
permission. Any OpenAI-compatible provider works instead -- point
``ATLAS_LLM_BASE_URL``, ``ATLAS_LLM_MODEL`` and ``ATLAS_LLM_API_KEY`` at
OpenRouter's free models, Groq, a Gemini compatibility endpoint, or a local
Ollama server.

The client is advisory only: callers must treat a completion as candidate
evidence that still needs deterministic corroboration or human review.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

from integrations.http_retry import retry_delay_seconds


# GitHub Models used to be the default, reached free inside Actions with the
# workflow's own GITHUB_TOKEN. It now answers every request with 410 Gone and
# "github_models_retirement_brownout": the service is being retired, so no URL,
# model or header would bring it back. opencode Zen is OpenAI-compatible and its
# free tier needs no account, which is what this job actually wants.
DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
# Zen's free tier rotates, so this id will eventually be retired too. A failure
# then prints the ids the endpoint advertises, which is the whole fix.
DEFAULT_MODEL = "mimo-v2-pro-free"
# Zen serves its free models against this literal bearer token, so the job needs
# no secret at all to adjudicate. It is a public constant, not a credential.
ZEN_PUBLIC_TOKEN = "public"
# Hosts that may receive GITHUB_TOKEN as the bearer token. The fallback to it
# exists only because GitHub's own inference endpoint authenticates that way;
# sending a repository token to anyone else would hand a third party write
# access to the repository.
GITHUB_TOKEN_HOSTS = frozenset({"models.github.ai", "api.github.com", "github.com"})
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_MIN_INTERVAL_SECONDS = 1.0
MAX_RETRY_DELAY_SECONDS = 60
# Retrying a permanent rejection wastes the whole run: a misconfigured endpoint
# answered 410 Gone four times for each of 38 tickers once, burning nine minutes
# and reporting itself as 38 separate per-ticker outages. Only these are worth a
# second attempt; every other 4xx is a configuration error.
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class JsonResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class PostSession(Protocol):
    def post(self, url: str, **kwargs: Any) -> JsonResponse: ...


class LlmError(RuntimeError):
    """The LLM endpoint did not return a usable JSON completion."""


class LlmNotConfiguredError(LlmError):
    """No API key is available, so no completion can be requested."""


class LlmConfigurationError(LlmError):
    """The endpoint rejected the request in a way retrying cannot fix.

    A wrong URL, a retired API version, a bad key or a missing scope are all
    permanent for the lifetime of a run, so the caller should stop asking rather
    than fail once per symbol.
    """


@dataclass(frozen=True)
class LlmConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    api_version: str = ""

    def __post_init__(self) -> None:
        if not self.api_key:
            raise LlmNotConfiguredError("no LLM API key was provided")
        if not self.base_url or not self.model:
            raise ValueError("LLM base URL and model must be set")
        if self.timeout_seconds <= 0 or self.max_attempts <= 0:
            raise ValueError("LLM timeout and attempt budget must be positive")
        if self.min_interval_seconds < 0:
            raise ValueError("LLM min_interval_seconds must not be negative")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LlmConfig:
        """Build a config from the environment, defaulting to opencode Zen.

        Raises ``LlmNotConfiguredError`` when no usable token can be resolved, so
        callers can degrade to deterministic matching instead of failing a
        scheduled run.
        """
        environment = os.environ if env is None else env
        base_url = (
            environment.get("ATLAS_LLM_BASE_URL") or DEFAULT_BASE_URL
        ).strip()
        return cls(
            api_key=_resolve_api_key(environment, base_url),
            base_url=base_url,
            model=(environment.get("ATLAS_LLM_MODEL") or DEFAULT_MODEL).strip(),
            timeout_seconds=_int_from_env(
                environment, "ATLAS_LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
            ),
            max_attempts=_int_from_env(
                environment, "ATLAS_LLM_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS
            ),
            min_interval_seconds=_float_from_env(
                environment,
                "ATLAS_LLM_MIN_INTERVAL_SECONDS",
                DEFAULT_MIN_INTERVAL_SECONDS,
            ),
            # Kept for any GitHub-hosted endpoint that wants a version header;
            # every other provider ignores it.
            api_version=(environment.get("ATLAS_LLM_API_VERSION") or "").strip(),
        )

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "atlas-llm/1.0",
        }
        if self.api_version:
            headers["X-GitHub-Api-Version"] = self.api_version
        return headers


class ChatClient:
    """Request JSON object completions from an OpenAI-compatible endpoint."""

    def __init__(self, config: LlmConfig, session: PostSession) -> None:
        self.config = config
        self._session = session
        self._next_request_at = 0.0
        self.calls = 0

    def check(self) -> None:
        """Validate the endpoint with one cheap request before the real work.

        Raises ``LlmConfigurationError`` when the endpoint is permanently
        unusable, so a run can say so once instead of discovering it per symbol.
        """
        self.complete_json(
            "Reply with JSON only.",
            'Reply with exactly {"ok": true} and nothing else.',
        )

    def available_models(self) -> list[str]:
        """Return the model ids the endpoint advertises, or [] if it will not say.

        Providers with a rotating free tier retire model ids without notice, which
        reaches a scheduled run as an unexplained rejection of a name that worked
        yesterday. Listing what is actually on offer turns that into a one-line
        fix. The listing endpoint is optional in practice -- opencode Zen was
        asked to add one and may not serve it -- so every failure here is silent:
        this is a diagnostic aid, never a precondition for a completion.
        """
        get = getattr(self._session, "get", None)
        if not callable(get):
            return []
        try:
            response = get(
                self.config.models_url,
                timeout=self.config.timeout_seconds,
                headers=self.config.headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return sorted(
            str(entry["id"])
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        )

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Return one parsed JSON object, retrying transient endpoint failures."""
        body = {
            "model": self.config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        last_error: Exception | None = None
        for attempt in range(self.config.max_attempts):
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                response = self._session.post(
                    self.config.chat_completions_url,
                    json=body,
                    timeout=self.config.timeout_seconds,
                    headers=self.config.headers,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                status = _status_code(error)
                if status is not None and status not in RETRYABLE_STATUS_CODES:
                    raise LlmConfigurationError(
                        f"{self.config.model} at {self.config.chat_completions_url} "
                        f"rejected the request with HTTP {status}; retrying cannot "
                        f"fix this: {error}{_response_detail(error)}"
                    ) from error
                last_error = error
                self._next_request_at = time.monotonic() + retry_delay_seconds(
                    error, attempt, MAX_RETRY_DELAY_SECONDS
                )
                continue
            self.calls += 1
            self._next_request_at = time.monotonic() + self.config.min_interval_seconds
            try:
                return _parse_json_object(_message_content(payload))
            except LlmError as error:
                last_error = error
                continue
        raise LlmError(
            f"{self.config.model} returned no usable JSON after "
            f"{self.config.max_attempts} attempts: {last_error}"
        ) from last_error


def _resolve_api_key(env: Mapping[str, str], base_url: str) -> str:
    """Return the bearer token for ``base_url``, never leaking one across hosts.

    ``GITHUB_TOKEN`` is a repository credential with write access, and the
    workflow exports it alongside the provider settings. Falling back to it for
    whatever endpoint happens to be configured would send it to a third party the
    moment someone sets ``ATLAS_LLM_BASE_URL`` and forgets the key -- two
    separate settings, so exactly the mistake a person makes. It is therefore
    offered only to GitHub's own hosts.
    """
    configured = (env.get("ATLAS_LLM_API_KEY") or "").strip()
    if configured:
        return configured
    host = (urlparse(base_url).hostname or "").lower()
    if host in GITHUB_TOKEN_HOSTS:
        return (env.get("GITHUB_TOKEN") or "").strip()
    if host == "opencode.ai":
        # Free Zen models answer to this public token, so the common case needs
        # no secret. Anything paid rejects it and says so.
        return ZEN_PUBLIC_TOKEN
    return ""


def _status_code(error: Exception) -> int | None:
    """Return the HTTP status behind a request failure, if it carries one."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _response_detail(error: Exception, limit: int = 400) -> str:
    """Return what the endpoint said, which is where the actual reason lives.

    An HTTP status alone leaves a misconfiguration ambiguous -- a 410 could be a
    retired path, a retired model, or a token the endpoint will not serve. The
    body usually says which, so discarding it turns a one-line fix into guesswork.
    """
    response = getattr(error, "response", None)
    body = getattr(response, "text", None)
    if not isinstance(body, str) or not body.strip():
        return ""
    collapsed = " ".join(body.split())
    if len(collapsed) > limit:
        collapsed = f"{collapsed[:limit]}…"
    return f" -- endpoint said: {collapsed}"


def _message_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise LlmError("completion response is not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmError("completion response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        # Some providers return content parts instead of a single string.
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str) or not content.strip():
        raise LlmError("completion response has no message content")
    return content


def _parse_json_object(content: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating a fenced or prose-wrapped completion."""
    for text in _json_candidates(content):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise LlmError("completion content is not a JSON object")


def _json_candidates(content: str) -> list[str]:
    stripped = content.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        fenced = stripped.split("```")
        candidates.extend(
            block.split("\n", 1)[1] if block.lower().startswith("json") else block
            for block in fenced
            if block.strip()
        )
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    return candidates


def _int_from_env(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(env[key]).strip())
    except (KeyError, TypeError, ValueError):
        return default


def _float_from_env(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(str(env[key]).strip())
    except (KeyError, TypeError, ValueError):
        return default
