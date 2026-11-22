import asyncio
from unittest.mock import MagicMock

import pytest
from web3 import Web3
from web3.exceptions import Web3RPCError
from web3.providers import BaseProvider

from bots.votemarket.v2 import votemarket
from shared.utils import event_logs


@pytest.fixture(autouse=True)
def isolate_scanner(monkeypatch):
    monkeypatch.setattr(event_logs.time, "sleep", lambda _seconds: None)
    event_logs.reset_learned_limits()
    yield
    event_logs.reset_learned_limits()


@pytest.mark.parametrize("handler", ["get_campaigns_created", "get_campaigns_increased"])
@pytest.mark.parametrize("block_count", [1, 30000, 30001, 47789])
@pytest.mark.parametrize("chain_id, limit", [(42161, 30000), (8453, 5000)])
def test_campaign_scans_cover_backlog_within_rpc_limit(monkeypatch, handler, block_count, chain_id, limit):
    start = 57981792 if chain_id == 8453 else 513317642
    end = start + block_count - 1
    ranges = []

    class Provider(BaseProvider):
        def make_request(self, method, params):
            assert method == "eth_getLogs"
            low = int(params[0]["fromBlock"], 16)
            high = int(params[0]["toBlock"], 16)
            if high - low + 1 > limit:
                return {"jsonrpc": "2.0", "id": 1, "error": {
                    "code": -32012, "message": "getLogs request exceeded max allowed range"
                }}
            ranges.append((low, high))
            return {"jsonrpc": "2.0", "id": 1, "result": []}

    multicall = MagicMock()
    multicall.return_value.call.return_value = []
    monkeypatch.setattr(votemarket, "W3Multicall", multicall)
    asyncio.run(getattr(votemarket, handler)(
        Web3(Provider()), chain_id, "0x5e5C922a5Eeab508486eB906ebE7bDFFB05D81e5",
        start, end, {}, {},
    ))

    assert ranges[0][0] == start
    assert ranges[-1][1] == end
    assert all(high - low + 1 <= limit for low, high in ranges)
    assert all(previous[1] + 1 == following[0] for previous, following in zip(ranges, ranges[1:]))


@pytest.mark.parametrize("handler", ["get_campaigns_created", "get_campaigns_increased"])
def test_base_scan_rotates_past_ten_block_endpoint(monkeypatch, handler):
    start, end = 57981792, 57994136
    ranges = []

    class Provider(BaseProvider):
        endpoint_count = 2
        index = 0

        def rotate(self):
            self.index += 1
            return "https://backup.example"

        def make_request(self, method, params):
            assert method == "eth_getLogs"
            low = int(params[0]["fromBlock"], 16)
            high = int(params[0]["toBlock"], 16)
            limit = 10 if self.index == 0 else 5000
            if high - low + 1 > limit:
                return {"jsonrpc": "2.0", "id": 1, "error": {
                    "code": -32012,
                    "message": f"request exceeded max allowed range: You can make eth_getLogs requests with up to a {limit} block range",
                }}
            ranges.append((low, high))
            return {"jsonrpc": "2.0", "id": 1, "result": []}

    multicall = MagicMock()
    multicall.return_value.call.return_value = []
    monkeypatch.setattr(votemarket, "W3Multicall", multicall)
    asyncio.run(getattr(votemarket, handler)(
        Web3(Provider()), 8453, "0x105694FC5204787eD571842671d1262A54a8135B",
        start, end, {}, {},
    ))

    assert ranges == [(57981792, 57986791), (57986792, 57991791), (57991792, 57994136)]


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
