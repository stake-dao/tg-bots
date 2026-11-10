from types import SimpleNamespace

import pytest

from bots.utils.telegram_format import format_activity_header


def test_format_activity_header_matches_compact_preview_format():
    assert (
        format_activity_header("frxUSD", "Ethereum", "🦋")
        == "🦋 frxUSD | Ethereum\n"
    )


@pytest.mark.parametrize("address, name", [
    ("0x8EDCC305E633d29BFB383872e79401c506cE9E6f", "Stake DAO USDC"),
    ("0xCE13e39534082FCF8f13F6D84e6D95414D14271e", "Stake DAO frxUSD"),
    ("0x038d362028f89bcea7F598afb8d6Ff44b0a16708", "Frax Ecosystem Accelerator"),
])
def test_morpho_vault_header_identifies_the_vault(address, name):
    from bots.morpho.main import format_vault_header, VAULTS

    vault = next(vault for vault in VAULTS if vault.address == address)
    assert format_vault_header(vault) == f"🦋 {name} | Ethereum\n"


@pytest.mark.parametrize("withdrawal", [False, True])
def test_accelerator_deposit_and_withdrawal_alerts(monkeypatch, withdrawal):
    from bots.morpho import main as bot
    from eth_abi import encode

    address = "0x038d362028f89bcea7F598afb8d6Ff44b0a16708"
    vault = next(vault for vault in bot.VAULTS if vault.address == address)
    user = "0x" + "11" * 20
    topic_user = "0x" + "00" * 12 + "11" * 20
    tx_hash = "0x" + "ab" * 32
    topic = bot.TOPIC_VAULT_WITHDRAW if withdrawal else bot.TOPIC_VAULT_DEPOSIT
    log = {
        "topics": [topic, topic_user, topic_user] + ([topic_user] if withdrawal else []),
        # Unequal assets/shares and 18-decimal units catch the wrong amount field
        # and the VaultConfig default of six decimals.
        "data": "0x" + encode(["uint256", "uint256"], [125 * 10**16, 119 * 10**16]).hex(),
        "transactionHash": tx_hash,
        "blockNumber": "0x64",
    }
    monkeypatch.setattr(bot, "get_logs_by_address_and_topics", lambda *args: [log])
    monkeypatch.setattr(bot, "get_tx_sender", lambda *args: user)
    monkeypatch.setattr(bot, "is_safe_multisig", lambda *args: False)
    monkeypatch.setattr(bot, "get_vault_tvl", lambda *args, **kwargs: None)
    service = SimpleNamespace(get_w3=lambda chain_id: None)

    fetch = bot.fetch_vault_withdrawals if withdrawal else bot.fetch_vault_deposits
    events = fetch(vault, 100, 100, service)

    assert len(events) == 1
    message = events[0]["message"]
    assert message.startswith("🦋 Frax Ecosystem Accelerator | Ethereum\n")
    action = "🔴 Withdrew" if withdrawal else "🟢 Deposited"
    assert f"{action} 1.25 frxUSD\n" in message
    assert f"href='https://app.morpho.org/ethereum/vault/{address}'" in message
    assert f"href='https://etherscan.io/tx/{tx_hash}'" in message


def test_accelerator_reallocation_alert(monkeypatch):
    from bots.morpho import main as bot
    from eth_abi import encode

    address = "0x038d362028f89bcea7F598afb8d6Ff44b0a16708"
    vault = next(vault for vault in bot.VAULTS if vault.address == address)
    user = "0x" + "11" * 20
    topic_user = "0x" + "00" * 12 + "11" * 20
    topic_adapter = "0x" + "00" * 12 + "22" * 20
    market_id = "0xf653cb42a02c56113e954fa9370327f6665186069a68370af370c2da246e9eb1"
    tx_hash = "0x" + "ab" * 32
    allocation = {
        "address": address,
        "topics": [bot.TOPIC_ALLOCATE, topic_user, topic_adapter],
        "transactionHash": tx_hash,
        "blockNumber": "0x64",
    }
    supply = {
        "address": bot.ContractRegistry.MORPHO_BLUE[1],
        "topics": [bot.TOPIC_BLUE_SUPPLY, market_id, topic_user, topic_adapter],
        "data": "0x" + encode(["uint256", "uint256"], [125 * 10**16, 119 * 10**16]).hex(),
    }
    monkeypatch.setattr(
        bot, "get_logs_by_address_and_topics",
        lambda address, start, end, topics, chain: [allocation] if topics["0"] == bot.TOPIC_ALLOCATE else [],
    )
    monkeypatch.setattr(bot, "is_safe_multisig", lambda *args: False)
    web3 = SimpleNamespace(eth=SimpleNamespace(
        get_transaction_receipt=lambda tx: {"from": user, "logs": [allocation, supply]},
    ))
    service = SimpleNamespace(get_w3=lambda chain_id: web3)
    markets = {market_id: SimpleNamespace(collateral_symbol="stakedao-USD3frxUSD", loan_symbol="frxUSD")}

    events = bot.fetch_vault_reallocations(vault, 100, 100, service, markets)

    assert len(events) == 1
    message = events[0]["message"]
    assert message.startswith("🦋 Frax Ecosystem Accelerator | Ethereum\n🔀 Reallocated 1.25 frxUSD\n")
    assert f"href='https://app.morpho.org/ethereum/market/{market_id}'>stakedao-USD3frxUSD/frxUSD</a> (1.25)" in message
    assert f"href='https://app.morpho.org/ethereum/vault/{address}'" in message


def test_morpho_market_header_uses_pair_without_market_prefix():
    from bots.morpho.main import format_market_header

    market = SimpleNamespace(collateral_symbol="sdCRV", loan_symbol="USDC")

    assert format_market_header(market) == "🦋 sdCRV/USDC | Ethereum\n"


def test_asdcrv_header_uses_market_asset_without_lending_prefix():
    from bots.asdcrv.main import MSG_HEADER

    assert MSG_HEADER == "asdcrv | Arbitrum\n"
