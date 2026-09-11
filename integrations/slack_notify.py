"""Post a short CI notification to Slack.

Two transports are supported so a workflow can be wired up either way:

- a bot token plus a channel ID (``SLACK_BOT_TOKEN`` and ``SLACK_CHANNEL_ID``),
  which uses ``chat.postMessage``; or
- an incoming webhook URL (``SLACK_WEBHOOK_URL``), as the daily update already
  uses for failure alerts.

When neither is configured the CLI says so and exits successfully, so a
scheduled job keeps working before the secrets are in place.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Protocol

import requests


SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
REQUEST_TIMEOUT_SECONDS = 20
MAX_TEXT_BYTES = 39000


class JsonResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class PostSession(Protocol):
    def post(self, url: str, **kwargs: Any) -> JsonResponse: ...


class SlackError(RuntimeError):
    """Slack rejected the message."""


class SlackNotConfiguredError(SlackError):
    """No Slack transport is configured."""


def post_message(
    session: PostSession,
    text: str,
    bot_token: str | None = None,
    channel_id: str | None = None,
    webhook_url: str | None = None,
) -> str:
    """Post ``text`` to Slack and return the transport that delivered it."""
    if not text.strip():
        raise ValueError("refusing to post an empty Slack message")
    text = _truncate(text)
    if bot_token and channel_id:
        response = session.post(
            SLACK_POST_MESSAGE_URL,
            json={"channel": channel_id, "text": text},
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        response.raise_for_status()
        payload = response.json()
        # chat.postMessage answers 200 even when it refuses the message.
        if not isinstance(payload, dict) or not payload.get("ok"):
            error = payload.get("error") if isinstance(payload, dict) else payload
            raise SlackError(f"chat.postMessage failed: {error}")
        return "chat.postMessage"
    if webhook_url:
        response = session.post(
            webhook_url,
            json={"text": text},
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        response.raise_for_status()
        return "webhook"
    raise SlackNotConfiguredError(
        "set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID, or SLACK_WEBHOOK_URL"
    )


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return text
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore") + "\n…(truncated)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="message to post")
    group.add_argument("--text-file", type=Path, help="file holding the message")
    args = parser.parse_args()

    text = args.text if args.text is not None else args.text_file.read_text()
    try:
        with requests.Session() as session:
            transport = post_message(
                session,
                text,
                bot_token=os.environ.get("SLACK_BOT_TOKEN"),
                channel_id=os.environ.get("SLACK_CHANNEL_ID"),
                webhook_url=os.environ.get("SLACK_WEBHOOK_URL"),
            )
    except SlackNotConfiguredError as error:
        print(f"Slack notification skipped: {error}", file=sys.stderr)
        return 0
    except (SlackError, requests.RequestException, OSError, ValueError) as error:
        print(f"Slack notification failed: {error}", file=sys.stderr)
        return 1
    print(f"Slack notification sent via {transport}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
