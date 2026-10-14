from unittest.mock import MagicMock

import pytest
import requests

from shared.communication import telegram


def response(status, retry_after=None, headers=None):
    reply = MagicMock()
    reply.status_code = status
    reply.headers = headers or {}
    if retry_after is not None:
        reply.json.return_value = {"ok": False, "parameters": {"retry_after": retry_after}}
    else:
        reply.json.return_value = {"ok": False, "description": "Too Many Requests"}
    if status >= 400:
        reply.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status} error", response=reply)
    return reply


def patch(monkeypatch, replies):
    posts, sleeps = [], []
    monkeypatch.setattr(telegram.requests, "post", lambda url, **kwargs: posts.append(kwargs["json"]) or replies.pop(0))
    monkeypatch.setattr(telegram.time, "sleep", sleeps.append)
    return posts, sleeps


def test_send_retries_after_429_honouring_retry_after(monkeypatch):
    posts, sleeps = patch(monkeypatch, [response(429, retry_after=2), response(200)])

    assert telegram.send_telegram_message("fixture-bot", "@chat", "hello") is True
    assert len(posts) == 2
    assert sleeps == [2, 1]


def test_send_gives_up_after_the_last_429(monkeypatch):
    posts, sleeps = patch(monkeypatch, [response(429, retry_after=3), response(429), response(429)])

    assert telegram.send_telegram_message("fixture-bot", "@chat", "hello") is False
    assert len(posts) == 3
    assert sleeps == [3, 1, 1]  # no retry_after in the body and no header: 1 s


def test_ban_longer_than_the_cap_fails_without_waiting(monkeypatch):
    posts, sleeps = patch(monkeypatch, [response(429, retry_after=telegram.MAX_RETRY_AFTER_SECONDS + 1)])

    assert telegram.send_telegram_message("fixture-bot", "@chat", "hello") is False
    assert len(posts) == 1
    assert sleeps == [1]


@pytest.mark.parametrize("header, delay", [("5", 5), ("Wed, 21 Oct 2015 07:28:00 GMT", 1)])
def test_retry_after_header_fallback(monkeypatch, header, delay):
    _, sleeps = patch(monkeypatch, [response(429, headers={"Retry-After": header}), response(200)])

    assert telegram.send_telegram_message("fixture-bot", "@chat", "hello") is True
    assert sleeps == [delay, 1]
