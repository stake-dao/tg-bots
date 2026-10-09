"""
Tests for user address resolution in OnlyBoost V2 bot.

Reproduces the bug where a Beefy strategy contract withdrawal shows the
strategy address instead of the real user, while the matching deposit
correctly shows the Beefy vault address.

Tx references:
- Withdraw: 0xdbe1ea4a5e3b25c712182aa28294c25cc48613a1f166c32211fd9ed20d3cdab3
- Deposit:  0x7bdcee39c2711c0faa2c2d656b1e4ba92304ecc1901875a2996bce4788282bad
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_DIR = REPO_ROOT / "script"

BEEFY_VAULT_ADDRESS = "0x44e139B83c450E22F9a2CAe2910df5f17A51b511"
BEEFY_STRATEGY_ADDRESS = "0x125110C3b1b4859d833B1353d7Af5bdA51BAAf2E"
REAL_USER_EOA = "0xDeaDBeEf00000000000000000000000000000001"

STAKE_DAO_VAULT = "0x7053FA875C478045124CE3Ef740a189b6037DF91"
CURVE_GAUGE = "0x4e227d29b33B77113F84bcC189a6F886755a1f24"

MOCK_BEEFY_VAULTS = [
    {
        "id": "stakedao-ethereum-mseth-weth",
        "name": "msETH/WETH",
        "status": "active",
        "platformId": "stakedao",
        "network": "ethereum",
        "earnContractAddress": BEEFY_VAULT_ADDRESS,
    },
]

MOCK_IPOR_VAULTS = []

VAULT_DATA = {
    "chainId": 1,
    "address": STAKE_DAO_VAULT,
    "asset": {
        "name": "Curve.fi Factory Plain Pool: msETH/WETH",
        "decimals": 18,
        "address": "0xasset",
    },
    "totalSupply": str(10_000 * 10**18),
    "totalSupplyUSD": str(10_000 * 2189),
    "gauge": {"address": CURVE_GAUGE},
    "protocolId": 1,
}


@pytest.fixture
def mock_env():
    with patch.dict(os.environ, {"PROD": "false"}):
        yield


@pytest.fixture
def mock_requests():
    with patch("requests.get") as mock_get:
        yield mock_get


@pytest.fixture
def mock_github_service():
    with patch("bots.utils.github.GithubLogService") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.extract_chain_id_last_block.return_value = []
        mock_instance.get_last_block_and_log.return_value = (0, 0)
        mock_cls.return_value = mock_instance
        yield mock_cls


@pytest.fixture(autouse=True)
def stub_redis_dedup():
    """Keep tests hermetic: never touch the real upstash instance.

    Patched at the source module because these tests delete and re-import
    bots.onlyboost_v2.main, which rebinds get_redis_client on import.
    """
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.sismember.return_value = False
    fake.hgetall.return_value = {}
    with patch("shared.utils.globals.get_redis_client", return_value=fake):
        yield fake


def _make_requests_side_effect(mock_requests):
    def get_side_effect(url, **kwargs):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        if "beefy.finance" in url:
            response.json.return_value = MOCK_BEEFY_VAULTS
        else:
            response.json.return_value = MOCK_IPOR_VAULTS
        return response

    mock_requests.side_effect = get_side_effect


def _make_bot(mock_requests):
    _make_requests_side_effect(mock_requests)
    from importlib import import_module

    if "bots.onlyboost_v2.main" in sys.modules:
        del sys.modules["bots.onlyboost_v2.main"]

    main_module = import_module("bots.onlyboost_v2.main")
    main_module.DRY_RUN = False
    bot = main_module.OnlyBoostV2Bot()
    bot.vault_apr_cache = {}
    bot.hook_incentives = {}
    bot.reward_prices = {}
    return main_module, bot


def _make_mock_web3(owner_is_contract: bool, tx_from: str = REAL_USER_EOA):
    mock_web3 = MagicMock()

    mock_contract = MagicMock()
    mock_contract.functions.totalSupply.return_value.call.return_value = (
        10_000 * 10**18
    )
    mock_contract.functions.balanceOf.return_value.call.return_value = (
        3_300 * 10**18
    )
    mock_web3.eth.contract.return_value = mock_contract
    mock_web3.to_checksum_address = lambda x: x

    from web3 import Web3 as RealWeb3

    mock_web3.to_checksum_address = RealWeb3.to_checksum_address

    if owner_is_contract:
        mock_web3.eth.get_code.return_value = b"\x60\x80\x60\x40"
    else:
        mock_web3.eth.get_code.return_value = b""

    mock_web3.eth.get_transaction.return_value = {"from": tx_from}

    return mock_web3


class TestWithdrawUserResolution:
    """Withdrawal where owner is a contract should resolve to tx['from']."""

    def test_withdraw_contract_owner_resolves_to_tx_from(
        self, mock_env, mock_requests, mock_github_service
    ):
        main_module, bot = _make_bot(mock_requests)

        with patch.object(main_module, "send_telegram_message") as mock_tg:
            event = {
                "is_deposit": False,
                "is_enso": False,
                "is_rebalance": False,
                "ipor_label": "",
                "beefy_label": "",
                "args": {
                    "sender": BEEFY_STRATEGY_ADDRESS,
                    "owner": BEEFY_STRATEGY_ADDRESS,
                    "receiver": BEEFY_STRATEGY_ADDRESS,
                    "assets": 144_370000000000000000,
                },
                "transactionHash": bytes.fromhex(
                    "dbe1ea4a5e3b25c712182aa28294c25cc48613a1f166c32211fd9ed20d3cdab3"
                ),
                "logIndex": 1,
                "blockNumber": 22150000,
                "vault": VAULT_DATA,
            }

            mock_web3 = _make_mock_web3(
                owner_is_contract=True, tx_from=REAL_USER_EOA
            )

            bot.process_vault_event(VAULT_DATA, event, mock_web3)

            mock_web3.eth.get_code.assert_called_once()
            mock_web3.eth.get_transaction.assert_called_once()

            assert mock_tg.called
            message = mock_tg.call_args[0][2]
            assert "Withdraw" in message
            assert REAL_USER_EOA[:6].lower() in message.lower() or REAL_USER_EOA in message

    def test_withdraw_eoa_owner_stays_as_is(
        self, mock_env, mock_requests, mock_github_service
    ):
        """When owner is an EOA, no resolution needed — keep the address."""
        main_module, bot = _make_bot(mock_requests)

        eoa_user = "0xAAAABBBBCCCCDDDDEEEEFFFF0000111122223333"

        with patch.object(main_module, "send_telegram_message") as mock_tg:
            event = {
                "is_deposit": False,
                "is_enso": False,
                "is_rebalance": False,
                "ipor_label": "",
                "beefy_label": "",
                "args": {
                    "sender": eoa_user,
                    "owner": eoa_user,
                    "receiver": eoa_user,
                    "assets": 144_370000000000000000,
                },
                "transactionHash": bytes.fromhex("ab" * 32),
                "logIndex": 1,
                "blockNumber": 22150000,
                "vault": VAULT_DATA,
            }

            mock_web3 = _make_mock_web3(owner_is_contract=False)

            bot.process_vault_event(VAULT_DATA, event, mock_web3)

            mock_web3.eth.get_code.assert_called_once()
            mock_web3.eth.get_transaction.assert_not_called()

            assert mock_tg.called
            message = mock_tg.call_args[0][2]
            assert eoa_user[:6].lower() in message.lower() or eoa_user in message


class TestDepositAndWithdrawConsistency:
    """Same real user should appear in both deposit and withdraw alerts."""

    def test_same_user_for_matching_deposit_and_withdraw(
        self, mock_env, mock_requests, mock_github_service
    ):
        main_module, bot = _make_bot(mock_requests)

        messages = []

        def capture_message(*args, **kwargs):
            messages.append(args[2])

        with patch.object(
            main_module, "send_telegram_message", side_effect=capture_message
        ):
            deposit_event = {
                "is_deposit": True,
                "is_enso": False,
                "is_rebalance": False,
                "ipor_label": "",
                "beefy_label": "Beefy msETH/WETH",
                "args": {
                    "sender": BEEFY_VAULT_ADDRESS,
                    "owner": BEEFY_VAULT_ADDRESS,
                    "assets": 144_380000000000000000,
                },
                "transactionHash": bytes.fromhex(
                    "7bdcee39c2711c0faa2c2d656b1e4ba92304ecc1901875a2996bce4788282bad"
                ),
                "logIndex": 1,
                "blockNumber": 22150001,
                "vault": VAULT_DATA,
            }

            withdraw_event = {
                "is_deposit": False,
                "is_enso": False,
                "is_rebalance": False,
                "ipor_label": "",
                "beefy_label": "",
                "args": {
                    "sender": BEEFY_STRATEGY_ADDRESS,
                    "owner": BEEFY_STRATEGY_ADDRESS,
                    "receiver": BEEFY_STRATEGY_ADDRESS,
                    "assets": 144_370000000000000000,
                },
                "transactionHash": bytes.fromhex(
                    "dbe1ea4a5e3b25c712182aa28294c25cc48613a1f166c32211fd9ed20d3cdab3"
                ),
                "logIndex": 1,
                "blockNumber": 22150000,
                "vault": VAULT_DATA,
            }

            deposit_web3 = _make_mock_web3(owner_is_contract=True, tx_from=BEEFY_VAULT_ADDRESS)
            bot.process_vault_event(VAULT_DATA, deposit_event, deposit_web3)

            withdraw_web3 = _make_mock_web3(
                owner_is_contract=True, tx_from=REAL_USER_EOA
            )
            bot.process_vault_event(VAULT_DATA, withdraw_event, withdraw_web3)

            assert len(messages) == 2

            deposit_msg = messages[0]
            withdraw_msg = messages[1]

            assert "Deposited" in deposit_msg
            assert "Withdraw" in withdraw_msg

            assert BEEFY_STRATEGY_ADDRESS not in withdraw_msg
