"""
Tests for Beefy vault labeling in OnlyBoost V2 bot.

Tests the integration with Beefy Finance API to label transactions
that originate from Beefy vaults using Stake DAO strategies.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_DIR = REPO_ROOT / "script"


# Mock Beefy API response with realistic data
MOCK_BEEFY_VAULTS = [
    {
        "id": "stakedao-ethereum-susd-susde",
        "name": "sUSD/sUSDe",
        "status": "active",
        "platformId": "stakedao",
        "network": "ethereum",
        "earnContractAddress": "0x0F59726bC1101A18cB332E176f4b0674d65Ff453",
    },
    {
        "id": "stakedao-ethereum-sdcrv-crv",
        "name": "sdCRV/CRV",
        "status": "active",
        "platformId": "stakedao",
        "network": "ethereum",
        "earnContractAddress": "0xdf8bA5860dEeD6308463501E63fBb8659Dc82F4C",
    },
    {
        "id": "stakedao-base-scrvusd-usdc",
        "name": "scrvUSD/USDC",
        "status": "active",
        "platformId": "stakedao",
        "network": "base",
        "earnContractAddress": "0xaE82708d470FE681d16582EbBA5FC34D6Bb5775b",
    },
    {
        # Inactive vault - should be filtered out
        "id": "stakedao-ethereum-old-vault",
        "name": "Old Vault",
        "status": "paused",
        "platformId": "stakedao",
        "network": "ethereum",
        "earnContractAddress": "0x1234567890123456789012345678901234567890",
    },
    {
        # Non-stakedao vault - should be filtered out
        "id": "convex-ethereum-some-pool",
        "name": "Convex Pool",
        "status": "active",
        "platformId": "convex",
        "network": "ethereum",
        "earnContractAddress": "0xABCDEF1234567890ABCDEF1234567890ABCDEF12",
    },
]


# Mock IPOR vault data
MOCK_IPOR_VAULTS = [
    {
        "address": "0x1111111111111111111111111111111111111111",
        "name": "LlamaRisk crvUSD Optimizer",
    }
]


@pytest.fixture
def mock_env():
    """Set up mock environment variables."""
    with patch.dict(os.environ, {"PROD": "false"}):
        yield


@pytest.fixture
def mock_requests():
    """Mock requests for API calls."""
    with patch("requests.get") as mock_get:
        yield mock_get


@pytest.fixture
def mock_github_service():
    """Mock GitHub log service to avoid real API calls."""
    with patch(
        "bots.utils.github.GithubLogService"
    ) as mock_cls:
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


class TestFetchBeefyVaults:
    """Tests for _fetch_beefy_vaults() method."""

    def test_fetch_beefy_vaults_success(
        self, mock_env, mock_requests, mock_github_service
    ):
        """Verify Beefy vault mapping is populated correctly from API response."""

        def get_side_effect(url, **kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "beefy.finance" in url:
                response.json.return_value = MOCK_BEEFY_VAULTS
            else:
                response.json.return_value = MOCK_IPOR_VAULTS
            return response

        mock_requests.side_effect = get_side_effect

        # Import after mocking
        from importlib import import_module, reload

        # Clear cached module
        if "bots.onlyboost_v2.main" in sys.modules:
            del sys.modules["bots.onlyboost_v2.main"]

        main_module = import_module("bots.onlyboost_v2.main")

        # Create bot instance
        bot = main_module.OnlyBoostV2Bot()

        # Verify Beefy vaults were loaded (only active stakedao vaults)
        assert len(bot.beefy_vault_mapping) == 3

        # Verify correct vault addresses are in mapping (lowercase)
        assert (
            "0x0f59726bc1101a18cb332e176f4b0674d65ff453"
            in bot.beefy_vault_mapping
        )
        assert (
            "0xdf8ba5860deed6308463501e63fbb8659dc82f4c"
            in bot.beefy_vault_mapping
        )
        assert (
            "0xae82708d470fe681d16582ebba5fc34d6bb5775b"
            in bot.beefy_vault_mapping
        )

        # Verify label format
        vault_info = bot.beefy_vault_mapping[
            "0x0f59726bc1101a18cb332e176f4b0674d65ff453"
        ]
        assert vault_info["label"] == "Beefy sUSD/sUSDe"
        assert vault_info["chain"] == "ethereum"

    def test_fetch_beefy_vaults_filters_inactive(
        self, mock_env, mock_requests, mock_github_service
    ):
        """Verify only active vaults with platformId=stakedao are loaded."""

        def get_side_effect(url, **kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "beefy.finance" in url:
                response.json.return_value = MOCK_BEEFY_VAULTS
            else:
                response.json.return_value = MOCK_IPOR_VAULTS
            return response

        mock_requests.side_effect = get_side_effect

        from importlib import import_module

        if "bots.onlyboost_v2.main" in sys.modules:
            del sys.modules["bots.onlyboost_v2.main"]

        main_module = import_module("bots.onlyboost_v2.main")
        bot = main_module.OnlyBoostV2Bot()

        # Paused vault should NOT be in mapping
        assert (
            "0x1234567890123456789012345678901234567890"
            not in bot.beefy_vault_mapping
        )

        # Convex vault should NOT be in mapping
        assert (
            "0xabcdef1234567890abcdef1234567890abcdef12"
            not in bot.beefy_vault_mapping
        )

    def test_fetch_beefy_vaults_api_failure(
        self, mock_env, mock_requests, mock_github_service
    ):
        """Verify graceful handling when Beefy API fails."""
        import requests as req

        def get_side_effect(url, **kwargs):
            response = MagicMock()
            if "beefy.finance" in url:
                raise req.RequestException("API unavailable")
            else:
                response.raise_for_status = MagicMock()
                response.json.return_value = MOCK_IPOR_VAULTS
            return response

        mock_requests.side_effect = get_side_effect

        from importlib import import_module

        if "bots.onlyboost_v2.main" in sys.modules:
            del sys.modules["bots.onlyboost_v2.main"]

        main_module = import_module("bots.onlyboost_v2.main")

        # Should not raise exception
        bot = main_module.OnlyBoostV2Bot()

        # Mapping should be empty but bot should still work
        assert bot.beefy_vault_mapping == {}


class TestCheckBeefyTransaction:
    """Tests for check_beefy_transaction() method."""

    @pytest.fixture
    def bot_with_beefy_vaults(self, mock_env, mock_requests, mock_github_service):
        """Create bot instance with mocked Beefy vaults."""

        def get_side_effect(url, **kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "beefy.finance" in url:
                response.json.return_value = MOCK_BEEFY_VAULTS
            else:
                response.json.return_value = MOCK_IPOR_VAULTS
            return response

        mock_requests.side_effect = get_side_effect

        from importlib import import_module

        if "bots.onlyboost_v2.main" in sys.modules:
            del sys.modules["bots.onlyboost_v2.main"]

        main_module = import_module("bots.onlyboost_v2.main")
        return main_module.OnlyBoostV2Bot()

    def test_check_beefy_transaction_sender_match(self, bot_with_beefy_vaults):
        """Returns label when sender matches a Beefy vault address."""
        event = {
            "args": {
                "sender": "0x0F59726bC1101A18cB332E176f4b0674d65Ff453",
                "owner": "0x9999999999999999999999999999999999999999",
            },
            "transactionHash": "0xabc123",
        }
        vault = {"address": "0x1234"}
        mock_web3 = MagicMock()

        label = bot_with_beefy_vaults.check_beefy_transaction(event, vault, mock_web3)

        assert label == "Beefy sUSD/sUSDe"

    def test_check_beefy_transaction_owner_match(self, bot_with_beefy_vaults):
        """Returns label when owner matches a Beefy vault address."""
        event = {
            "args": {
                "sender": "0x9999999999999999999999999999999999999999",
                "owner": "0xdf8bA5860dEeD6308463501E63fBb8659Dc82F4C",
            },
            "transactionHash": "0xabc123",
        }
        vault = {"address": "0x1234"}
        mock_web3 = MagicMock()

        label = bot_with_beefy_vaults.check_beefy_transaction(event, vault, mock_web3)

        assert label == "Beefy sdCRV/CRV"

    def test_check_beefy_transaction_receiver_match(self, bot_with_beefy_vaults):
        """Returns label when receiver matches a Beefy vault address (for withdrawals)."""
        event = {
            "args": {
                "owner": "0x9999999999999999999999999999999999999999",
                "receiver": "0xaE82708d470FE681d16582EbBA5FC34D6Bb5775b",
            },
            "transactionHash": "0xabc123",
        }
        vault = {"address": "0x1234"}
        mock_web3 = MagicMock()

        label = bot_with_beefy_vaults.check_beefy_transaction(event, vault, mock_web3)

        assert label == "Beefy scrvUSD/USDC"

    def test_check_beefy_transaction_no_match(self, bot_with_beefy_vaults):
        """Returns empty string when no addresses match Beefy vaults."""
        event = {
            "args": {
                "sender": "0x9999999999999999999999999999999999999999",
                "owner": "0x8888888888888888888888888888888888888888",
            },
            "transactionHash": "0xabc123",
        }
        vault = {"address": "0x1234"}
        mock_web3 = MagicMock()

        label = bot_with_beefy_vaults.check_beefy_transaction(event, vault, mock_web3)

        assert label == ""


class TestMessageFormatting:
    """Tests for message formatting with Beefy labels."""

    def test_message_formatting_beefy_deposit(
        self, mock_env, mock_requests, mock_github_service
    ):
        """Message includes 'via Beefy {name}' for Beefy deposits."""

        def get_side_effect(url, **kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "beefy.finance" in url:
                response.json.return_value = MOCK_BEEFY_VAULTS
            else:
                response.json.return_value = MOCK_IPOR_VAULTS
            return response

        mock_requests.side_effect = get_side_effect

        from importlib import import_module

        if "bots.onlyboost_v2.main" in sys.modules:
            del sys.modules["bots.onlyboost_v2.main"]

        main_module = import_module("bots.onlyboost_v2.main")

        # Create bot instance
        bot_instance = main_module.OnlyBoostV2Bot()

        # Patch send_telegram_message in the module's namespace
        with patch.object(
            main_module, "send_telegram_message"
        ) as mock_telegram:
            # Create mock event with beefy_label
            # Using 100,000 tokens at $1/token = $100,000 (above $10k threshold)
            event = {
                "is_deposit": True,
                "is_enso": False,
                "ipor_label": "",
                "beefy_label": "Beefy sUSD/sUSDe",
                "args": {
                    "sender": "0x0F59726bC1101A18cB332E176f4b0674d65Ff453",
                    "owner": "0x9999999999999999999999999999999999999999",
                    "assets": 100000000000000000000000,  # 100,000 tokens = $100,000
                },
                "transactionHash": b"\x12\x34" * 16,
                "logIndex": 1,
                "blockNumber": 12345678,
                "vault": {
                    "chainId": 1,
                    "address": "0x1234567890123456789012345678901234567890",
                    "asset": {"name": "Test LP", "decimals": 18, "address": "0xasset"},
                    "totalSupply": "1000000000000000000000000",
                    "totalSupplyUSD": "1000000",
                    "gauge": {"address": ""},
                },
            }

            vault = event["vault"]

            # Mock web3
            mock_web3 = MagicMock()
            mock_contract = MagicMock()
            mock_contract.functions.totalSupply.return_value.call.return_value = (
                1000000000000000000000000
            )
            mock_web3.eth.contract.return_value = mock_contract
            mock_web3.to_checksum_address = lambda x: x

            # Process the event
            bot_instance.process_vault_event(vault, event, mock_web3)

            # Verify telegram was called
            assert mock_telegram.called

            # Get the message that was sent
            call_args = mock_telegram.call_args
            message = call_args[0][2]  # Third positional arg is the message

            # Verify Beefy label is in the message
            assert message.startswith("Curve | Ethereum\n")
            assert "via Beefy sUSD/sUSDe" in message
            assert "Deposited" in message

    def test_message_formatting_beefy_withdraw(
        self, mock_env, mock_requests, mock_github_service
    ):
        """Message includes 'via Beefy {name}' for Beefy withdrawals."""

        def get_side_effect(url, **kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "beefy.finance" in url:
                response.json.return_value = MOCK_BEEFY_VAULTS
            else:
                response.json.return_value = MOCK_IPOR_VAULTS
            return response

        mock_requests.side_effect = get_side_effect

        from importlib import import_module

        if "bots.onlyboost_v2.main" in sys.modules:
            del sys.modules["bots.onlyboost_v2.main"]

        main_module = import_module("bots.onlyboost_v2.main")
        bot_instance = main_module.OnlyBoostV2Bot()

        with patch.object(main_module, "send_telegram_message") as mock_telegram:
            # Using 50,000 tokens at $1/token = $50,000 (above $10k threshold)
            event = {
                "is_deposit": False,
                "is_enso": False,
                "ipor_label": "",
                "beefy_label": "Beefy sdCRV/CRV",
                "args": {
                    "owner": "0x9999999999999999999999999999999999999999",
                    "receiver": "0xdf8bA5860dEeD6308463501E63fBb8659Dc82F4C",
                    "assets": 50000000000000000000000,  # 50,000 tokens = $50,000
                },
                "transactionHash": b"\x12\x34" * 16,
                "logIndex": 1,
                "blockNumber": 12345678,
                "vault": {
                    "chainId": 1,
                    "address": "0x1234567890123456789012345678901234567890",
                    "asset": {"name": "Test LP", "decimals": 18, "address": "0xasset"},
                    "totalSupply": "1000000000000000000000000",
                    "totalSupplyUSD": "1000000",
                    "gauge": {"address": ""},
                },
            }

            vault = event["vault"]

            mock_web3 = MagicMock()
            mock_contract = MagicMock()
            mock_contract.functions.totalSupply.return_value.call.return_value = (
                1000000000000000000000000
            )
            mock_web3.eth.contract.return_value = mock_contract
            mock_web3.to_checksum_address = lambda x: x

            bot_instance.process_vault_event(vault, event, mock_web3)

            assert mock_telegram.called
            message = mock_telegram.call_args[0][2]

            assert "via Beefy sdCRV/CRV" in message
            assert "Withdraw" in message


class TestLabelPriority:
    """Tests for label priority (IPOR should take precedence over Beefy)."""

    def test_ipor_takes_priority_over_beefy(
        self, mock_env, mock_requests, mock_github_service
    ):
        """When both IPOR and Beefy labels are present, IPOR is used."""

        def get_side_effect(url, **kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "beefy.finance" in url:
                response.json.return_value = MOCK_BEEFY_VAULTS
            else:
                response.json.return_value = MOCK_IPOR_VAULTS
            return response

        mock_requests.side_effect = get_side_effect

        from importlib import import_module

        if "bots.onlyboost_v2.main" in sys.modules:
            del sys.modules["bots.onlyboost_v2.main"]

        main_module = import_module("bots.onlyboost_v2.main")
        bot_instance = main_module.OnlyBoostV2Bot()

        with patch.object(main_module, "send_telegram_message") as mock_telegram:
            # Using 100,000 tokens at $1/token = $100,000 (above $10k threshold)
            event = {
                "is_deposit": True,
                "is_enso": False,
                "ipor_label": "IPOR vault LlamaRisk crvUSD Optimizer",
                "beefy_label": "Beefy sUSD/sUSDe",  # Both labels present
                "args": {
                    "sender": "0x0F59726bC1101A18cB332E176f4b0674d65Ff453",
                    "owner": "0x9999999999999999999999999999999999999999",
                    "assets": 100000000000000000000000,  # 100,000 tokens = $100,000
                },
                "transactionHash": b"\x12\x34" * 16,
                "logIndex": 1,
                "blockNumber": 12345678,
                "vault": {
                    "chainId": 1,
                    "address": "0x1234567890123456789012345678901234567890",
                    "asset": {"name": "Test LP", "decimals": 18, "address": "0xasset"},
                    "totalSupply": "1000000000000000000000000",
                    "totalSupplyUSD": "1000000",
                    "gauge": {"address": ""},
                },
            }

            vault = event["vault"]

            mock_web3 = MagicMock()
            mock_contract = MagicMock()
            mock_contract.functions.totalSupply.return_value.call.return_value = (
                1000000000000000000000000
            )
            mock_web3.eth.contract.return_value = mock_contract
            mock_web3.to_checksum_address = lambda x: x

            bot_instance.process_vault_event(vault, event, mock_web3)

            assert mock_telegram.called
            message = mock_telegram.call_args[0][2]

            # IPOR should be in the message
            assert "via IPOR vault LlamaRisk crvUSD Optimizer" in message
            # Beefy should NOT be in the message (IPOR takes priority)
            assert "Beefy" not in message
