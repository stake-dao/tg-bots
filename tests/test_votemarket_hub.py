import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from bots.votemarket.v2 import votemarket
from bots.votemarket_incentives import main as incentives
from shared.services import stakedao_api as api

GAUGE = "0x1111111111111111111111111111111111111111"
PLATFORM = "0x2222222222222222222222222222222222222222"


def reply(payload, status=200):
    def check():
        if status >= 400:
            raise requests.HTTPError(str(status))

    return SimpleNamespace(raise_for_status=check, json=lambda: payload)


@pytest.mark.parametrize("protocol", ["curve", "balancer", "fxn", "pendle", "yb"])
def test_shared_reader_uses_hub_and_keeps_full_catalog(monkeypatch, protocol):
    payload = {"chainId": 1, "gauges": [{"gauge": GAUGE, "isKilled": True}]}
    calls = []
    monkeypatch.setattr(api._session, "get", lambda url, **kw: calls.append((url, kw)) or reply(payload))
    assert api.fetch_gauges_data(protocol) == payload
    assert calls == [(f"https://hub.stakedao.org/v1/votemarket/{protocol}/gauges", {"timeout": 30})]


@pytest.mark.parametrize("payload", [None, [], {}, {"gauges": None}, {"gauges": [None]}, {"gauges": [{}]}])
def test_malformed_catalog_is_rejected(monkeypatch, payload):
    monkeypatch.setattr(api._session, "get", lambda *a, **kw: reply(payload))
    with pytest.raises(ValueError, match="gauges"):
        api.fetch_gauges_data("curve")


def test_retired_market_can_return_an_empty_catalog(monkeypatch):
    monkeypatch.setattr(api._session, "get", lambda *a, **kw: reply({"gauges": []}))
    assert api.fetch_gauges_data("pendle") == {"gauges": []}


def test_campaign_bot_uses_shared_reader_and_preserves_chain_fallback(monkeypatch):
    calls = []
    payload = {"chainId": 42161, "gauges": [{"gauge": GAUGE, "name": "Pool"}]}
    monkeypatch.setattr(api._session, "get", lambda url, **kw: calls.append(url) or reply(payload))
    monkeypatch.setattr(votemarket, "gauges_endpoint_data_cache", {})
    for _ in range(2):
        assert asyncio.run(votemarket.get_gauge_info(PLATFORM, {PLATFORM: "curve"}, GAUGE)) == (
            "Pool", 42161,
        )
    assert calls == [f"{api.DEFAULT_BASE_URL}/curve/gauges"]


def test_incentives_bot_reads_active_protocols_through_shared_reader(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        protocol = url.split("/")[-2]
        return reply({"gauges": [{"gauge": protocol, "name": protocol}]})

    monkeypatch.setattr(api._session, "get", get)
    assert incentives.fetch_gauge_data() == {
        protocol: {"name": protocol, "protocol": protocol} for protocol in incentives.ACTIVE_PROTOCOLS
    }
    assert calls == [f"{api.DEFAULT_BASE_URL}/{protocol}/gauges" for protocol in incentives.ACTIVE_PROTOCOLS]


def test_failed_protocol_never_caches_a_partial_catalog(monkeypatch):
    redis = MagicMock()
    redis.get.return_value = None
    monkeypatch.setattr(incentives, "get_redis_client", lambda: redis)

    def get(url, **kwargs):
        return reply({"gauges": [{"gauge": GAUGE, "name": "Pool"}]}, 503 if "/fxn/" in url else 200)

    monkeypatch.setattr(api._session, "get", get)
    with pytest.raises(requests.HTTPError):
        incentives.load_gauge_data()
    redis.setex.assert_not_called()


def test_existing_notification_checkpoints_are_still_read(monkeypatch):
    redis = MagicMock()
    redis.get.return_value = "[123, 456]"
    monkeypatch.setattr(incentives, "get_redis_client", lambda: redis)
    assert incentives.load_seen_ids() == {123, 456}
    redis.get.assert_called_once_with("tg-bot:votemarket-incentives:seen_ids")
