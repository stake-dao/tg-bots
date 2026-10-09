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
    (tmp_path / "json").mkdir()
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
