import io
import re
from types import SimpleNamespace
from unittest.mock import MagicMock
import zipfile

import pytest
import requests

from bots.vlcvx_delegation import main as bot
from shared.constants import GlobalConstants
from shared.utils.globals import pad_address


@pytest.fixture
def isolated_bot(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    monkeypatch.setenv("GITHUB_RUN_ID", "3")
    monkeypatch.setenv("PROD", "True")
    monkeypatch.setattr(GlobalConstants, "BOT_API_KEY", "fixture-bot")
    state = {"checkpoint": 900, "head": 1000, "lookup_status": 200, "payloads": []}
    user = "0x00000000000000000000000000000000000000A1"
    w3 = MagicMock()
    w3.eth.get_block.side_effect = lambda block: SimpleNamespace(number=state["head"])
    w3.eth.get_transaction.return_value = {"from": user}
    w3.eth.call.return_value = b""
    service = MagicMock()
    service.get_w3.return_value = w3
    contract = MagicMock()
    contract.functions.epochCount.return_value.call.return_value = 20
    contract.functions.findEpochId.return_value.call.return_value = 18
    contract.functions.balanceAtEpochOf.return_value.call.return_value = 100 * 10**18
    service.get_contract.return_value = contract
    monkeypatch.setattr(bot, "get_web3_service", lambda chain: service)
    monkeypatch.setattr(bot, "get_single_token_price", lambda *args: 1.0)

    def get(url, **kwargs):
        state.setdefault("reads", []).append(url)
        response = MagicMock()
        response.status_code = 200
        if url.endswith("/runs"):
            response.status_code = state["lookup_status"]
            response.json.return_value = {"workflow_runs": [
                {"id": 3, "jobs_url": "https://fixture.invalid/current"},
                {"id": 2, "jobs_url": "https://fixture.invalid/skipped"},
                {"id": 1, "jobs_url": "https://fixture.invalid/previous"},
            ]}
        elif url.endswith("/skipped"):
            response.json.return_value = {"jobs": [{"conclusion": "skipped"}]}
        elif url.endswith("/previous"):
            response.json.return_value = {"jobs": [{"conclusion": "success"}]}
        elif url.endswith("/logs"):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                content = "no checkpoint" if state["checkpoint"] is None else f"Chain 1 / last block {state['checkpoint']}"
                content += state.get("extra_checkpoint", "")
                archive.writestr("build/run.txt", content)
            response.iter_content.return_value = [data.getvalue()]
        else:
            raise AssertionError("Unexpected HTTP read")
        return response

    def post(url, **kwargs):
        assert url == "https://api.telegram.org/botfixture-bot/sendMessage"
        state["payloads"].append(kwargs["json"])
        response = MagicMock()
        response.status_code = 200
        return response

    def logs(address, start, end, topics, chain):
        if start <= 910 <= end:
            return [{"topics": [bot.TOPIC_DELEGATE_SET, pad_address(user), pad_address(bot.STAKE_DAO_DELEGATE)],
                     "blockNumber": hex(910), "logIndex": "0x0", "transactionHash": "0xdead"}]
        return []

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(bot, "get_logs_by_address_and_topics", logs)
    return state, w3


def test_main_recovers_existing_logs_delivers_and_restarts(isolated_bot, caplog):
    state, _ = isolated_bot
    bot.main()
    output = caplog.text
    state["checkpoint"] = int(re.search(r"Chain 1 / last block (\d+)", output)[1])
    assert state["checkpoint"] == 999
    assert len(state["payloads"]) == 1
    payload = state["payloads"][0]
    assert payload["chat_id"] == "@SDLiquidLockerBot"
    assert "New vlCVX delegation to Stake DAO" in payload["text"]
    assert "100.00 vlCVX" in payload["text"]
    state["head"] = 1010
    caplog.clear()
    bot.main()
    assert len(state["payloads"]) == 1
    assert "Chain 1 / last block 1009" in caplog.text


@pytest.mark.parametrize("failure", ["credentials", "github", "checkpoint", "rpc"])
def test_main_aborts_before_delivery_on_unavailable_inputs(isolated_bot, monkeypatch, failure):
    state, w3 = isolated_bot
    if failure == "credentials":
        monkeypatch.delenv("GITHUB_TOKEN")
    elif failure == "github":
        state["lookup_status"] = 503
    elif failure == "checkpoint":
        state["checkpoint"] = None
    else:
        w3.eth.get_block.side_effect = ConnectionError("fixture RPC unavailable")
    with pytest.raises((KeyError, RuntimeError, ConnectionError)):
        bot.main()
    assert state["payloads"] == []


def test_incentives_partial_delivery_and_restart(monkeypatch):
    import json
    from bots.votemarket_incentives import main as incentives

    monkeypatch.setenv("PROD", "True")
    monkeypatch.setattr(GlobalConstants, "BOT_VOTEMARKET_API_KEY", "fixture-bot")
    monkeypatch.setattr(incentives.time, "sleep", lambda seconds: None)
    state = {
        incentives.REDIS_KEY_SEEN: "[]",
        incentives.REDIS_KEY_GAUGES: "{}",
        incentives.REDIS_KEY_TVL: "{}",
    }
    redis = MagicMock()
    redis.get.side_effect = state.get
    redis.set.side_effect = lambda key, value: state.update({key: value})
    monkeypatch.setattr(incentives, "get_redis_client", lambda: redis)
    rows = [{"id": i, "rewardSymbol": symbol, "amount": str(10**18),
             "duration": 86400, "start": 1700000000, "end": 1700086400}
            for i, symbol in [(1, "FIRST"), (2, "SECOND")]]
    response = MagicMock(status_code=200)
    response.json.return_value = rows

    def get(url, **kwargs):
        assert url == incentives.INCENTIVES_URL
        return response

    attempts = []
    fail_second = True

    def post(url, **kwargs):
        assert url == "https://api.telegram.org/botfixture-bot/sendMessage"
        payload = kwargs["json"]
        assert payload["chat_id"] == "@votemarket"
        symbol = "FIRST" if "FIRST" in payload["text"] else "SECOND"
        attempts.append(symbol)
        reply = requests.Response()
        reply.status_code = 503 if fail_second and symbol == "SECOND" else 200
        reply._content = b"{}"
        return reply

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    incentives.main()
    assert set(json.loads(state[incentives.REDIS_KEY_SEEN])) == {1}
    fail_second = False
    incentives.main()
    assert set(json.loads(state[incentives.REDIS_KEY_SEEN])) == {1, 2}
    incentives.main()
    assert attempts == ["FIRST", "SECOND", "SECOND"]


@pytest.mark.parametrize("handler", ["manageTokenEchange", "manageAddLiquidity", "manageRemoveLiquidity", "manageRemoveLiquidityOneCoin"])
def test_pool_api_and_compact_fallback_deliver_identical_messages(monkeypatch, handler):
    import copy
    import json
    from pathlib import Path
    from bots.curve.pools import main as pools

    root = Path(pools.__file__).parents[4]
    monkeypatch.chdir(root)
    compact = json.loads((root / "json/bots/allPools.json").read_text())
    pool = next(p for p in compact if p.get("blockchainId") == "ethereum" and len(p["coins"]) >= 2)
    full = copy.deepcopy(pool)
    full.update({"usdTotal": 999, "gaugeRewards": [], "poolUrls": ["https://fixture.invalid"]})
    for coin in full["coins"]:
        coin.update({"poolBalance": "999", "usdPrice": 1, "name": "Unused fixture metadata"})
    response = MagicMock()
    response.json.return_value = {"data": {"poolData": [full]}}
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    api_pools = pools.get_pools_from_api()

    def unavailable(*args, **kwargs):
        raise requests.ConnectionError("fixture pool API unavailable")

    monkeypatch.setattr(requests, "get", unavailable)
    fallback_pools = pools.get_pools_from_api()
    assert len(fallback_pools) == 5480
    w3 = MagicMock()
    w3.eth.get_transaction.return_value = {"from": "0x00000000000000000000000000000000000000A1"}
    w3.eth.call.return_value = b""
    w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 10**18
    w3.eth.contract.return_value.functions.coins.return_value.call.return_value = pool["coins"][0]["address"]
    amounts = [100000 * 10**int(c["decimals"]) for c in pool["coins"]]
    event = {"blockNumber": 100, "transactionHash": bytes.fromhex("ab" * 32),
             "args": {"sold_id": 0, "bought_id": 1, "tokens_sold": amounts[0],
                      "tokens_bought": amounts[1], "token_amounts": amounts,
                      "token_id": 0, "coin_amount": amounts[0]}}
    monkeypatch.setenv("PROD", "True")
    monkeypatch.setattr(GlobalConstants, "BOT_API_KEY", "fixture-bot")
    payloads = []

    def post(url, **kwargs):
        assert url == "https://api.telegram.org/botfixture-bot/sendMessage"
        payloads.append(kwargs["json"])
        reply = requests.Response()
        reply.status_code = 200
        return reply

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr("shared.communication.telegram.time.sleep", lambda seconds: None)
    for data in [api_pools, fallback_pools]:
        getattr(pools, handler)(w3, pool["address"], event, data, "ethereum", 1)
    assert len(payloads) == 2
    assert "/tx/0x" + "ab" * 32 in payloads[0]["text"]
    assert payloads[0] == payloads[1]
    assert payloads[0]["chat_id"] == "@SDLiquidLockerBot"


def test_native_web3_keeps_rpc_fallback_and_poa(monkeypatch):
    import json
    from shared.services.web3_service import build_web3

    urls = ["https://fixture.invalid/primary", "https://fixture.invalid/backup"]
    monkeypatch.setattr(GlobalConstants, "rpc_endpoints", lambda chain: urls)
    calls = []

    def send(session, request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.status_code = 503 if request.url == urls[0] else 200
        response.url = request.url
        payload = json.loads(request.body)
        result = {"number": "0x64", "extraData": "0x" + "00" * 97, "transactions": []}
        response._content = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.sessions.Session, "send", send)
    w3 = build_web3(56)
    w3.provider.exception_retry_configuration = None
    block = w3.eth.get_block("latest")
    assert block.number == 100
    assert len(block.proofOfAuthorityData) == 97
    assert calls == urls
    w3.eth.get_block("latest")
    assert calls == urls + [urls[1]]



@pytest.mark.parametrize("rpc_fails", [False, True])
def test_votemarket_multichain_recovers_logs_once_per_run(isolated_bot, monkeypatch, caplog, rpc_fails):
    import asyncio
    from bots.votemarket.v2 import votemarket

    from web3 import Web3
    from web3.providers import BaseProvider

    state, _ = isolated_bot
    rpc_calls = []

    class Provider(BaseProvider):
        def make_request(self, method, params):
            rpc_calls.append((method, params))
            if rpc_fails and method == "eth_getLogs":
                raise ConnectionError("fixture RPC unavailable")
            result = {"eth_getBlockByNumber": {"number": "0x3e8"}, "eth_chainId": "0x1", "eth_getLogs": []}[method]
            return {"jsonrpc": "2.0", "id": 1, "result": result}

    w3 = Web3(Provider())
    state["extra_checkpoint"] = "\nChain 10 / last block 900"
    service = MagicMock()
    service.w3 = {1: w3, 10: w3}
    service.get_w3.return_value = w3
    monkeypatch.setattr(votemarket, "web3_service", service)
    platform = "0x00000000000000000000000000000000000000A1"
    monkeypatch.setattr(votemarket, "get_all_platforms", lambda: (
        [{1: [platform], 10: [platform]}, {1: [platform]}], {}, {}
    ))
    multicall = MagicMock()
    multicall.return_value.call.return_value = []
    monkeypatch.setattr(votemarket, "W3Multicall", multicall)
    monkeypatch.setattr(votemarket.time, "sleep", lambda seconds: None)
    if rpc_fails:
        with pytest.raises(ConnectionError):
            asyncio.run(votemarket.main())
        assert state["payloads"] == []
        return
    asyncio.run(votemarket.main())
    assert sum(url.endswith("/logs") for url in state["reads"]) == 1
    assert caplog.text.count("Chain 1 / last block 999") == 1
    assert caplog.text.count("Chain 10 / last block 941") == 1
    assert sum(method == "eth_getLogs" for method, _ in rpc_calls) == 6
    assert state["payloads"] == []
    state["reads"].clear()
    asyncio.run(votemarket.main())
    assert sum(url.endswith("/logs") for url in state["reads"]) == 1



def test_lockers_native_event_scans(isolated_bot, monkeypatch, caplog):
    from web3 import Web3
    from web3.providers import BaseProvider
    from bots.curve.pools import main as pools

    state, _ = isolated_bot
    rpc_calls = []

    class Provider(BaseProvider):
        def make_request(self, method, params):
            rpc_calls.append((method, params))
            result = {"eth_getBlockByNumber": {"number": "0x3e8"}, "eth_chainId": "0x1", "eth_getLogs": []}[method]
            return {"jsonrpc": "2.0", "id": 1, "result": result}

    service = MagicMock()
    service.get_w3.return_value = Web3(Provider())
    chains = list(GlobalConstants.CHAIN_ID_TO_PUBLIC_RPC)
    state["extra_checkpoint"] = "".join(f"\nChain {chain} / last block 900" for chain in chains if chain != 1)
    monkeypatch.setattr(pools, "get_web3_service", lambda: service)
    monkeypatch.setattr(pools, "load_lockers", lambda: [])
    monkeypatch.setattr(pools, "get_pools_from_api", lambda: [])
    pools.main()
    for chain in chains:
        assert caplog.text.count(f"Chain {chain} / last block") == int(chain in pools.BLOCKCHAIN_IDS)
    filters = [params[0] for method, params in rpc_calls if method == "eth_getLogs"]
    assert len(filters) > 10
    assert all(int(item["fromBlock"], 16) == 900 and int(item["toBlock"], 16) >= 900 for item in filters)
    assert state["payloads"] == []



@pytest.mark.parametrize("rpc_fails", [False, True])
def test_onlyboost_native_scans_preserve_redis(isolated_bot, monkeypatch, caplog, rpc_fails):
    from web3 import Web3
    from web3.providers import BaseProvider
    from bots.onlyboost_v2 import main as onlyboost

    state, _ = isolated_bot
    records = {"1": "900", "42161": "900", "146": "900"}
    rpc_calls = []

    class Provider(BaseProvider):
        def make_request(self, method, params):
            if method == "eth_getLogs":
                assert all(topic.startswith("0x") and len(topic) == 66 for topic in params[0]["topics"])
                if rpc_fails:
                    raise ConnectionError("fixture RPC unavailable")
                rpc_calls.append(params[0])
            result = {"eth_getBlockByNumber": {"number": "0x3e8"}, "eth_chainId": "0x1", "eth_getLogs": [], "eth_call": "0x" + "00" * 32}[method]
            return {"jsonrpc": "2.0", "id": 1, "result": result}

    redis = MagicMock()
    redis.__enter__.return_value = redis
    redis.hgetall.side_effect = lambda key: records.copy()
    redis.hset.side_effect = lambda key, field, value: records.update({str(field): str(value)})
    monkeypatch.setattr(onlyboost, "get_redis_client", lambda: redis)
    monkeypatch.setattr(onlyboost, "PROD", True)
    monkeypatch.setattr(onlyboost, "DRY_RUN", False)
    service = MagicMock()
    service.w3 = {int(chain): Web3(Provider()) for chain in records}
    service.get_w3.side_effect = lambda chain: service.w3[chain]
    monkeypatch.setattr(onlyboost, "get_web3_service", lambda chain: service)
    monkeypatch.setattr(onlyboost.OnlyBoostV2Bot, "get_alternate_web3", lambda self, chain: None)
    monkeypatch.setattr(onlyboost.OnlyBoostV2Bot, "_fetch_ipor_vaults", lambda self: None)
    monkeypatch.setattr(onlyboost.OnlyBoostV2Bot, "_fetch_beefy_vaults", lambda self: None)
    monkeypatch.setattr(onlyboost, "fetch_adapted_vaults", lambda: [
        {"address": "0x00000000000000000000000000000000000000A1", "chainId": int(chain)} for chain in records
    ])
    onlyboost.main()
    if rpc_fails:
        assert records == {"1": "900", "42161": "900", "146": "900"}
    else:
        assert records == {"1": "999", "42161": "901", "146": "971"}
        assert len(rpc_calls) == 6
        assert "failed, skipping" not in caplog.text
        onlyboost.main()
        assert records == {"1": "1000", "42161": "902", "146": "972"}
    assert state["payloads"] == []



@pytest.mark.parametrize("unavailable", [False, True])
def test_native_rpc_log_limit_failover(monkeypatch, unavailable):
    import json
    from web3 import Web3
    from web3.exceptions import Web3RPCError
    from shared.services.web3_service import build_web3
    from bots.curve.pools import main as pools

    urls = ["https://fixture.invalid/limited", "https://fixture.invalid/healthy"]
    monkeypatch.setattr(GlobalConstants, "rpc_endpoints", lambda chain: urls)
    calls = []
    queries = []

    def send(session, request, **kwargs):
        payload = json.loads(request.body)
        assert payload["method"] == "eth_getLogs"
        calls.append(request.url)
        queries.append(payload["params"][0])
        data = {"jsonrpc": "2.0", "id": payload["id"]}
        if request.url == urls[0] or unavailable:
            data["error"] = {"code": -32005, "message": "limit exceeded"}
        else:
            data["result"] = []
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(data).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.sessions.Session, "send", send)
    w3 = build_web3(1)
    w3.provider.exception_retry_configuration = None
    contract = w3.eth.contract(address=Web3.to_checksum_address("0x00000000000000000000000000000000000000A1"), abi=pools.curveStableSwapABI)
    if unavailable:
        with pytest.raises(Web3RPCError):
            contract.events.TokenExchange().get_logs(from_block=10, to_block=20)
        assert calls == urls
    else:
        assert list(contract.events.TokenExchange().get_logs(from_block=10, to_block=20)) == []
        assert calls == urls
        contract.events.TokenExchange().get_logs(from_block=10, to_block=20)
        assert calls == urls + [urls[1]]
    assert all(query == queries[0] for query in queries)
    assert queries[0]["fromBlock"] == "0xa"
    assert queries[0]["toBlock"] == "0x14"
