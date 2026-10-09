"""
Morpho market discovery and metadata caching.

Discovers markets from two sources:
1. V1 MetaMorpho vault supply queues (on-chain)
2. Morpho GraphQL API (catches V2 vault allocations that don't expose supply queues)
"""

import logging
from typing import Dict, List, NamedTuple, Optional

import requests
from shared.services.data_hub_service import fetch_vault_aprs
from shared.services.web3_service import Web3Service, get_web3_service
from shared.constants import ContractRegistry
from web3 import Web3

MORPHO_GRAPHQL_URL = "https://api.morpho.org/graphql"


class MarketParams(NamedTuple):
    """Market parameters from Morpho Blue."""

    id: bytes
    loan_token: str
    collateral_token: str


class MarketMetadata(NamedTuple):
    """Cached market metadata including token info."""

    id: bytes
    id_hex: str
    loan_token: str
    loan_symbol: str
    loan_decimals: int
    collateral_token: str
    collateral_symbol: str
    collateral_decimals: int
    cap: int
    enabled: bool
    collateral_apr: Optional[float] = None  # APR for Stake DAO LP collateral
    is_stakedao_collateral: bool = False


def get_vault_market_ids(web3_service: Web3Service, vault_address: str) -> List[bytes]:
    """
    Fetch all market IDs from the vault's supply queue.

    Returns:
        List of market IDs as bytes32.
    """
    vault = web3_service.get_contract(
        vault_address,
        "morpho-vault",
        1,
    )

    queue_length = vault.functions.supplyQueueLength().call()
    market_ids = []

    for i in range(queue_length):
        market_id = vault.functions.supplyQueue(i).call()
        market_ids.append(market_id)

    return market_ids


def get_market_config(web3_service: Web3Service, market_id: bytes, vault_address: str) -> tuple:
    """
    Fetch market config (cap, enabled, removableAt) from vault.

    Returns:
        Tuple of (cap, enabled, removableAt).
    """
    vault = web3_service.get_contract(
        vault_address,
        "morpho-vault",
        1,
    )

    return vault.functions.config(market_id).call()


def get_market_params(web3_service: Web3Service, market_id: bytes) -> MarketParams:
    """
    Fetch market parameters from Morpho Blue.

    Returns:
        MarketParams with loan token, collateral token, oracle, irm, lltv.
    """
    morpho = web3_service.get_contract(
        ContractRegistry.MORPHO_BLUE[1],
        "morpho-blue",
        1,
    )

    params = morpho.functions.idToMarketParams(market_id).call()

    return MarketParams(
        id=market_id,
        loan_token=params[0],
        collateral_token=params[1],
    )


def get_reward_vault_address(
    collateral_token: str, web3_service: Web3Service
) -> Optional[str]:
    """
    Get the REWARD_VAULT address from a Morpho collateral token.

    The Stake DAO Morpho collateral tokens have a REWARD_VAULT() function
    that returns the staking-v2 vault address used for APR matching.

    Args:
        collateral_token: The collateral token address
        web3_service: Web3 service instance

    Returns:
        The reward vault address, or None if not available
    """
    abi = [
        {
            "name": "REWARD_VAULT",
            "inputs": [],
            "outputs": [{"type": "address"}],
            "stateMutability": "view",
            "type": "function",
        }
    ]

    try:
        web3 = web3_service.get_w3(1)
        contract = web3.eth.contract(
            address=Web3.to_checksum_address(collateral_token), abi=abi
        )
        return contract.functions.REWARD_VAULT().call()
    except Exception:
        return None


def fetch_stakedao_aprs() -> Dict[str, float]:
    """
    Fetch APRs for all Stake DAO vaults from DataHub.

    Returns:
        Dictionary mapping vault address (lowercase) to total APR (decimal).
    """
    try:
        return fetch_vault_aprs(chain_id=1)
    except Exception as e:
        logging.warning(f"Failed to fetch DataHub APRs: {e}")
        return {}


def discover_stakedao_markets_from_api() -> List[Dict]:
    """
    Discover Stake DAO collateral markets via Morpho GraphQL API.

    This catches markets allocated by V2 vaults that don't expose
    supply queue functions (supplyQueueLength reverts).
    """
    query = """
    {
        markets(where: { search: "stakedao" }, first: 50) {
            items {
                marketId
                collateralAsset { address symbol decimals }
                loanAsset { address symbol decimals }
            }
        }
    }
    """
    response = requests.post(MORPHO_GRAPHQL_URL, json={"query": query}, timeout=30)
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(f"Morpho market discovery query failed: {data['errors']}")
    response.raise_for_status()
    return data["data"]["markets"]["items"]


def get_active_markets(
    web3_service: Optional[Web3Service] = None,
    stakedao_aprs: Optional[Dict[str, float]] = None,
    discovery_vaults: Optional[List[str]] = None,
) -> Dict[str, MarketMetadata]:
    """
    Get all active markets from V1 supply queues + Morpho API discovery.

    Args:
        web3_service: Web3 service instance
        stakedao_aprs: Pre-fetched Stake DAO vault APRs (optional, will fetch if None)
        discovery_vaults: List of V1 vault addresses to read supply queues from.
                          Defaults to USDC V1 only for backwards compatibility.

    Returns:
        Dictionary mapping market ID hex to MarketMetadata.
    """
    if web3_service is None:
        web3_service = get_web3_service(1)

    if discovery_vaults is None:
        discovery_vaults = [ContractRegistry.STAKEDAO_MORPHO_USDC_V1[1]]

    active_markets: Dict[str, MarketMetadata] = {}

    # Collect all token addresses for batch fetching
    token_addresses = set()

    # First pass: filter active markets and collect token addresses
    market_params_list = []
    seen_market_ids: set = set()
    for vault_address in discovery_vaults:
        market_ids = get_vault_market_ids(web3_service, vault_address)
        for market_id in market_ids:
            if market_id in seen_market_ids:
                continue
            seen_market_ids.add(market_id)

            cap, enabled, _ = get_market_config(web3_service, market_id, vault_address)

            # Skip markets with zero cap or disabled
            if cap == 0 or not enabled:
                continue

            params = get_market_params(web3_service, market_id)
            market_params_list.append((market_id, params, cap, enabled))

            token_addresses.add(params.loan_token)
            token_addresses.add(params.collateral_token)

    # Batch fetch token metadata using web3_service (uses multicall + caching)
    if token_addresses:
        token_info = web3_service.get_token_info(
            list(token_addresses), 1
        )
    else:
        token_info = {}

    # Fetch Stake DAO APRs if not provided
    if stakedao_aprs is None:
        stakedao_aprs = fetch_stakedao_aprs()

    # Get reward vault addresses for collateral tokens
    collateral_to_vault: Dict[str, str] = {}
    for _, params, _, _ in market_params_list:
        if params.collateral_token not in collateral_to_vault:
            vault_addr = get_reward_vault_address(params.collateral_token, web3_service)
            if vault_addr:
                collateral_to_vault[params.collateral_token] = vault_addr.lower()

    # Second pass: build market metadata with collateral APR
    for market_id, params, cap, enabled in market_params_list:
        loan_info = token_info.get(params.loan_token, {})
        collateral_info = token_info.get(params.collateral_token, {})

        # Get collateral APR from Stake DAO vault
        collateral_apr = None
        vault_addr = collateral_to_vault.get(params.collateral_token)
        if vault_addr and vault_addr in stakedao_aprs:
            collateral_apr = stakedao_aprs[vault_addr]

        market_id_hex = "0x" + market_id.hex()

        active_markets[market_id_hex] = MarketMetadata(
            id=market_id,
            id_hex=market_id_hex,
            loan_token=params.loan_token,
            loan_symbol=loan_info.get("symbol", "UNKNOWN"),
            loan_decimals=loan_info.get("decimals", 18),
            collateral_token=params.collateral_token,
            collateral_symbol=collateral_info.get("symbol", "UNKNOWN"),
            collateral_decimals=collateral_info.get("decimals", 18),
            cap=cap,
            enabled=enabled,
            collateral_apr=collateral_apr,
            is_stakedao_collateral=params.collateral_token in collateral_to_vault,
        )

    # --- API discovery: catch markets from V2 vaults ---
    api_markets = discover_stakedao_markets_from_api()
    for api_market in api_markets:
        market_id_hex = api_market["marketId"].lower()
        if market_id_hex in active_markets:
            continue

        collateral = api_market["collateralAsset"]
        loan = api_market["loanAsset"]
        collateral_token = Web3.to_checksum_address(collateral["address"])

        # Verify it's a Stake DAO collateral (has REWARD_VAULT)
        if collateral_token not in collateral_to_vault:
            vault_addr = get_reward_vault_address(collateral_token, web3_service)
            if vault_addr:
                collateral_to_vault[collateral_token] = vault_addr.lower()
            else:
                continue

        collateral_apr = None
        vault_addr = collateral_to_vault.get(collateral_token)
        if vault_addr and vault_addr in stakedao_aprs:
            collateral_apr = stakedao_aprs[vault_addr]

        market_id = bytes.fromhex(market_id_hex[2:])

        active_markets[market_id_hex] = MarketMetadata(
            id=market_id,
            id_hex=market_id_hex,
            loan_token=Web3.to_checksum_address(loan["address"]),
            loan_symbol=loan.get("symbol", "UNKNOWN"),
            loan_decimals=loan.get("decimals", 18),
            collateral_token=collateral_token,
            collateral_symbol=collateral.get("symbol", "UNKNOWN"),
            collateral_decimals=collateral.get("decimals", 18),
            cap=0,
            enabled=True,
            collateral_apr=collateral_apr,
            is_stakedao_collateral=True,
        )
        logging.info(
            f"  API-discovered market: {collateral.get('symbol')}/{loan.get('symbol')} ({market_id_hex[:20]}...)"
        )

    return active_markets
