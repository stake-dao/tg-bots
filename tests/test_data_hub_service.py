import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.services.data_hub_service import (
    CACHE_TTL_SECONDS,
    DataHubError,
    DataHubService,
)

SAMPLE_VAULTS = [
    {"vault": "0xAAA", "chainId": 1, "tvl": 1000.0, "protocol": "curve"},
    {"vault": "0xBBB", "chainId": 1, "tvl": 2000.0, "protocol": "balancer"},
    {"vault": "0xCCC", "chainId": 42161, "tvl": 500.0, "protocol": "curve"},
]


def _make_service():
    return DataHubService(base_url="https://test.example.com")


def _mock_response(data, status_code=200):
    return httpx.Response(
        status_code=status_code,
        request=httpx.Request("GET", "https://test.example.com/v1/vaults"),
        json=data,
    )


def _attach_mock_client(service, side_effect=None, return_value=None):
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    if side_effect:
        mock_client.request = AsyncMock(side_effect=side_effect)
    else:
        mock_client.request = AsyncMock(return_value=return_value or _mock_response(SAMPLE_VAULTS))
    service._client = mock_client
    return mock_client


class TestGetVaults:
    def test_returns_list(self):
        svc = _make_service()
        _attach_mock_client(svc)
        result = asyncio.run(svc.get_vaults())
        assert result == SAMPLE_VAULTS
        assert len(result) == 3

    def test_chain_id_filter(self):
        svc = _make_service()
        _attach_mock_client(svc)
        result = asyncio.run(svc.get_vaults(chain_id=1))
        assert len(result) == 2
        assert all(v["chainId"] == 1 for v in result)

    def test_chain_id_filter_no_match(self):
        svc = _make_service()
        _attach_mock_client(svc)
        result = asyncio.run(svc.get_vaults(chain_id=999))
        assert result == []

    def test_caching(self):
        svc = _make_service()
        mc = _attach_mock_client(svc)
        asyncio.run(svc.get_vaults())
        asyncio.run(svc.get_vaults())
        mc.request.assert_called_once()

    def test_cache_expiry(self):
        svc = _make_service()
        mc = _attach_mock_client(svc)
        asyncio.run(svc.get_vaults())
        svc._cache["vaults:all"] = (time.time() - CACHE_TTL_SECONDS - 1, SAMPLE_VAULTS)
        asyncio.run(svc.get_vaults())
        assert mc.request.call_count == 2

    def test_separate_cache_per_chain_id(self):
        svc = _make_service()
        mc = _attach_mock_client(svc)
        asyncio.run(svc.get_vaults())
        asyncio.run(svc.get_vaults(chain_id=1))
        assert mc.request.call_count == 2


class TestRetry:
    def test_retry_on_502(self):
        svc = _make_service()
        fail_resp = httpx.Response(
            status_code=502,
            request=httpx.Request("GET", "https://test.example.com/v1/vaults"),
        )
        _attach_mock_client(
            svc,
            side_effect=[
                httpx.HTTPStatusError("502", request=fail_resp.request, response=fail_resp),
                _mock_response(SAMPLE_VAULTS),
            ],
        )
        with patch("shared.services.data_hub_service.asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.run(svc.get_vaults())
        assert result == SAMPLE_VAULTS

    def test_retry_on_timeout(self):
        svc = _make_service()
        _attach_mock_client(
            svc,
            side_effect=[
                httpx.TimeoutException("timeout"),
                _mock_response(SAMPLE_VAULTS),
            ],
        )
        with patch("shared.services.data_hub_service.asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.run(svc.get_vaults())
        assert result == SAMPLE_VAULTS

    def test_no_retry_on_404(self):
        svc = _make_service()
        fail_resp = httpx.Response(
            status_code=404,
            request=httpx.Request("GET", "https://test.example.com/v1/vaults"),
        )
        mc = _attach_mock_client(
            svc,
            side_effect=httpx.HTTPStatusError("404", request=fail_resp.request, response=fail_resp),
        )
        with pytest.raises(DataHubError, match="HTTP 404"):
            asyncio.run(svc.get_vaults())
        mc.request.assert_called_once()

    def test_exhausted_retries(self):
        svc = _make_service()
        fail_resp = httpx.Response(
            status_code=503,
            request=httpx.Request("GET", "https://test.example.com/v1/vaults"),
        )
        mc = _attach_mock_client(
            svc,
            side_effect=httpx.HTTPStatusError("503", request=fail_resp.request, response=fail_resp),
        )
        with patch("shared.services.data_hub_service.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(DataHubError, match="Failed after 3 retries"):
                asyncio.run(svc.get_vaults())
        assert mc.request.call_count == 3


SAMPLE_HUB_VAULTS = [
    {
        "vault": "0xAAA",
        "chainId": 1,
        "tvl": 1000.0,
        "protocol": "curve",
        "totalSupply": "1000000",
        "lpPriceInUsd": 1.0,
        "rewardReceiver": "0xRR1",
        "gaugeAddress": "0xG1",
        "lpToken": {"name": "Test", "symbol": "TST", "address": "0xLP1", "decimals": 18},
        "gauge": {"address": "0xG1", "totalSupply": "500", "totalSupplyUsd": 500.0},
        "apr": {"boost": 1.5, "current": {"total": 5.0, "details": []}},
        "onlyboost": None,
        "rewards": [],
    },
    {
        "vault": "0xBBB",
        "chainId": 1,
        "tvl": 2000.0,
        "protocol": "balancer",
        "totalSupply": "2000000",
        "lpPriceInUsd": 2.0,
        "rewardReceiver": "0xRR2",
        "gaugeAddress": "0xG2",
        "lpToken": {"name": "Test2", "symbol": "TST2", "address": "0xLP2", "decimals": 18},
        "gauge": {"address": "0xG2", "totalSupply": "1000", "totalSupplyUsd": 1000.0},
        "apr": {"boost": 2.0, "current": {"total": 10.0, "details": []}},
        "onlyboost": None,
        "rewards": [],
    },
]


class TestGetAdaptedVaults:
    def test_returns_adapted(self):
        svc = _make_service()
        _attach_mock_client(svc, return_value=_mock_response(SAMPLE_HUB_VAULTS))
        result = asyncio.run(svc.get_adapted_vaults())
        assert len(result) == 2
        assert result[0]["address"] == "0xAAA"
        assert result[1]["address"] == "0xBBB"
        assert "_hub" in result[0]


class TestGetVaultAprs:
    def test_returns_apr_map(self):
        svc = _make_service()
        _attach_mock_client(svc, return_value=_mock_response(SAMPLE_HUB_VAULTS))
        result = asyncio.run(svc.get_vault_aprs())
        assert result["0xaaa"] == pytest.approx(0.05)
        assert result["0xbbb"] == pytest.approx(0.10)


class TestGetGaugeTvls:
    def test_returns_tvl_map(self):
        svc = _make_service()
        _attach_mock_client(svc, return_value=_mock_response(SAMPLE_HUB_VAULTS))
        result = asyncio.run(svc.get_gauge_tvls())
        assert result["0xg1"] == 1000.0
        assert result["0xg2"] == 2000.0


class TestContextManager:
    def test_close(self):
        svc = _make_service()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.is_closed = False
        svc._client = mock_client
        asyncio.run(svc.close())
        mock_client.aclose.assert_called_once()
        assert svc._client is None
