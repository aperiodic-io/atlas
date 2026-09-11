import pytest
import requests

from integrations.slack_notify import (
    SLACK_POST_MESSAGE_URL,
    SlackError,
    SlackNotConfiguredError,
    post_message,
)


class FakeResponse:
    def __init__(self, payload: object = None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.requests: list[dict] = []

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return self._response


def test_post_message_uses_a_bot_token_and_channel_id():
    session = FakeSession(FakeResponse({"ok": True}))

    transport = post_message(
        session, "a new PR is open", bot_token="xoxb-1", channel_id="C123"
    )

    assert transport == "chat.postMessage"
    request = session.requests[0]
    assert request["url"] == SLACK_POST_MESSAGE_URL
    assert request["headers"]["Authorization"] == "Bearer xoxb-1"
    assert request["json"] == {"channel": "C123", "text": "a new PR is open"}


def test_post_message_falls_back_to_an_incoming_webhook():
    session = FakeSession(FakeResponse())

    transport = post_message(
        session, "a new PR is open", webhook_url="https://hooks.example/abc"
    )

    assert transport == "webhook"
    assert session.requests[0]["url"] == "https://hooks.example/abc"
    assert session.requests[0]["json"] == {"text": "a new PR is open"}


def test_post_message_prefers_the_channel_id_over_a_webhook():
    session = FakeSession(FakeResponse({"ok": True}))

    post_message(
        session,
        "hello",
        bot_token="xoxb-1",
        channel_id="C123",
        webhook_url="https://hooks.example/abc",
    )

    assert session.requests[0]["url"] == SLACK_POST_MESSAGE_URL


def test_post_message_raises_when_slack_refuses_a_two_hundred_response():
    session = FakeSession(FakeResponse({"ok": False, "error": "channel_not_found"}))

    with pytest.raises(SlackError, match="channel_not_found"):
        post_message(session, "hello", bot_token="xoxb-1", channel_id="C404")


def test_post_message_propagates_a_transport_error():
    session = FakeSession(FakeResponse(error=requests.HTTPError("500")))

    with pytest.raises(requests.HTTPError):
        post_message(session, "hello", webhook_url="https://hooks.example/abc")


def test_post_message_without_a_transport_is_reported_as_unconfigured():
    with pytest.raises(SlackNotConfiguredError):
        post_message(FakeSession(FakeResponse()), "hello")


def test_post_message_rejects_an_empty_message():
    with pytest.raises(ValueError, match="empty Slack message"):
        post_message(FakeSession(FakeResponse()), "   ", webhook_url="https://hooks.example")


def test_post_message_truncates_an_oversized_message():
    session = FakeSession(FakeResponse())

    post_message(session, "x" * 60000, webhook_url="https://hooks.example/abc")

    assert session.requests[0]["json"]["text"].endswith("…(truncated)")
    assert len(session.requests[0]["json"]["text"].encode("utf-8")) < 60000
