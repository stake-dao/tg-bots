import asyncio
from unittest.mock import MagicMock

import pytest
from web3 import Web3
from web3.exceptions import Web3RPCError
from web3.providers import BaseProvider

from bots.votemarket.v2 import votemarket


@pytest.mark.parametrize("handler", ["get_campaigns_created", "get_campaigns_increased"])
@pytest.mark.parametrize("block_count", [1, 30000, 30001, 47789])
def test_campaign_scans_cover_backlog_within_rpc_limit(monkeypatch, handler, block_count):
    start = 513317642
    end = start + block_count - 1
    ranges = []

    class Provider(BaseProvider):
        def make_request(self, method, params):
            assert method == "eth_getLogs"
            low = int(params[0]["fromBlock"], 16)
            high = int(params[0]["toBlock"], 16)
            ranges.append((low, high))
            if high - low + 1 > 30000:
                return {"jsonrpc": "2.0", "id": 1, "error": {
                    "code": -32012, "message": "getLogs request exceeded max allowed range"
                }}
            return {"jsonrpc": "2.0", "id": 1, "result": []}

    multicall = MagicMock()
    multicall.return_value.call.return_value = []
    monkeypatch.setattr(votemarket, "W3Multicall", multicall)
    asyncio.run(getattr(votemarket, handler)(
        Web3(Provider()), 42161, "0x5e5C922a5Eeab508486eB906ebE7bDFFB05D81e5",
        start, end, {}, {},
    ))

    assert ranges[0][0] == start
    assert ranges[-1][1] == end
    assert all(high - low + 1 <= 30000 for low, high in ranges)
    assert all(previous[1] + 1 == following[0] for previous, following in zip(ranges, ranges[1:]))


@pytest.mark.parametrize("handler", ["get_campaigns_created", "get_campaigns_increased"])
def test_campaign_scan_failure_aborts_before_processing(monkeypatch, handler):
    start = 513317642
    calls = []

    class Provider(BaseProvider):
        def make_request(self, method, params):
            calls.append(params[0])
            if len(calls) == 2:
                return {"jsonrpc": "2.0", "id": 1, "error": {
                    "code": -32000, "message": "fixture RPC unavailable"
                }}
            return {"jsonrpc": "2.0", "id": 1, "result": []}

    multicall = MagicMock()
    monkeypatch.setattr(votemarket, "W3Multicall", multicall)
    with pytest.raises(Web3RPCError, match="fixture RPC unavailable"):
        asyncio.run(getattr(votemarket, handler)(
            Web3(Provider()), 42161, "0x5e5C922a5Eeab508486eB906ebE7bDFFB05D81e5",
            start, start + 47788, {}, {},
        ))
    assert len(calls) == 2
    multicall.assert_not_called()
