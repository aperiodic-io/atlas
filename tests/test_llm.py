from types import SimpleNamespace

import pytest
import requests

from integrations.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ChatClient,
    LlmConfig,
    LlmConfigurationError,
    LlmError,
    LlmNotConfiguredError,
)


class FakeResponse:
    def __init__(self, payload: object, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return self._responses.pop(0)


def _http_error(status: int, body: str | None = None) -> requests.HTTPError:
    error = requests.HTTPError(f"{status} Client Error")
    response = SimpleNamespace(status_code=status, headers={})
    if body is not None:
        response.text = body
    error.response = response
    return error


def _completion(content: str) -> FakeResponse:
    return FakeResponse({"choices": [{"message": {"content": content}}]})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("integrations.llm.time.sleep", lambda _seconds: None)


def test_config_from_env_defaults_to_github_models_with_the_workflow_token():
    config = LlmConfig.from_env({"GITHUB_TOKEN": "ghs_token"})

    assert config.api_key == "ghs_token"
    assert config.base_url == DEFAULT_BASE_URL
    assert config.model == DEFAULT_MODEL
    assert config.chat_completions_url == f"{DEFAULT_BASE_URL}/chat/completions"


def test_config_from_env_prefers_an_explicit_provider():
    config = LlmConfig.from_env(
        {
            "GITHUB_TOKEN": "ghs_token",
            "ATLAS_LLM_API_KEY": "sk-free",
            "ATLAS_LLM_BASE_URL": "https://openrouter.ai/api/v1/",
            "ATLAS_LLM_MODEL": "meta-llama/llama-3.3-70b-instruct:free",
            "ATLAS_LLM_MAX_ATTEMPTS": "2",
        }
    )

    assert config.api_key == "sk-free"
    assert config.model == "meta-llama/llama-3.3-70b-instruct:free"
    assert config.max_attempts == 2
    assert config.chat_completions_url == "https://openrouter.ai/api/v1/chat/completions"


def test_config_from_env_without_a_key_is_reported_as_unconfigured():
    with pytest.raises(LlmNotConfiguredError):
        LlmConfig.from_env({})


def test_complete_json_sends_a_bearer_token_and_returns_the_parsed_object():
    session = FakeSession([_completion('{"cmc_id": 1, "confidence": "high"}')])
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    answer = client.complete_json("system", "user")

    assert answer == {"cmc_id": 1, "confidence": "high"}
    assert client.calls == 1
    request = session.requests[0]
    assert request["headers"]["Authorization"] == "Bearer key"
    assert request["json"]["response_format"] == {"type": "json_object"}
    assert request["json"]["messages"][1] == {"role": "user", "content": "user"}


def test_complete_json_unwraps_a_fenced_completion():
    session = FakeSession([_completion('```json\n{"cmc_id": 2}\n```')])
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    assert client.complete_json("system", "user") == {"cmc_id": 2}


def test_complete_json_retries_a_rate_limited_request():
    error = requests.HTTPError("429")
    error.response = SimpleNamespace(headers={"Retry-After": "1"})
    session = FakeSession(
        [FakeResponse(None, error), _completion('{"cmc_id": 3}')]
    )
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    assert client.complete_json("system", "user") == {"cmc_id": 3}
    assert len(session.requests) == 2


def test_complete_json_gives_up_on_content_that_is_never_json():
    session = FakeSession([_completion("I cannot help"), _completion("nor now")])
    client = ChatClient(
        LlmConfig(api_key="key", max_attempts=2, min_interval_seconds=0), session
    )

    with pytest.raises(LlmError):
        client.complete_json("system", "user")


def test_complete_json_rejects_a_response_without_choices():
    session = FakeSession([FakeResponse({"error": "bad request"})])
    client = ChatClient(
        LlmConfig(api_key="key", max_attempts=1, min_interval_seconds=0), session
    )

    with pytest.raises(LlmError):
        client.complete_json("system", "user")


def test_complete_json_does_not_retry_a_permanently_rejected_request():
    """Regression: a dead endpoint answered 410 four times for each of 38 tickers.

    Retrying a permanent rejection cost nine minutes and reported one dead URL as
    38 unrelated per-symbol outages.
    """
    session = FakeSession([FakeResponse(None, _http_error(410))])
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    with pytest.raises(LlmConfigurationError, match="410"):
        client.complete_json("system", "user")

    assert len(session.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
def test_complete_json_treats_every_permanent_4xx_as_configuration(status):
    session = FakeSession([FakeResponse(None, _http_error(status))])
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    with pytest.raises(LlmConfigurationError):
        client.complete_json("system", "user")

    assert len(session.requests) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_complete_json_still_retries_a_transient_status(status):
    session = FakeSession(
        [FakeResponse(None, _http_error(status)), _completion('{"cmc_id": 1}')]
    )
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    assert client.complete_json("system", "user") == {"cmc_id": 1}
    assert len(session.requests) == 2


def test_check_validates_the_endpoint_with_one_request():
    session = FakeSession([_completion('{"ok": true}')])
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    client.check()

    assert len(session.requests) == 1


def test_check_surfaces_a_dead_endpoint_as_a_configuration_error():
    session = FakeSession([FakeResponse(None, _http_error(410))])
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    with pytest.raises(LlmConfigurationError):
        client.check()


def test_config_sends_the_github_api_version_when_one_is_configured():
    config = LlmConfig.from_env(
        {"GITHUB_TOKEN": "ghs", "ATLAS_LLM_API_VERSION": "2026-03-10"}
    )

    assert config.api_version == "2026-03-10"
    assert config.headers["X-GitHub-Api-Version"] == "2026-03-10"


def test_config_omits_the_api_version_header_when_unset():
    assert "X-GitHub-Api-Version" not in LlmConfig.from_env({"GITHUB_TOKEN": "g"}).headers


def test_a_permanent_rejection_quotes_what_the_endpoint_said():
    """Regression: a bare status code turned a one-line fix into days of guessing.

    GitHub Models answered 410 Gone and the client reported only the number, which
    cannot distinguish a retired path from a retired model from a token the
    endpoint will not serve. The body says which, so it belongs in the message.
    """
    body = '{"error":{"message":"unknown model: openai/gpt-4o-mini"}}'
    session = FakeSession([FakeResponse(None, _http_error(410, body))])
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    with pytest.raises(LlmConfigurationError) as caught:
        client.complete_json("system", "user")

    assert "unknown model: openai/gpt-4o-mini" in str(caught.value)


def test_a_quoted_response_body_is_collapsed_and_truncated():
    """A provider that answers with an HTML error page must not flood the log."""
    session = FakeSession([FakeResponse(None, _http_error(404, "<html>\n" + "x" * 900))])
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    with pytest.raises(LlmConfigurationError) as caught:
        client.complete_json("system", "user")

    message = str(caught.value)
    assert "\n" not in message
    assert len(message) < 700
    assert message.endswith("\u2026")


def test_a_rejection_without_a_body_reads_no_differently():
    """An endpoint that says nothing must not leave a dangling 'said:' clause."""
    session = FakeSession([FakeResponse(None, _http_error(403))])
    client = ChatClient(LlmConfig(api_key="key", min_interval_seconds=0), session)

    with pytest.raises(LlmConfigurationError) as caught:
        client.complete_json("system", "user")

    assert "endpoint said" not in str(caught.value)
