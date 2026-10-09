"""vlCVX delegation reporting of the daily lockers bot (ENG-2104).

Convex moved vlCVX voting on-chain, so the bot reads the two Delegation
contracts — gauge weights and DAO proposals delegate independently — instead
of the legacy Snapshot cvx.eth registry.
"""

from unittest.mock import MagicMock

import pytest
from shared.constants import ContractRegistry

from bots.daily.lockers import main as lockers_bot

GAUGE = ContractRegistry.CONVEX_GAUGE_DELEGATION[1]
DAO = ContractRegistry.CONVEX_DAO_DELEGATION[1]
DELEGATE = ContractRegistry.DELEGATION_ONCHAIN[1]
VLCVX = ContractRegistry.CONVEX_CVX_LOCKER[1]

BLOCK = 23_456_789
E18 = 10**18


def test_registry_holds_the_convex_delegation_addresses():
    """Literals, so a swapped or mistyped registry entry cannot pass here."""
    assert GAUGE == "0xb8270eef1319173dE9f5033FED442F638ff1607d"
    assert DAO == "0x22697721EC1C14e42305e91BF02672932E8a7B09"
    assert DELEGATE == "0xbB06fEFB8f23A7c60C93fe20464DB6687C51955f"


@pytest.fixture
def web3(monkeypatch):
    balances = {GAUGE.lower(): 9_990_838 * E18, DAO.lower(): 1_671_791 * E18}
    w3 = MagicMock()
    w3.calls = []
    w3.eth.block_number = BLOCK

    def recorder(addr, abi, fn, arg, result):
        def call(block_identifier):
            w3.calls.append((addr.lower(), abi, fn, arg, block_identifier))
            return result

        return MagicMock(call=call)

    def fake_load_contract_w3(_w3, addr, abi):
        contract = MagicMock()
        contract.functions.balanceOf.side_effect = lambda delegate: recorder(
            addr, abi, "balanceOf", delegate, balances[addr.lower()]
        )
        return contract

    def fake_eth_contract(address, abi):
        contract = MagicMock()
        contract.functions.totalSupply.return_value = recorder(
            address, "vlCVX", "totalSupply", None, 43_647_601 * E18
        )
        return contract

    monkeypatch.setattr(lockers_bot, "load_contract_w3", fake_load_contract_w3)
    w3.eth.contract.side_effect = fake_eth_contract
    return w3


def test_reads_both_delegation_contracts_at_one_pinned_block(web3):
    """One block, so the reads cannot straddle the Thursday epoch rollover."""
    gauge, dao, supply = lockers_bot.fetch_vlcvx_delegations(web3)

    assert (gauge, dao, supply) == (
        9_990_838 * E18,
        1_671_791 * E18,
        43_647_601 * E18,
    )
    assert web3.calls == [
        (GAUGE.lower(), "convex_delegation", "balanceOf", DELEGATE, BLOCK),
        (DAO.lower(), "convex_delegation", "balanceOf", DELEGATE, BLOCK),
        (VLCVX.lower(), "vlCVX", "totalSupply", None, BLOCK),
    ]


def test_delegation_message_reports_gauge_and_dao_separately(
    web3, monkeypatch
):
    sent = []
    monkeypatch.setattr(lockers_bot, "_send", sent.append)

    lockers_bot._send_vlcvx_delegations(web3)

    assert sent == [
        "<u>Stake DAO Delegations :</u>\n\n"
        "vlCVX gauge: 9.99M delegated (22.89%)\n"
        "vlCVX DAO: 1.67M delegated (3.83%)"
    ]


def test_delegation_message_is_skipped_when_the_reads_fail(monkeypatch):
    """A failed RPC must not post a misleading '0 delegated' line."""
    sent = []
    monkeypatch.setattr(lockers_bot, "_send", sent.append)
    monkeypatch.setattr(
        lockers_bot,
        "fetch_vlcvx_delegations",
        MagicMock(side_effect=RuntimeError("rpc down")),
    )

    lockers_bot._send_vlcvx_delegations(MagicMock())

    assert sent == []
