from types import SimpleNamespace

import pytest
import requests

from integrations.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ChatClient,
    LlmConfig,
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
