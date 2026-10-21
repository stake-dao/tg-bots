import asyncio
import httpx
import pytest

from shared.services.stakedao_api import (
    StakeDAOApiService,
    StakeDAOError,
    DataNotFoundError,
    CAPABILITIES,
)


def client(handler):
    service = StakeDAOApiService(
        base_url="https://hub.test/v1/votemarket",
        transport=httpx.MockTransport(handler),
    )
    service.initial_delay = 0
    return service


def test_gauge_identity_is_preserved_without_rewriting_root_child_or_order():
    gauges = [
        {
            "gauge": "0xROOT",
            "childGauge": "0xCHILD",
            "chainId": 42161,
            "weight": "0",
            "isKilled": True,
        },
        {
            "gauge": "0xOTHER",
            "childGauge": None,
            "chainId": 1,
            "weight": "123",
            "isKilled": False,
        },
    ]
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"gauges": gauges})

    assert asyncio.run(client(handler).get_gauges_data("balancer", block=123)) == {
        "gauges": gauges
    }
    assert seen[0].url.path == "/v1/votemarket/balancer/gauges"
    assert dict(seen[0].url.params) == {"block": "123"}


@pytest.mark.parametrize("payload", [{}, [], {"gauges": None}, {"gauges": [None]}])
def test_invalid_response_never_becomes_empty_catalog(payload):
    with pytest.raises(DataNotFoundError):
        asyncio.run(
            client(lambda request: httpx.Response(200, json=payload)).get_gauges_data(
                "curve"
            )
        )


@pytest.mark.parametrize("status,count", [(404, 1), (429, 3), (503, 3)])
def test_request_failure_remains_visible(status, count):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "fixture"})

    with pytest.raises(StakeDAOError):
        asyncio.run(client(handler).get_gauges_data("curve"))
    assert len(calls) == count


def test_discovery_filters_current_markets_but_keeps_historical_readers():
    data = [
        {
            "id": name,
            "status": status,
            "capabilities": {key: True for key in CAPABILITIES},
        }
        for name, status in [("curve", "active"), ("pendle", "deprecated")]
    ]
    service = client(lambda request: httpx.Response(200, json=data))
    assert asyncio.run(
        service.get_protocols(active_only=True, capability="gauges")
    ) == ["curve"]
    assert asyncio.run(service.get_protocols(capability="gauges")) == [
        "curve",
        "pendle",
    ]
