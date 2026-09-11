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
from typing import Any, Protocol

import requests

from integrations.http_retry import retry_delay_seconds


DEFAULT_BASE_URL = "https://models.github.ai/inference"
DEFAULT_MODEL = "openai/gpt-4o-mini"
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
        """Build a config from the environment, defaulting to GitHub Models.

        Raises ``LlmNotConfiguredError`` when neither ``ATLAS_LLM_API_KEY`` nor
        ``GITHUB_TOKEN`` is set, so callers can degrade to deterministic
        matching instead of failing a scheduled run.
        """
        environment = os.environ if env is None else env
        return cls(
            api_key=(
                environment.get("ATLAS_LLM_API_KEY")
                or environment.get("GITHUB_TOKEN")
                or ""
            ).strip(),
            base_url=(
                environment.get("ATLAS_LLM_BASE_URL") or DEFAULT_BASE_URL
            ).strip(),
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
            # GitHub Models answers 410 Gone to an unversioned request; other
            # providers ignore the header.
            api_version=(environment.get("ATLAS_LLM_API_VERSION") or "").strip(),
        )

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

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
