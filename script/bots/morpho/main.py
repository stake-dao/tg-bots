"""
Morpho Stake DAO USDC Vault Activity Bot.

Monitors:
1. Vault events: Deposit/Withdraw on Stake DAO USDC vaults (v1 and v2)
2. Market events: Borrow/Repay/Collateral/Liquidation on Morpho Blue markets

Sends Telegram notifications for all activity.
"""

import logging
import time
from typing import Dict, List, NamedTuple, Optional

from bots.morpho.markets import MarketMetadata, fetch_stakedao_aprs, get_active_markets
from shared.services.morpho_api import get_morpho_api_service
from shared.bot_runner import get_block_range
from dotenv import load_dotenv
from shared.services.etherscan_service import get_logs_by_address_and_topics
from shared.services.web3_service import get_web3_service
from shared.address import format_eth_address
from shared.communication.telegram import send_telegram_message
from shared.constants import Common, ContractRegistry, GlobalConstants
from shared.utils.formatters import format_amount
from shared.utils.safe import format_user_info, is_safe_multisig
from bots.utils.telegram_format import format_activity_header
from shared.utils.globals import decode_hex_data, replace_double_quotes_with_single
from web3 import Web3

load_dotenv()
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

WORKFLOW_NAME = "morpho"

# =============================================================================
# Event Topic Hashes
# =============================================================================

# ERC-4626 Vault events
TOPIC_VAULT_DEPOSIT = "0xdcbc1c05240f31ff3ad067ef1ee35ce4997762752e3a095284754544f4c709d7"
TOPIC_VAULT_WITHDRAW = "0xfbde797d201c681b91056529119e0b02407c7bb96a4a2c75c01fc9667232c8db"

# Morpho Blue market events
TOPIC_BORROW = "0x570954540bed6b1304a87dfe815a5eda4a648f7097a16240dcd85c9b5fd42a43"
TOPIC_REPAY = "0x52acb05cebbd3cd39715469f22afbf5a17496295ef3bc9bb5944056c63ccaa09"
TOPIC_SUPPLY_COLLATERAL = "0xa3b9472a1399e17e123f3c2e6586c23e504184d504de59cdaa2b375e880c6184"
TOPIC_WITHDRAW_COLLATERAL = "0xe80ebd7cc9223d7382aab2e0d1d6155c65651f83d53c8b9b06901d167e321142"
TOPIC_LIQUIDATE = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"

# Morpho Vault V2 reallocation events (emitted on the vault address). Their bytes32[]
# ids are adapter-internal and do NOT map to Blue market ids — they only flag that a
# reallocation happened and name the allocator/adapter.
TOPIC_ALLOCATE = "0x2bc7948a96a066968d2a58aaf46eb0b305aa166b1d1951d2f7ef0919746b8c2a"
TOPIC_DEALLOCATE = "0xd602b36fb24934aef1bc2a658de029b486fa4c664a6e45de1f48e3fd1be25dd9"

# Morpho Blue liquidity events. A reallocation routes through Morpho Blue, so its
# Supply/Withdraw legs carry the REAL market id in topics[1] (and the moved assets in
# data) — this is what resolves reallocation markets to working links.
TOPIC_BLUE_SUPPLY = "0xedf8870433c83823eb071d3df1caa8d008f12f6440918c20d75a3602cda30fe0"
TOPIC_BLUE_WITHDRAW = "0xa56fc0ad5702ec05ce63666221f796fb62437c32db1aa1aa075fc6484cf58fbf"

ETHERSCAN_URL = "https://etherscan.io"
USDC_DECIMALS = 6

# ERC-4626 totalAssets ABI (for TVL)
TOTAL_ASSETS_ABI = [
    {
        "inputs": [],
        "name": "totalAssets",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


# =============================================================================
# Vault Configuration
# =============================================================================

class VaultConfig(NamedTuple):
    address: str
    name: str
    symbol: str
    morpho_url: str
    asset_symbol: str = "USDC"
    asset_decimals: int = 6


VAULTS = [
    VaultConfig(
        address=ContractRegistry.STAKEDAO_MORPHO_USDC_V2[1],
        name="Stake DAO USDC",
        symbol="Stake DAO USDC",
        morpho_url="https://app.morpho.org/ethereum/vault/0x8EDCC305E633d29BFB383872e79401c506cE9E6f",
        asset_symbol="USDC",
        asset_decimals=6,
    ),
    VaultConfig(
        address=ContractRegistry.STAKEDAO_MORPHO_FRXUSD_V2[1],
        name="Stake DAO frxUSD",
        symbol="Stake DAO frxUSD",
        morpho_url="https://app.morpho.org/ethereum/vault/0xCE13e39534082FCF8f13F6D84e6D95414D14271e/stake-dao-frxusd-v2",
        asset_symbol="frxUSD",
        asset_decimals=18,
    ),
]

MORPHO_MARKET_URL = "https://app.morpho.org/ethereum/market"


# =============================================================================
# Helper Functions
# =============================================================================

def get_size_circles(amount_usd: float, is_deposit: bool) -> str:
    """Return repeated circle emoji based on deposit/withdrawal USD size."""
    if amount_usd >= 500_000:
        count = 3
    elif amount_usd >= 100_000:
        count = 2
    else:
        count = 1
    return ("🟢" if is_deposit else "🔴") * count


def get_vault_tvl(vault: VaultConfig, web3, block_identifier="latest") -> Optional[float]:
    """Fetch vault totalAssets at a specific block and return TVL in USD (stablecoin = 1:1)."""
    try:
        contract = web3.eth.contract(
            address=Web3.to_checksum_address(vault.address),
            abi=TOTAL_ASSETS_ABI,
        )
        total_assets = contract.functions.totalAssets().call(block_identifier=block_identifier)
        return total_assets / (10 ** vault.asset_decimals)
    except Exception as e:
        logging.warning(f"Failed to fetch TVL for {vault.name} at block {block_identifier}: {e}")
        return None


def get_tx_sender(tx_hash: str, web3_service) -> str:
    """Get the actual transaction sender (tx.from) for a transaction."""
    try:
        web3 = web3_service.get_w3(1)
        tx = web3.eth.get_transaction(tx_hash)
        return tx["from"]
    except Exception:
        return None


def _log_hex(value) -> str:
    """Normalize a receipt log topic/data field (HexBytes or str) to lowercase 0x hex."""
    h = value.hex() if hasattr(value, "hex") else value
    if not h.startswith("0x"):
        h = "0x" + h
    return h.lower()


def receipt_emits_vault_deposit_or_withdraw(receipt, vault_addr: str) -> bool:
    """True if the receipt carries an ERC-4626 Deposit/Withdraw from the vault.

    Deposits/withdrawals route liquidity through the vault adapter and also emit
    Allocate/Deallocate plus Morpho Blue Supply/Withdraw, so they look like a
    reallocation. Only a curator reallocation has no ERC-4626 Deposit/Withdraw in the
    tx. Read it from the receipt (atomic) rather than a separate ranged log query that
    can transiently return empty and let a deposit leak through as a "reallocation".
    """
    vault = vault_addr.lower()
    targets = {TOPIC_VAULT_DEPOSIT, TOPIC_VAULT_WITHDRAW}
    for log in receipt["logs"]:
        if log["address"].lower() == vault and _log_hex(log["topics"][0]) in targets:
            return True
    return False


def reallocation_legs(receipt, adapters: set, blue_addr: str):
    """Resolve a reallocation's market legs from the Morpho Blue events in the receipt.

    Each Blue Withdraw by the vault's adapter is a "from" market and each Blue Supply a
    "to". Both carry the real Blue market id in topics[1] (so the links resolve) and the
    moved assets in their data. Restricting to ``adapters`` (this vault's adapter
    addresses, from its Allocate/Deallocate events) keeps a batched multi-vault tx from
    cross-reporting other vaults' legs.

    Returns (froms, tos), each a list of (market_id_hex, assets).
    """
    froms, tos = [], []
    for log in receipt["logs"]:
        if log["address"].lower() != blue_addr:
            continue
        topic0 = _log_hex(log["topics"][0])
        if topic0 == TOPIC_BLUE_WITHDRAW:
            on_behalf = "0x" + _log_hex(log["topics"][2])[-40:]  # [id, onBehalf, receiver]
            assets_word, bucket = 1, froms  # data = [caller, assets, shares]
        elif topic0 == TOPIC_BLUE_SUPPLY:
            on_behalf = "0x" + _log_hex(log["topics"][3])[-40:]  # [id, caller, onBehalf]
            assets_word, bucket = 0, tos  # data = [assets, shares]
        else:
            continue
        if on_behalf not in adapters:
            continue
        market_id = _log_hex(log["topics"][1])
        data = _log_hex(log["data"])[2:]
        assets = int(data[assets_word * 64:(assets_word + 1) * 64], 16)
        bucket.append((market_id, assets))
    return froms, tos


def is_valid_market(log: Dict, active_markets: Dict[str, MarketMetadata]) -> tuple:
    """Check if event belongs to an active market."""
    if len(log["topics"]) < 2:
        return False, None
    market_id_hex = log["topics"][1]
    if market_id_hex in active_markets:
        return True, active_markets[market_id_hex]
    return False, None


def format_apr_line(
    collateral_apr: Optional[float], borrow_apr: Optional[float]
) -> Optional[str]:
    """Format the APR line for market events."""
    parts = []
    if collateral_apr is not None:
        parts.append(f"Collateral APR: {collateral_apr * 100:.2f}%")
    if borrow_apr is not None:
        parts.append(f"Borrow APR: {borrow_apr * 100:.2f}%")

    if parts:
        return " | ".join(parts)
    return None


def format_vault_header(vault: VaultConfig) -> str:
    return format_activity_header(vault.asset_symbol, "Ethereum", "🦋")


def format_market_header(market: MarketMetadata) -> str:
    return format_activity_header(
        f"{market.collateral_symbol}/{market.loan_symbol}", "Ethereum", "🦋"
    )


# =============================================================================
# Vault Event Fetchers
# =============================================================================
# Safe Detection Pattern:
#   owner = "0x" + log["topics"][N][-40:]  # Get owner from indexed event param
#   executor = get_tx_sender(tx_hash, web3_service)  # Get tx.from
#   is_safe = is_safe_multisig(owner, web3)
#   message += format_user_info(owner, executor, is_safe, ETHERSCAN_URL)
# =============================================================================

def fetch_vault_deposits(
    vault: VaultConfig,
    from_block: int,
    to_block: int,
    web3_service,
    vault_apy: float = None,
) -> List[Dict]:
    """Fetch Deposit events from a vault."""
    logs = get_logs_by_address_and_topics(
        Web3.to_checksum_address(vault.address),
        from_block, to_block,
        {"0": TOPIC_VAULT_DEPOSIT},
        1,
    )

    web3 = web3_service.get_w3(1)
    events = []
    for log in logs:
        tx_hash = log["transactionHash"]

        # Get owner from event (indexed parameter) and executor from tx.from
        owner = "0x" + log["topics"][2][-40:]
        executor = get_tx_sender(tx_hash, web3_service)
        is_safe = is_safe_multisig(owner, web3)

        data = log["data"][2:]
        res = decode_hex_data(data, ["uint256", "uint256"])
        assets = res[0]
        amount = assets / (10 ** vault.asset_decimals)
        formatted_assets = format_amount(assets, vault.asset_decimals)

        # Stablecoin: amount ≈ USD value
        circles = get_size_circles(amount, is_deposit=True)

        # Fetch TVL at the event block so it reflects the post-deposit state
        block_number = int(log["blockNumber"], 16) if isinstance(log["blockNumber"], str) else log["blockNumber"]
        vault_tvl = get_vault_tvl(vault, web3, block_identifier=block_number)

        message = format_vault_header(vault)
        message += f"{circles} Deposited {formatted_assets} {vault.asset_symbol}\n"
        if vault_apy is not None:
            message += f"Supply APY: {vault_apy * 100:.2f}%\n"
        if vault_tvl is not None:
            message += f"TVL ${format_amount(vault_tvl)}\n"
        message += format_user_info(owner, executor, is_safe, ETHERSCAN_URL)
        message += f"Links: <a href='{vault.morpho_url}'>Vault</a> | <a href='{ETHERSCAN_URL}/tx/{tx_hash}'>Tx</a>"

        events.append({"message": message, "blockNumber": log["blockNumber"]})

    return events


def fetch_vault_withdrawals(
    vault: VaultConfig,
    from_block: int,
    to_block: int,
    web3_service,
    vault_apy: float = None,
) -> List[Dict]:
    """Fetch Withdraw events from a vault."""
    logs = get_logs_by_address_and_topics(
        Web3.to_checksum_address(vault.address),
        from_block, to_block,
        {"0": TOPIC_VAULT_WITHDRAW},
        1,
    )

    web3 = web3_service.get_w3(1)
    events = []
    for log in logs:
        tx_hash = log["transactionHash"]

        # Get owner from event (indexed parameter) and executor from tx.from
        # For Withdraw events: topic[3] is owner
        owner = "0x" + log["topics"][3][-40:]
        executor = get_tx_sender(tx_hash, web3_service)
        is_safe = is_safe_multisig(owner, web3)

        data = log["data"][2:]
        res = decode_hex_data(data, ["uint256", "uint256"])
        assets = res[0]
        amount = assets / (10 ** vault.asset_decimals)
        formatted_assets = format_amount(assets, vault.asset_decimals)

        # Stablecoin: amount ≈ USD value
        circles = get_size_circles(amount, is_deposit=False)

        # Fetch TVL at the event block so it reflects the post-withdrawal state
        block_number = int(log["blockNumber"], 16) if isinstance(log["blockNumber"], str) else log["blockNumber"]
        vault_tvl = get_vault_tvl(vault, web3, block_identifier=block_number)

        message = format_vault_header(vault)
        message += f"{circles} Withdrew {formatted_assets} {vault.asset_symbol}\n"
        if vault_apy is not None:
            message += f"Supply APY: {vault_apy * 100:.2f}%\n"
        if vault_tvl is not None:
            message += f"TVL ${format_amount(vault_tvl)}\n"
        message += format_user_info(owner, executor, is_safe, ETHERSCAN_URL)
        message += f"Links: <a href='{vault.morpho_url}'>Vault</a> | <a href='{ETHERSCAN_URL}/tx/{tx_hash}'>Tx</a>"

        events.append({"message": message, "blockNumber": log["blockNumber"]})

    return events


def format_market_ref(market_id_hex: str, active_markets: Dict[str, MarketMetadata]) -> str:
    """HTML link for a Morpho Blue market id, labelled with its symbols when known.

    ``market_id_hex`` is a real Blue market id (read from the reallocation's Blue
    Supply/Withdraw legs), so the URL always resolves; the label falls back to a
    shortened id when the market isn't in ``active_markets``.
    """
    market = active_markets.get(market_id_hex)
    label = (
        f"{market.collateral_symbol}/{market.loan_symbol}"
        if market is not None
        else f"{market_id_hex[:10]}…"
    )
    return f"<a href='{MORPHO_MARKET_URL}/{market_id_hex}'>{label}</a>"


def fetch_vault_reallocations(
    vault: VaultConfig,
    from_block: int,
    to_block: int,
    web3_service,
    active_markets: Dict[str, MarketMetadata],
) -> List[Dict]:
    """Fetch curator reallocations on a Vault V2 and build one message per tx.

    A reallocation emits Allocate/Deallocate on the vault (the trigger + allocator,
    with no ERC-4626 Deposit/Withdraw) and routes the funds through Morpho Blue, which
    emits Supply/Withdraw carrying the real market ids. We detect candidate txs from the
    vault events, then read one receipt per tx to (a) exclude deposits/withdrawals and
    (b) resolve the actual market legs from the Blue events.
    """
    vault_addr = Web3.to_checksum_address(vault.address)

    alloc_logs = get_logs_by_address_and_topics(
        vault_addr, from_block, to_block, {"0": TOPIC_ALLOCATE}, 1
    )
    dealloc_logs = get_logs_by_address_and_topics(
        vault_addr, from_block, to_block, {"0": TOPIC_DEALLOCATE}, 1
    )
    if not alloc_logs and not dealloc_logs:
        return []

    web3 = web3_service.get_w3(1)
    blue_addr = ContractRegistry.MORPHO_BLUE[1].lower()

    # Per tx: the allocator (topics[1]) and the adapters it moved through (topics[2]).
    txs: Dict[str, Dict] = {}
    for log in alloc_logs + dealloc_logs:
        entry = txs.setdefault(
            log["transactionHash"],
            {
                "sender": "0x" + log["topics"][1][-40:],
                "blockNumber": log["blockNumber"],
                "adapters": set(),
            },
        )
        entry["adapters"].add(("0x" + log["topics"][2][-40:]).lower())

    events = []
    for tx_hash, entry in txs.items():
        try:
            receipt = web3.eth.get_transaction_receipt(tx_hash)
        except Exception as e:  # fail closed: can't confirm a curator move, so suppress
            logging.warning(f"Could not fetch receipt for {tx_hash}: {e}")
            continue
        if receipt_emits_vault_deposit_or_withdraw(receipt, vault_addr):
            continue  # deposit/withdraw, not a curator reallocation

        froms, tos = reallocation_legs(receipt, entry["adapters"], blue_addr)
        if not froms and not tos:
            continue  # not a deposit, but no Blue market legs to report

        sender = entry["sender"]
        executor = receipt.get("from")  # tx.from, already on the receipt we fetched
        is_safe = is_safe_multisig(sender, web3)

        # Headline amount = total supplied (else total withdrawn).
        moved = sum(a for _, a in tos) or sum(a for _, a in froms)

        message = format_vault_header(vault)
        message += f"🔀 Reallocated {format_amount(moved, vault.asset_decimals)} {vault.asset_symbol}\n"
        for market_id, assets in froms:
            message += f"🔴 From {format_market_ref(market_id, active_markets)} ({format_amount(assets, vault.asset_decimals)})\n"
        for market_id, assets in tos:
            message += f"🟢 To {format_market_ref(market_id, active_markets)} ({format_amount(assets, vault.asset_decimals)})\n"
        message += format_user_info(sender, executor, is_safe, ETHERSCAN_URL)
        message += f"Links: <a href='{vault.morpho_url}'>Vault</a> | <a href='{ETHERSCAN_URL}/tx/{tx_hash}'>Tx</a>"

        events.append({"message": message, "blockNumber": entry["blockNumber"]})

    return events


# =============================================================================
# Market Event Fetchers
# =============================================================================

class MarketEventConfig(NamedTuple):
    topic: str
    user_topic_index: int
    decode_types: List[str]
    amount_result_index: int
    use_collateral: bool
    emoji: str
    verb: str
    suffix: str


MARKET_EVENT_CONFIGS = [
    MarketEventConfig(TOPIC_BORROW, 2, ["address", "uint256", "uint256"], 1, False, "🔵", "Borrowed", ""),
    MarketEventConfig(TOPIC_REPAY, 3, ["uint256", "uint256"], 0, False, "🟡", "Repaid", ""),
    MarketEventConfig(TOPIC_SUPPLY_COLLATERAL, 3, ["uint256"], 0, True, "🟢", "Deposited", " collateral"),
    MarketEventConfig(TOPIC_WITHDRAW_COLLATERAL, 2, ["address", "uint256"], 1, True, "🔴", "Withdrew", " collateral"),
]


def fetch_market_events(
    config: MarketEventConfig,
    from_block: int,
    to_block: int,
    active_markets: Dict[str, MarketMetadata],
    web3_service,
    borrow_apys: Dict[str, float] = None,
) -> List[Dict]:
    """Fetch market events from Morpho Blue using a config-driven approach."""
    logs = get_logs_by_address_and_topics(
        Web3.to_checksum_address(ContractRegistry.MORPHO_BLUE[1]),
        from_block, to_block,
        {"0": config.topic},
        1,
    )

    web3 = web3_service.get_w3(1)
    events = []
    for log in logs:
        is_valid, market = is_valid_market(log, active_markets)
        if not is_valid or not market.is_stakedao_collateral:
            continue

        tx_hash = log["transactionHash"]
        on_behalf = "0x" + log["topics"][config.user_topic_index][-40:]
        executor = get_tx_sender(tx_hash, web3_service)
        is_safe = is_safe_multisig(on_behalf, web3)

        data = log["data"][2:]
        res = decode_hex_data(data, config.decode_types)
        assets = res[config.amount_result_index]
        decimals = market.collateral_decimals if config.use_collateral else market.loan_decimals
        symbol = market.collateral_symbol if config.use_collateral else market.loan_symbol
        formatted_assets = format_amount(assets, decimals)

        borrow_apr = borrow_apys.get(market.id_hex) if borrow_apys else None

        message = format_market_header(market)
        message += f"{config.emoji} {config.verb} {formatted_assets} {symbol}{config.suffix}\n"
        apr_line = format_apr_line(market.collateral_apr, borrow_apr)
        if apr_line:
            message += f"{apr_line}\n"
        message += format_user_info(on_behalf, executor, is_safe, ETHERSCAN_URL)
        message += f"Links: <a href='{MORPHO_MARKET_URL}/{market.id_hex}'>Market</a> | <a href='{ETHERSCAN_URL}/tx/{tx_hash}'>Tx</a>"

        events.append({"message": message, "blockNumber": log["blockNumber"]})

    return events


def fetch_liquidations(
    from_block: int,
    to_block: int,
    active_markets: Dict[str, MarketMetadata],
    web3_service,
    borrow_apys: Dict[str, float] = None,
) -> List[Dict]:
    """Fetch Liquidate events from Morpho Blue."""
    logs = get_logs_by_address_and_topics(
        Web3.to_checksum_address(ContractRegistry.MORPHO_BLUE[1]),
        from_block, to_block,
        {"0": TOPIC_LIQUIDATE},
        1,
    )

    web3 = web3_service.get_w3(1)
    events = []
    for log in logs:
        is_valid, market = is_valid_market(log, active_markets)
        if not is_valid:
            continue

        if not market.is_stakedao_collateral:
            continue

        tx_hash = log["transactionHash"]
        executor = get_tx_sender(tx_hash, web3_service)
        liquidator = "0x" + log["topics"][2][-40:]
        borrower = "0x" + log["topics"][3][-40:]
        is_borrower_safe = is_safe_multisig(borrower, web3)
        is_liquidator_safe = is_safe_multisig(liquidator, web3)

        data = log["data"][2:]
        res = decode_hex_data(data, ["uint256", "uint256", "uint256", "uint256", "uint256"])
        repaid_assets = res[0]
        seized_assets = res[2]

        formatted_repaid = format_amount(repaid_assets, market.loan_decimals)
        formatted_seized = format_amount(seized_assets, market.collateral_decimals)

        # Format borrower line
        if is_borrower_safe:
            borrower_formatted = format_eth_address(borrower)
            borrower_line = f"🔒 Borrower (Safe): <a href='{ETHERSCAN_URL}/address/{borrower}'>{borrower_formatted}</a>\n"
        else:
            borrower_formatted = format_eth_address(borrower)
            borrower_line = f"Borrower: <a href='{ETHERSCAN_URL}/address/{borrower}'>{borrower_formatted}</a>\n"

        # Format liquidator line
        if is_liquidator_safe:
            liquidator_formatted = format_eth_address(liquidator)
            executor_formatted = format_eth_address(executor) if executor else "Unknown"
            liquidator_line = f"🔒 Liquidator (Safe): <a href='{ETHERSCAN_URL}/address/{liquidator}'>{liquidator_formatted}</a> | Executor: <a href='{ETHERSCAN_URL}/address/{executor}'>{executor_formatted}</a>\n"
        else:
            liquidator_formatted = format_eth_address(liquidator)
            liquidator_line = f"Liquidator: <a href='{ETHERSCAN_URL}/address/{liquidator}'>{liquidator_formatted}</a>\n"

        message = format_market_header(market)
        message += f"🚨 LIQUIDATION\n"
        message += f"Repaid: {formatted_repaid} {market.loan_symbol}\n"
        message += f"Seized: {formatted_seized} {market.collateral_symbol}\n"
        message += borrower_line
        message += liquidator_line
        message += f"Links: <a href='{MORPHO_MARKET_URL}/{market.id_hex}'>Market</a> | <a href='{ETHERSCAN_URL}/tx/{tx_hash}'>Tx</a>"

        events.append({"message": message, "blockNumber": log["blockNumber"]})

    return events


# =============================================================================
# Main Job
# =============================================================================

def job():
    """Main job function to fetch events and send notifications."""
    web3_service = get_web3_service(1)
    web3 = web3_service.get_w3(1)

    from_block, current_block = get_block_range(WORKFLOW_NAME, 1, web3)

    # TEMP
    # from_block = 24683889
    # current_block = 24683900

    if from_block == 0:
        logging.info("No previous block found, skipping")
        return

    logging.info(f"Fetching events from block {from_block} to {current_block}")

    # ----- Fetch APY/APR data upfront -----
    logging.info("Fetching APY/APR data...")
    morpho_api = get_morpho_api_service()

    # Fetch vault APYs (1 API call per vault)
    vault_apys: Dict[str, float] = {}
    for vault in VAULTS:
        apy = morpho_api.get_vault_apy(vault.address)
        if apy is not None:
            vault_apys[vault.address] = apy
            logging.info(f"  {vault.name} Supply APY: {apy * 100:.2f}%")
        else:
            logging.warning(f"  {vault.name} Supply APY: unavailable")

    all_events = []

    # ----- Vault Events -----
    for vault in VAULTS:
        logging.info(f"Fetching vault events for {vault.name}")
        vault_apy = vault_apys.get(vault.address)
        deposits = fetch_vault_deposits(
            vault, from_block, current_block, web3_service, vault_apy
        )
        withdrawals = fetch_vault_withdrawals(
            vault, from_block, current_block, web3_service, vault_apy
        )
        logging.info(f"  Deposits: {len(deposits)}, Withdrawals: {len(withdrawals)}")
        all_events.extend(deposits)
        all_events.extend(withdrawals)

    # ----- Market Events -----
    logging.info("Fetching market events from Morpho Blue")

    # Fetch Stake DAO APRs (1 API call)
    stakedao_aprs = fetch_stakedao_aprs()
    logging.info(f"  Fetched Stake DAO APRs for {len(stakedao_aprs)} vaults")

    active_markets = get_active_markets(
        web3_service,
        stakedao_aprs,
        discovery_vaults=[
            ContractRegistry.STAKEDAO_MORPHO_USDC_V1[1],
            ContractRegistry.STAKEDAO_MORPHO_FRXUSD_V1[1],
        ],
    )
    logging.info(f"  Active markets: {len(active_markets)}")

    # Log collateral APRs
    for market_id, market in active_markets.items():
        if market.collateral_apr is not None:
            logging.info(
                f"  {market.collateral_symbol} Collateral APR: {market.collateral_apr * 100:.2f}%"
            )

    # ----- Vault Reallocations (allocator moves funds between markets) -----
    for vault in VAULTS:
        reallocations = fetch_vault_reallocations(
            vault, from_block, current_block, web3_service, active_markets
        )
        logging.info(f"  Reallocations ({vault.name}): {len(reallocations)}")
        all_events.extend(reallocations)

    # Fetch borrow APYs for all active markets (1 batch API call)
    borrow_apys: Dict[str, float] = {}
    if active_markets:
        market_ids = list(active_markets.keys())
        borrow_apys = morpho_api.get_markets_borrow_apy_batch(market_ids)
        for market_id, apy in borrow_apys.items():
            if apy is not None:
                market = active_markets[market_id]
                logging.info(
                    f"  {market.collateral_symbol}/{market.loan_symbol} Borrow APR: {apy * 100:.2f}%"
                )

    if active_markets:
        for config in MARKET_EVENT_CONFIGS:
            events = fetch_market_events(
                config, from_block, current_block, active_markets, web3_service, borrow_apys
            )
            logging.info(f"  {config.verb}: {len(events)}")
            all_events.extend(events)

        liquidations = fetch_liquidations(from_block, current_block, active_markets, web3_service, borrow_apys)
        logging.info(f"  Liquidations: {len(liquidations)}")
        all_events.extend(liquidations)

    # Sort by block number
    all_events.sort(
        key=lambda x: int(x["blockNumber"], 16)
        if isinstance(x["blockNumber"], str)
        else x["blockNumber"]
    )

    logging.info(f"Total events to send: {len(all_events)}")

    # Send messages with rate limit handling
    for i, event in enumerate(all_events):
        send_telegram_message(
            GlobalConstants.BOT_API_KEY,
            GlobalConstants.TELEGRAM_ACTIVITY_CHANNEL_ID,
            replace_double_quotes_with_single(event["message"]),
            parse_mode="HTML",
        )
        """ TESTING ONLY
        send_telegram_message(
            Common.TEST_TELEGRAM_API_KEY,
            Common.TEST_TELEGRAM_CHAT_ID,
            replace_double_quotes_with_single(event["message"]),
            parse_mode="HTML",
        )
        END TESTING ONLY """

        # Add delay between messages to avoid Telegram rate limits
        if i < len(all_events) - 1:
            time.sleep(1.5)


def main():
    """Entry point with error handling."""
    try:
        job()
    except Exception as e:
        logging.error(f"Error in morpho bot: {e}")
        raise


if __name__ == "__main__":
    main()
