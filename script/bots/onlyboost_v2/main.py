import json
import logging
import os
import time
from typing import Dict, List

import requests
from bots.utils.github import GithubLogService
from dotenv import load_dotenv
from shared.services.data_hub_service import fetch_adapted_vaults
from shared.address import format_eth_address
from shared.communication.telegram import send_telegram_message
from shared.constants import GlobalConstants, Protocol
from shared.external.explorer import CHAIN_EXPLORERS, CHAIN_NAMES
from shared.utils.apr import (
    adjust_apr_for_tvl,
    calculate_projected_apr,
)
from shared.utils.vault_adapter import get_apr_components_from_hub_vault
from shared.utils.formatters import format_amount
from shared.utils.globals import (
    get_redis_client,
    load_json,
    replace_double_quotes_with_single,
)
from bots.utils.telegram_format import format_activity_header
from shared.services.web3_service import get_web3_service
from web3 import Web3

load_dotenv()
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

# Configuration
IPOR_API_URL = "https://raw.githubusercontent.com/stake-dao/api/main/api/strategies/curated/ipor/index.json"
BEEFY_API_URL = "https://api.beefy.finance/vaults"

# Protocol IDs
PROTOCOL_CURVE = Protocol.CURVE
PROTOCOL_BALANCER = Protocol.BALANCER

# Protocol-specific configurations
PROTOCOL_CONFIG = {
    PROTOCOL_CURVE: {
        "name": "curve",
        "gauge_label": "Curve gauge",
        "optimizer": "Convex",
    },
    PROTOCOL_BALANCER: {
        "name": "balancer",
        "gauge_label": "Balancer gauge",
        "optimizer": "Aura",
    },
}

# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_BLOCKS_FILE = os.path.join(SCRIPT_DIR, "onlyboost_v2_last_blocks.txt")

# Per-chain backfill cap. Prevents job hang when cron is late and block gap is huge.
# Events older than cap are skipped on purpose — never-ending backfill worse than gap.
MAX_LOOKBACK_BLOCKS = {
    1: 10_000,       # ethereum ~33h
    8453: 25_000,    # base ~14h (2s blocks)
    42161: 100_000,  # arbitrum ~7h (0.25s blocks)
}
DEFAULT_MAX_LOOKBACK = 50_000

# Redis state (shared upstash, see get_redis_client):
# - checkpoint hash: chain_id -> next from_block, written after a chain
#   is fully processed, so a crashed run re-scans instead of skipping.
# - seen set: "txhash:logIndex" of already-alerted events, so a re-scan
#   (crash, overlapping runs) never re-sends a telegram alert.
REDIS_CHECKPOINT_KEY = "tg-bot:onlyboost-v2:last_blocks"
REDIS_SEEN_KEY = "tg-bot:onlyboost-v2:seen_events"
REDIS_SEEN_TTL = 7 * 24 * 3600  # outlives the widest MAX_LOOKBACK window

# Production mode - read from .env (defaults to True if not specified)
PROD = os.getenv("PROD", "True").lower() == "true"

# Dry run mode - log messages instead of sending to Telegram
DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"

# ABIs
vaultV2ABI = load_json("abi/vault_v2")
strategyV2ABI = load_json("abi/strategy_v2")

# Vault event topic0 hashes (constant — ERC4626 standard signatures used by vault_v2)
DEPOSIT_TOPIC0 = Web3.keccak(
    text="Deposit(address,address,uint256,uint256)"
).to_0x_hex()
WITHDRAW_TOPIC0 = Web3.keccak(
    text="Withdraw(address,address,address,uint256,uint256)"
).to_0x_hex()

# APR calculation is now handled by shared.utils.apr module
# The module correctly applies 16.5% fee only to base protocol rewards (CRV),
# not to trading fees or other incentives


def save_last_blocks_to_file(
    chain_blocks: Dict[int, int], filename: str = LAST_BLOCKS_FILE
):
    """Save chain ID to last block mapping to a text file"""
    try:
        with open(filename, "w") as f:
            json.dump(chain_blocks, f, indent=2)
        logging.info(
            f"Saved last blocks for {len(chain_blocks)} chains to {filename}"
        )
    except Exception as e:
        logging.error(f"Error saving last blocks to file: {e}")


def load_last_blocks_from_file(
    filename: str = LAST_BLOCKS_FILE,
) -> Dict[int, int]:
    """Load chain ID to last block mapping from a text file"""
    try:
        if os.path.exists(filename):
            with open(filename, "r") as f:
                data = json.load(f)
                # Convert string keys to integers
                return {int(k): v for k, v in data.items()}
        else:
            logging.info(f"No last blocks file found at {filename}")
            return {}
    except Exception as e:
        logging.error(f"Error loading last blocks from file: {e}")
        return {}


def load_checkpoints_from_redis() -> List[Dict]:
    """Load per-chain checkpoints. Raises on redis failure: better to fail
    the run (checkpoint intact, next run retries) than to bootstrap forward
    and silently skip events."""
    with get_redis_client() as redis_client:
        raw = redis_client.hgetall(REDIS_CHECKPOINT_KEY)
    return [
        {"chain_id": int(chain_id), "last_block": int(block)}
        for chain_id, block in raw.items()
    ]


def save_checkpoint_to_redis(
    chain_id: int, next_from_block: int, fail_loud: bool = False
):
    try:
        with get_redis_client() as redis_client:
            redis_client.hset(
                REDIS_CHECKPOINT_KEY, str(chain_id), str(next_from_block)
            )
    except Exception as e:
        # fail_loud is for bootstrap: with no prior checkpoint, a lost write
        # silently skips every block until the next bootstrap. Otherwise fail
        # soft: next run re-scans the same range, dedup set absorbs it.
        if fail_loud:
            raise
        logging.warning(
            f"Chain {chain_id}: failed to save redis checkpoint: {e}"
        )


def get_event_id(event: Dict) -> str:
    tx_hash = event["transactionHash"]
    tx_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    return f"{tx_hash.lower()}:{event['logIndex']}"


def is_event_seen(event_id: str) -> bool:
    try:
        with get_redis_client() as redis_client:
            return bool(redis_client.sismember(REDIS_SEEN_KEY, event_id))
    except Exception as e:
        # Fail soft: a duplicate alert beats a missed one
        logging.warning(f"Redis seen-check failed for {event_id}: {e}")
        return False


def mark_event_seen(event_id: str):
    try:
        with get_redis_client() as redis_client:
            redis_client.sadd(REDIS_SEEN_KEY, event_id)
            redis_client.expire(REDIS_SEEN_KEY, REDIS_SEEN_TTL)
    except Exception as e:
        logging.warning(f"Redis seen-mark failed for {event_id}: {e}")


class OnlyBoostV2Bot:
    def __init__(self):
        self.web3_service = get_web3_service(1)
        self._public_web3_cache: Dict[int, Web3] = {}
        self.chain_ids_last_blocks = []
        self.last_telegram_send = 0  # Track last telegram message time
        self.telegram_rate_limit = 3.0  # Minimum seconds between messages
        self.processed_blocks = {}  # Track processed blocks per chain
        self.strategy_addresses = (
            {}
        )  # Cache strategy addresses per chain (one per chain)

        # IPOR vault mappings (address -> label, lowercase for case-insensitive matching)
        self.ipor_vault_mapping = {}
        self._fetch_ipor_vaults()

        # Beefy vault mappings (address -> {label, chain, beefy_id}, lowercase for case-insensitive matching)
        self.beefy_vault_mapping = {}
        self._fetch_beefy_vaults()

        # Name mapping for Curve.fi Factory USD Metapools
        self.asset_name_mapping = {
            # Common metapools
            "Curve.fi Factory USD Metapool: TrueUSD": "TUSD/3CRV",
            "Curve.fi Factory USD Metapool: Liquity": "LUSD/3CRV",
            "Curve.fi Factory USD Metapool: Frax": "FRAX/3CRV",
            "Curve.fi Factory USD Metapool: Binance USD": "BUSD/3CRV",
            "Curve.fi Factory USD Metapool: Alchemix USD": "alUSD/3CRV",
            "Curve.fi Factory USD Metapool: Magic Internet Money 3Pool": "MIM/3CRV",
            "Curve.fi Factory USD Metapool: Origin Dollar": "OUSD/3CRV",
            "Curve.fi Factory USD Metapool: FEI Metapool": "FEI/3CRV",
            "Curve.fi Factory USD Metapool: GrapefruitUSD": "GFUSD/3CRV",
            "Curve.fi Factory USD Metapool: NAOS USD": "nUSD/3CRV",
            "Curve.fi Factory USD Metapool: USDM": "USDM/3CRV",
            "Curve.fi Factory USD Metapool: Wasabi USD": "WUSD/3CRV",
            "Curve.fi Factory USD Metapool: DOLA-3pool Curve LP": "DOLA/3CRV",
            "Curve.fi Factory USD Metapool: PWRD Metapool": "PWRD/3CRV",
            "Curve.fi Factory USD Metapool: 17PctCryptoDiversifiedDollar": "17d-USD/3CRV",
            "Curve.fi Factory USD Metapool: kusd-3pool": "kUSD/3CRV",
            "Curve.fi Factory USD Metapool: tusd-3pool": "TUSD/3CRV",
            "Curve.fi Factory USD Metapool: fUSD-3pool": "fUSD/3CRV",
            "Curve.fi Factory USD Metapool: DEI": "DEI/3CRV",
            "Curve.fi Factory USD Metapool: DSU": "DSU/3CRV",
            "Curve.fi Factory USD Metapool: Paxos Dollar (USDP)": "USDP/3CRV",
            "Curve.fi Factory USD Metapool: SORA XSTUSD": "XSTUSD/3CRV",
            "Curve.fi Factory USD Metapool: xDollar Interverse Money": "XIM/3CRV",
            "Curve.fi Factory USD Metapool: xim": "XIM/3CRV",
            "Curve.fi Factory USD Metapool: RAMP rUSD": "rUSD/3CRV",
            "Curve.fi Factory USD Metapool: dForce": "USX/3CRV",
            "Curve.fi Factory USD Metapool: Bean": "BEAN/3CRV",
            "Curve.fi Factory USD Metapool: USDV": "USDV/3CRV",
            "Curve.fi Factory USD Metapool: PAR/USDC": "PAR/USDC",
            "Curve.fi Factory USD Metapool: baoUSD": "baoUSD/3CRV",
            "Curve.fi Factory USD Metapool: sUSD Metapool": "sUSD/3CRV",
            "Curve.fi Factory USD Metapool: fiat": "FIAT/3CRV",
            "Curve.fi Factory USD Metapool: PUSd": "PUSd/3CRV",
            "Curve.fi Factory USD Metapool: USDD/3CRV": "USDD/3CRV",
            "Curve.fi Factory USD Metapool: Ubiquity 3Pool": "uAD/3CRV",
            "Curve.fi Factory USD Metapool: USDS/3CRV": "USDS/3CRV",
            "Curve.fi Factory USD Metapool: USDi": "USDi/3CRV",
            "Curve.fi Factory USD Metapool: 3CRV/lvUSD": "lvUSD/3CRV",
            # FRAXBP pools
            "Curve.fi Factory USD Metapool: USDDFRAXBP": "USDD/FRAXBP",
            "Curve.fi Factory USD Metapool: sUSDFRAXBP": "sUSD/FRAXBP",
            "Curve.fi Factory USD Metapool: LUSDFRAXBP": "LUSD/FRAXBP",
            "Curve.fi Factory USD Metapool: apeUSDFRAXBP": "apeUSD/FRAXBP",
            "Curve.fi Factory USD Metapool: alUSDFRAXBP": "alUSD/FRAXBP",
            "Curve.fi Factory USD Metapool: GUSDFRAXBP": "GUSD/FRAXBP",
            "Curve.fi Factory USD Metapool: BUSDFRAXBP": "BUSD/FRAXBP",
            "Curve.fi Factory USD Metapool: TUSDFRAXBP": "TUSD/FRAXBP",
            "Curve.fi Factory USD Metapool: MIMFRAXBP": "MIM/FRAXBP",
            "Curve.fi Factory USD Metapool: DOLA/FRAXBP": "DOLA/FRAXBP",
            "Curve.fi Factory USD Metapool: pUSDFRAXBP": "pUSD/FRAXBP",
            "Curve.fi Factory USD Metapool: MAI+FRAXBP": "MAI/FRAXBP",
            # Other pools
            "Curve.fi Factory USD Metapool: MXNT": "MXNT/3CRV",
            "Curve.fi Factory USD Metapool: EUROC": "EUROC/3CRV",
            "Curve.fi Factory USD Metapool: MAI+3pool": "MAI/3CRV",
            "Curve.fi Factory USD Metapool: handleUSD": "fxUSD/3CRV",
            "Curve.fi Factory USD Metapool: USDT/DAI": "USDT/DAI",
            "Curve.fi Factory USD Metapool: CRV/GNO": "CRV/GNO",
            "Curve.fi Factory USD Metapool: CRV/ZARP": "CRV/ZARP",
            "Curve.fi Factory USD Metapool: GYEN/USDC": "GYEN/USDC",
            "Curve.fi Factory USD Metapool: Pull": "PLA/3CRV",
            "Curve.fi Factory USD Metapool: CRV/REN": "CRV/REN",
            "Curve.fi Factory USD Metapool: CRV/YFI": "CRV/YFI",
            # Generic fallback patterns
            "Curve.fi Factory USD Metapool: wormhole v2 UST-3Pool": "UST/3CRV",
            "Curve.fi Factory USD Metapool: 3CRVUST": "UST/3CRV",
            "Curve.fi Factory USD Metapool: OMI/USD": "OMI/3CRV",
        }

        self.github_log_service = GithubLogService()

        # Cache for APR tracking across events within a single run
        # Key: vault_address (lowercase), Value: {"apr_after": float, "gauge_tvl_after_usd": float, "apr_components": AprComponents}
        self.vault_apr_cache: Dict[str, Dict] = {}

        # Hook incentives (campaign-based rewards like YB APR) fetched once per run

        if PROD:
            # Production mode: checkpoints live in redis
            self.chain_ids_last_blocks = load_checkpoints_from_redis()
            logging.info(
                f"Loaded {len(self.chain_ids_last_blocks)} chain checkpoints from redis"
            )
        else:
            # Development mode: use local file
            logging.info(
                "Running in development mode, loading last blocks from file"
            )
            last_blocks_dict = load_last_blocks_from_file()
            # Convert to the format expected by get_last_block_and_log
            self.chain_ids_last_blocks = [
                {"chain_id": chain_id, "last_block": last_block}
                for chain_id, last_block in last_blocks_dict.items()
            ]
            logging.info(
                f"Loaded {len(self.chain_ids_last_blocks)} chain block records from file"
            )

    def _fetch_ipor_vaults(self):
        """Fetch IPOR vault configurations from GitHub API"""
        try:
            response = requests.get(IPOR_API_URL, timeout=10)
            response.raise_for_status()
            ipor_data = response.json()

            # Parse IPOR vault data - it's a list of vault objects
            if isinstance(ipor_data, list):
                for vault_info in ipor_data:
                    vault_address = vault_info.get("address", "").lower()
                    vault_name = vault_info.get("name", "")

                    if vault_address and vault_name:
                        # Create label like "IPOR vault LlamaRisk crvUSD Optimizer"
                        label = f"IPOR vault {vault_name}"
                        self.ipor_vault_mapping[vault_address] = label
                        logging.info(f"Loaded IPOR vault: {vault_address} -> {label}")
            elif isinstance(ipor_data, dict):
                # Fallback for single vault dict format
                vault_address = ipor_data.get("address", "").lower()
                vault_name = ipor_data.get("name", "")

                if vault_address and vault_name:
                    # Create label like "IPOR vault LlamaRisk crvUSD Optimizer"
                    label = f"IPOR vault {vault_name}"
                    self.ipor_vault_mapping[vault_address] = label
                    logging.info(f"Loaded IPOR vault: {vault_address} -> {label}")

        except Exception as e:
            logging.warning(f"Failed to fetch IPOR vaults: {e}")

    def _fetch_beefy_vaults(self):
        """Fetch Beefy vaults that use Stake DAO strategies from Beefy API"""
        try:
            response = requests.get(BEEFY_API_URL, timeout=30)
            response.raise_for_status()
            beefy_vaults = response.json()

            for vault in beefy_vaults:
                if vault.get("status") != "active":
                    continue

                if vault.get("platformId", "").lower() != "stakedao":
                    continue

                earn_contract = vault.get("earnContractAddress", "").lower()
                vault_name = vault.get("name", vault.get("id", "Unknown"))
                chain = vault.get("network", "")

                if earn_contract:
                    label = f"Beefy {vault_name}"
                    self.beefy_vault_mapping[earn_contract] = {
                        "label": label,
                        "chain": chain,
                        "beefy_id": vault.get("id", "")
                    }
                    logging.info(f"Loaded Beefy vault: {earn_contract} -> {label} ({chain})")

            logging.info(f"Loaded {len(self.beefy_vault_mapping)} Beefy vaults for Stake DAO")

        except Exception as e:
            logging.warning(f"Failed to fetch Beefy vaults: {e}")

    def get_proper_asset_name(self, asset_name: str) -> str:
        """Get proper name for asset, replacing generic Curve.fi Factory USD Metapool names"""
        # Check if we have a mapping for this asset name
        if asset_name in self.asset_name_mapping:
            return self.asset_name_mapping[asset_name]

        # If it starts with the generic prefix, try to extract a better name
        if asset_name.startswith("Curve.fi Factory USD Metapool:"):
            # Extract the part after the colon
            suffix = asset_name.replace(
                "Curve.fi Factory USD Metapool:", ""
            ).strip()

            # If suffix looks like a pool name already, use it
            if "/" in suffix or "+" in suffix or "-" in suffix:
                return suffix

            # Otherwise, append /3CRV as a default
            return f"{suffix}/3CRV"

        # Return original name if no mapping needed
        return asset_name

    def get_protocol_config(self, vault: Dict) -> Dict:
        """Get protocol configuration based on vault protocolId"""
        protocol_id = vault.get("protocolId", PROTOCOL_CURVE)
        return PROTOCOL_CONFIG.get(protocol_id, PROTOCOL_CONFIG[PROTOCOL_CURVE])

    def check_ipor_transaction(self, event: Dict, vault: Dict, web3: Web3) -> str:
        """
        Check if a transaction involves an IPOR vault by looking at the transaction
        recipient and internal calls. Returns IPOR vault label if found, empty string otherwise.
        """
        try:
            # First check if the sender or owner in the event args is an IPOR vault
            args = event.get("args", {})

            # For deposits, check sender and owner
            if "sender" in args:
                sender = args["sender"].lower()
                if sender in self.ipor_vault_mapping:
                    return self.ipor_vault_mapping[sender]

            if "owner" in args:
                owner = args["owner"].lower()
                if owner in self.ipor_vault_mapping:
                    return self.ipor_vault_mapping[owner]

            # Also check receiver for withdrawals
            if "receiver" in args:
                receiver = args["receiver"].lower()
                if receiver in self.ipor_vault_mapping:
                    return self.ipor_vault_mapping[receiver]

            tx_hash = event["transactionHash"]
            receipt = web3.eth.get_transaction_receipt(tx_hash)

            # Check all log addresses in the transaction
            for log in receipt["logs"]:
                log_address = log["address"].lower()
                if log_address in self.ipor_vault_mapping:
                    return self.ipor_vault_mapping[log_address]

            # Also check the transaction 'to' address
            tx = web3.eth.get_transaction(tx_hash)
            if tx and tx.get("to"):
                to_address = tx["to"].lower()
                if to_address in self.ipor_vault_mapping:
                    return self.ipor_vault_mapping[to_address]

            return ""

        except Exception as e:
            logging.debug(f"Error checking for IPOR transaction: {e}")
            return ""

    def check_beefy_transaction(self, event: Dict, vault: Dict, web3: Web3) -> str:
        """
        Check if a transaction involves a Beefy vault.
        Returns Beefy vault label if found, empty string otherwise.
        """
        try:
            args = event.get("args", {})

            # Check sender (for deposits)
            if "sender" in args:
                sender = args["sender"].lower()
                if sender in self.beefy_vault_mapping:
                    return self.beefy_vault_mapping[sender]["label"]

            # Check owner
            if "owner" in args:
                owner = args["owner"].lower()
                if owner in self.beefy_vault_mapping:
                    return self.beefy_vault_mapping[owner]["label"]

            # Check receiver (for withdrawals)
            if "receiver" in args:
                receiver = args["receiver"].lower()
                if receiver in self.beefy_vault_mapping:
                    return self.beefy_vault_mapping[receiver]["label"]

            # Check transaction logs for Beefy vault involvement
            # (e.g., Transfer events to/from Beefy vaults)
            tx_hash = event["transactionHash"]
            receipt = web3.eth.get_transaction_receipt(tx_hash)

            # ERC20 Transfer event signature
            transfer_topic = web3.keccak(text="Transfer(address,address,uint256)")

            for log in receipt["logs"]:
                # Check if log address is a Beefy vault
                log_address = log["address"].lower()
                if log_address in self.beefy_vault_mapping:
                    return self.beefy_vault_mapping[log_address]["label"]

                # Check Transfer events for Beefy vault as sender or recipient
                if len(log["topics"]) >= 3 and log["topics"][0] == transfer_topic:
                    # from address (topic 1)
                    from_address = "0x" + log["topics"][1].hex()[-40:]
                    if from_address.lower() in self.beefy_vault_mapping:
                        return self.beefy_vault_mapping[from_address.lower()]["label"]

                    # to address (topic 2)
                    to_address = "0x" + log["topics"][2].hex()[-40:]
                    if to_address.lower() in self.beefy_vault_mapping:
                        return self.beefy_vault_mapping[to_address.lower()]["label"]

            return ""

        except Exception as e:
            logging.debug(f"Error checking for Beefy transaction: {e}")
            return ""

    def get_web3(self, chain_id: int) -> Web3:
        """
        Web3 connection for scanning. Uses CHAIN_ID_TO_PUBLIC_RPC by default
        (mevblocker / stake_rpc) — Alchemy quota was getting hammered, and the
        bulk-getLogs refactor reduced per-chain calls to 2, so a public RPC is
        plenty. Falls back to CHAIN_ID_TO_RPC if no public entry exists.
        """
        rpc_url = (
            GlobalConstants.CHAIN_ID_TO_PUBLIC_RPC.get(chain_id)
            or GlobalConstants.CHAIN_ID_TO_RPC.get(chain_id)
        )
        if not rpc_url:
            logging.warning(f"Unsupported chain ID: {chain_id}, skipping")
            return None

        if chain_id not in self.web3_service.w3:
            self.web3_service.add_chain(chain_id)
        return self.web3_service.get_w3(chain_id)

    def get_alternate_web3(self, chain_id: int) -> Web3:
        """
        Alternate Web3 on CHAIN_ID_TO_RPC (private/Alchemy/official) — used as
        fallback when the public RPC returns a hard error (403, etc.).
        """
        rpc_url = GlobalConstants.CHAIN_ID_TO_RPC.get(chain_id)
        if not rpc_url:
            return None
        return Web3(Web3.HTTPProvider(rpc_url))

    def get_strategy_address(
        self, chain_id: int, web3: Web3, vault_address: str
    ) -> str:
        """Get strategy address for a chain (cached - one per chain)"""
        # Since there's one strategy per chain, we only need to cache by chain_id
        if chain_id in self.strategy_addresses:
            return self.strategy_addresses[chain_id]

        try:
            vault_contract = web3.eth.contract(
                address=web3.to_checksum_address(vault_address), abi=vaultV2ABI
            )
            strategy_address = vault_contract.functions.strategy().call()
            self.strategy_addresses[chain_id] = strategy_address
            logging.info(
                f"Found strategy address for chain {chain_id}: {strategy_address}"
            )
            return strategy_address
        except Exception as e:
            logging.debug(
                f"Could not get strategy address for chain {chain_id}: {e}"
            )
            return None

    def get_tvl_shares(
        self, vault_address: str, gauge_address: str, web3: Web3
    ) -> Dict:
        """Get TVL shares for Stake DAO and Convex with optimization info"""
        try:
            # Get allocator
            vault_contract = web3.eth.contract(
                address=web3.to_checksum_address(vault_address), abi=vaultV2ABI
            )

            allocator_address = vault_contract.functions.allocator().call()

            # Get allocation targets and optimal balance from Allocator
            allocator_abi = [
                {
                    "inputs": [
                        {
                            "internalType": "address",
                            "name": "gauge",
                            "type": "address",
                        }
                    ],
                    "name": "getAllocationTargets",
                    "outputs": [
                        {
                            "internalType": "address[]",
                            "name": "",
                            "type": "address[]",
                        }
                    ],
                    "stateMutability": "view",
                    "type": "function",
                },
                {
                    "inputs": [
                        {
                            "internalType": "address",
                            "name": "gauge",
                            "type": "address",
                        }
                    ],
                    "name": "getOptimalLockerBalance",
                    "outputs": [
                        {
                            "internalType": "uint256",
                            "name": "",
                            "type": "uint256",
                        }
                    ],
                    "stateMutability": "view",
                    "type": "function",
                },
            ]

            allocator_contract = web3.eth.contract(
                address=web3.to_checksum_address(allocator_address),
                abi=allocator_abi,
            )

            # Get both allocation targets and optimal balance
            allocation_targets = (
                allocator_contract.functions.getAllocationTargets(
                    web3.to_checksum_address(gauge_address)
                ).call()
            )

            # If only one allocation target, no repartition to show
            if len(allocation_targets) < 2:
                return {
                    "stakedao_percentage": 0,
                    "convex_percentage": 0,
                    "optimized_stakedao_percentage": 0,
                    "optimized_convex_percentage": 0,
                }

            # Identify based on position: first = Convex, second = StakeDAO
            convex_holder = (
                allocation_targets[0] if len(allocation_targets) > 0 else None
            )
            stakedao_locker = (
                allocation_targets[1] if len(allocation_targets) > 1 else None
            )

            # Get optimal locker balance
            optimal_locker_balance = (
                allocator_contract.functions.getOptimalLockerBalance(
                    web3.to_checksum_address(gauge_address)
                ).call()
            )

            # Get balances
            gauge_balance_abi = [
                {
                    "inputs": [
                        {
                            "internalType": "address",
                            "name": "account",
                            "type": "address",
                        }
                    ],
                    "name": "balanceOf",
                    "outputs": [
                        {
                            "internalType": "uint256",
                            "name": "",
                            "type": "uint256",
                        }
                    ],
                    "stateMutability": "view",
                    "type": "function",
                },
                {
                    "inputs": [],
                    "name": "totalSupply",
                    "outputs": [
                        {
                            "internalType": "uint256",
                            "name": "",
                            "type": "uint256",
                        }
                    ],
                    "stateMutability": "view",
                    "type": "function",
                },
            ]

            gauge_contract = web3.eth.contract(
                address=web3.to_checksum_address(gauge_address),
                abi=gauge_balance_abi,
            )

            # Get Stake DAO balance
            stakedao_balance = 0
            if stakedao_locker:
                stakedao_balance = gauge_contract.functions.balanceOf(
                    web3.to_checksum_address(stakedao_locker)
                ).call()

            # Get Convex balance - call balanceOf ON the Convex contract
            convex_balance = 0
            if convex_holder:
                convex_abi = [
                    {
                        "inputs": [],
                        "name": "balanceOf",
                        "outputs": [
                            {
                                "internalType": "uint256",
                                "name": "",
                                "type": "uint256",
                            }
                        ],
                        "stateMutability": "view",
                        "type": "function",
                    }
                ]

                convex_contract = web3.eth.contract(
                    address=web3.to_checksum_address(convex_holder),
                    abi=convex_abi,
                )

                convex_balance = convex_contract.functions.balanceOf().call()

            # Calculate current percentages relative to StakeDAO + Convex only
            result = {
                "stakedao_percentage": 0,
                "convex_percentage": 0,
                "optimized_stakedao_percentage": 0,
                "optimized_convex_percentage": 0,
            }

            total_tracked = stakedao_balance + convex_balance
            if total_tracked > 0:
                result["stakedao_percentage"] = (
                    stakedao_balance / total_tracked
                ) * 100
                result["convex_percentage"] = (
                    convex_balance / total_tracked
                ) * 100

                vault_total_supply = (
                    vault_contract.functions.totalSupply().call()
                )

                # Calculate optimized allocation based on vault's total supply
                # optimal_locker_balance is the MAX that should go to StakeDAO
                optimized_stakedao = min(
                    optimal_locker_balance, vault_total_supply
                )
                optimized_convex = vault_total_supply - optimized_stakedao

                # Calculate optimized percentages relative to each other (SD vs CVX split)
                total_optimal = optimized_stakedao + optimized_convex
                if total_optimal > 0:
                    result["optimized_stakedao_percentage"] = (
                        optimized_stakedao / total_optimal
                    ) * 100
                    result["optimized_convex_percentage"] = (
                        optimized_convex / total_optimal
                    ) * 100

            return result

        except Exception as e:
            logging.debug(f"Error getting TVL shares: {e}")
            return {
                "stakedao_percentage": 0,
                "convex_percentage": 0,
                "optimized_stakedao_percentage": 0,
                "optimized_convex_percentage": 0,
            }

    def fetch_vaults_data(self) -> List[Dict]:
        """Fetch all vaults data from DataHub API"""
        try:
            return fetch_adapted_vaults()
        except Exception as e:
            logging.error(f"Error fetching vaults data: {e}")
            return []

    def _fetch_chain_logs_with_retry(
        self,
        web3: Web3,
        addresses: List[str],
        topic0: str,
        from_block: int,
        to_block: int,
        min_range: int = 500,
        max_429_attempts: int = 6,
    ) -> List[Dict]:
        """
        Bulk eth_getLogs across many addresses with the same recovery strategies
        as fetch_event_logs_with_retry: split block range on 413, fixed 0.5s
        backoff on 429.
        """
        filter_params = {
            "address": addresses,
            "topics": [topic0],
            "fromBlock": from_block,
            "toBlock": to_block,
        }
        for attempt in range(max_429_attempts):
            try:
                return list(web3.eth.get_logs(filter_params))
            except Exception as e:
                err = str(e)
                is_413 = (
                    "413" in err
                    or "Payload Too Large" in err
                    or "response size" in err.lower()
                )
                is_429 = "429" in err or "Too Many Requests" in err

                if is_413:
                    span = to_block - from_block
                    if span <= min_range:
                        logging.warning(
                            f"413 persists at min range {from_block}-{to_block}, dropping"
                        )
                        return []
                    mid = from_block + span // 2
                    logging.info(
                        f"413 at {from_block}-{to_block} (span {span}), splitting"
                    )
                    left = self._fetch_chain_logs_with_retry(
                        web3, addresses, topic0, from_block, mid, min_range, max_429_attempts
                    )
                    right = self._fetch_chain_logs_with_retry(
                        web3, addresses, topic0, mid + 1, to_block, min_range, max_429_attempts
                    )
                    return left + right

                if is_429 and attempt < max_429_attempts - 1:
                    logging.warning(
                        f"RPC 429 on bulk getLogs {from_block}-{to_block} "
                        f"(attempt {attempt + 1}/{max_429_attempts}), backing off 0.5s"
                    )
                    time.sleep(0.5)
                    continue
                raise
        return []

    def collect_chain_events(
        self,
        chain_vaults: List[Dict],
        web3: Web3,
        from_block: int,
        to_block: int,
        chain_id: int,
    ) -> List[Dict]:
        """
        Bulk-fetch Deposit/Withdraw events for every vault on a chain in 2 RPC
        calls (plus chunks if the block range is wide), then dispatch each
        decoded log back to its vault.
        """
        if not chain_vaults:
            return []

        # Lookup table by lowercased address (logs return mixed case)
        vault_by_address: Dict[str, Dict] = {}
        addresses: List[str] = []
        for v in chain_vaults:
            addr = web3.to_checksum_address(v["address"])
            vault_by_address[addr.lower()] = v
            addresses.append(addr)

        # Decoder contract: any vault works since all share vaultV2ABI
        decoder = web3.eth.contract(address=addresses[0], abi=vaultV2ABI)

        # Strategy address (same for the whole chain) for rebalance detection
        strategy_address = self.get_strategy_address(
            chain_id, web3, chain_vaults[0]["address"]
        )

        # Per-chain max chunk size (Alchemy / public RPC log range limits)
        if chain_id in (42161, 8453):
            max_block_range = 10000
        else:
            max_block_range = 50000

        def fetch_in_chunks(topic0: str) -> List[Dict]:
            if to_block - from_block <= max_block_range:
                return self._fetch_chain_logs_with_retry(
                    web3, addresses, topic0, from_block, to_block
                )
            out: List[Dict] = []
            for chunk_start in range(from_block, to_block + 1, max_block_range):
                chunk_end = min(chunk_start + max_block_range - 1, to_block)
                try:
                    out.extend(
                        self._fetch_chain_logs_with_retry(
                            web3, addresses, topic0, chunk_start, chunk_end
                        )
                    )
                except Exception as e:
                    logging.warning(
                        f"Chain {chain_id}: error fetching logs {chunk_start}-{chunk_end}: {e}"
                    )
            return out

        raw_deposits = fetch_in_chunks(DEPOSIT_TOPIC0)
        raw_withdraws = fetch_in_chunks(WITHDRAW_TOPIC0)

        logging.info(
            f"Chain {chain_id}: bulk fetched {len(raw_deposits)} Deposit + "
            f"{len(raw_withdraws)} Withdraw logs across {len(addresses)} vaults"
        )

        relevant_events: List[Dict] = []

        for raw_log in raw_deposits:
            try:
                event = decoder.events.Deposit().process_log(raw_log)
            except Exception as e:
                logging.debug(f"Failed to decode Deposit log: {e}")
                continue
            vault = vault_by_address.get(event["address"].lower())
            if not vault:
                continue
            event_dict = dict(event)
            event_dict["is_deposit"] = True
            event_dict["vault"] = vault
            event_dict["args"] = dict(event["args"])
            sender = event["args"]["sender"]
            event_dict["is_rebalance"] = bool(
                strategy_address
                and sender.lower() == strategy_address.lower()
            )
            event_dict["is_enso"] = self.check_if_enso_transaction(
                event, vault, web3
            )
            event_dict["ipor_label"] = self.check_ipor_transaction(
                event, vault, web3
            )
            event_dict["beefy_label"] = self.check_beefy_transaction(
                event, vault, web3
            )
            relevant_events.append(event_dict)

        for raw_log in raw_withdraws:
            try:
                event = decoder.events.Withdraw().process_log(raw_log)
            except Exception as e:
                logging.debug(f"Failed to decode Withdraw log: {e}")
                continue
            vault = vault_by_address.get(event["address"].lower())
            if not vault:
                continue
            event_dict = dict(event)
            event_dict["is_deposit"] = False
            event_dict["vault"] = vault
            event_dict["args"] = dict(event["args"])
            receiver = event["args"]["receiver"]
            event_dict["is_rebalance"] = bool(
                strategy_address
                and receiver.lower() == strategy_address.lower()
            )
            event_dict["is_enso"] = False
            event_dict["ipor_label"] = self.check_ipor_transaction(
                event, vault, web3
            )
            event_dict["beefy_label"] = self.check_beefy_transaction(
                event, vault, web3
            )
            relevant_events.append(event_dict)

        return relevant_events

    def check_if_enso_transaction(
        self, event: Dict, vault: Dict, web3: Web3
    ) -> bool:
        """
        Check if a deposit transaction came through Enso router by looking for
        LP token transfers to Enso router in the same transaction.
        """
        ENSO_ROUTER = "0x4Fe93ebC4Ce6Ae4f81601cC7Ce7139023919E003"

        try:
            # Get the transaction hash
            tx_hash = event["transactionHash"]

            # Get the transaction receipt to see all logs
            receipt = web3.eth.get_transaction_receipt(tx_hash)

            # Get asset (LP token) address from vault
            asset = vault.get("asset", {})
            lp_token_address = asset.get("address", "")

            if not lp_token_address:
                return False

            # ERC20 Transfer event signature
            transfer_topic = web3.keccak(
                text="Transfer(address,address,uint256)"
            )

            # Look for Transfer events in the transaction logs
            for log in receipt["logs"]:
                # Check if this is a Transfer event from the LP token
                if (
                    len(log["topics"]) >= 3
                    and log["topics"][0] == transfer_topic
                    and log["address"].lower() == lp_token_address.lower()
                ):

                    # The second topic is the 'from' address (padded)
                    # The third topic is the 'to' address (padded)
                    to_address = "0x" + log["topics"][2].hex()[-40:]

                    # Check if the transfer was TO the Enso router
                    if to_address.lower() == ENSO_ROUTER.lower():
                        logging.info(
                            f"Found Enso router transaction: LP token {lp_token_address} transferred to Enso router"
                        )
                        return True

            return False

        except Exception as e:
            logging.debug(f"Error checking for Enso transaction: {e}")
            return False

    def process_vault_event(self, vault: Dict, event: Dict, web3: Web3):
        """Process a vault deposit/withdraw event and send notification.

        Returns True when the event is handled (sent, or legitimately
        skipped) and False on failure, so the caller can hold back the
        chain checkpoint and re-scan next run instead of losing the alert.
        """
        try:
            event_id = get_event_id(event)
            # PROD-gated: dev sends go to the test telegram channel, so they
            # must not consult or pollute the shared prod dedup set
            if PROD and is_event_seen(event_id):
                logging.info(f"Skipping already-alerted event {event_id}")
                return True

            logging.info(
                f"Processing {'deposit' if event['is_deposit'] else 'withdrawal'} event..."
            )
            chain_id = vault["chainId"]
            chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
            explorer_url = CHAIN_EXPLORERS.get(chain_id, "")

            # Get event details
            tx_hash = event["transactionHash"]
            if hasattr(tx_hash, "hex"):
                tx_hash = Web3.to_hex(tx_hash)
            block_number = event["blockNumber"]

            # Get user and amount based on event type
            if event["is_deposit"]:
                # For deposits: owner is the actual recipient (beneficiary)
                # sender could be a router/contract, so we use owner
                sender = event["args"]["sender"]
                user = event["args"]["owner"]
                amount = event["args"]["assets"]

                # Log if deposit was made through router
                if sender.lower() != user.lower():
                    logging.info(
                        f"Deposit made through router/contract: sender={sender}, owner={user}"
                    )
            else:
                # For withdrawals: owner is the user in direct withdrawals.
                # But when an aggregator (Beefy, Yearn, etc.) is the intermediary,
                # owner = strategy/vault contract. In that case, resolve to tx["from"].
                user = event["args"]["owner"]
                amount = event["args"]["assets"]

                is_contract = len(web3.eth.get_code(Web3.to_checksum_address(user))) > 0
                if is_contract:
                    try:
                        tx = web3.eth.get_transaction(event["transactionHash"])
                        user = tx["from"]
                    except Exception as e:
                        logging.debug(f"Could not get tx sender for contract withdrawal: {e}")

            # Get asset info
            asset = vault.get("asset", {})
            raw_asset_name = asset.get("name", "Unknown")
            asset_name = self.get_proper_asset_name(
                raw_asset_name
            )  # Use proper name
            asset_decimals = asset.get("decimals", 18)
            asset.get("address", "")

            # Get LP token price — prefer pre-computed from hub API, fallback to calculation
            asset_price = float(vault.get("lpPriceInUsd", 0) or 0)
            if asset_price <= 0:
                try:
                    vault_total_supply = float(vault.get("totalSupply", "0"))
                    vault_total_supply_usd = float(vault.get("totalSupplyUSD", "0"))
                    if vault_total_supply > 0 and vault_total_supply_usd > 0:
                        asset_price = vault_total_supply_usd / (
                            vault_total_supply / (10**asset_decimals)
                        )
                except Exception:
                    asset_price = 0
            if asset_price > 0:
                logging.info(
                    f"LP price for {asset_name}: ${asset_price}"
                )
            else:
                logging.error(
                    f"No LP price available for {asset_name} on chain {chain_id}"
                )

            # Get vault address and gauge data
            vault_address = vault.get("address", "")
            gauge_data = vault.get("gauge", {}) or {}
            gauge_address = gauge_data.get("address", "")

            # Fetch vault total supply for display TVL
            # Note: block_identifier=N returns state AFTER block N is executed
            # So for event in block N: use N-1 for "before", N for "after"
            vault_total_supply_before = 0
            vault_total_supply_after = 0
            try:
                vault_contract = web3.eth.contract(
                    address=web3.to_checksum_address(vault_address),
                    abi=vaultV2ABI,
                )
                # TVL before the event (state at end of block N-1, before block N executes)
                vault_total_supply_before = (
                    vault_contract.functions.totalSupply().call(
                        block_identifier=block_number - 1
                    )
                )
                vault_total_supply_before = vault_total_supply_before / (10**asset_decimals)

                # TVL after the event (state at end of block N, after tx executes)
                vault_total_supply_after = (
                    vault_contract.functions.totalSupply().call(
                        block_identifier=block_number
                    )
                )
                vault_total_supply_after = vault_total_supply_after / (10**asset_decimals)
            except Exception as e:
                logging.error(f"Error fetching vault total supply: {e}")

            # Vault TVL in USD (for display only)
            vault_tvl_before_usd = vault_total_supply_before * asset_price
            vault_tvl_after_usd = vault_total_supply_after * asset_price

            # Fetch GAUGE total supply before and after the event for APR calculations
            # APR scales with gauge TVL (total pool), not vault TVL (Stake DAO's portion)
            gauge_total_supply_before = 0
            gauge_total_supply_after = 0
            if gauge_address:
                try:
                    gauge_supply_abi = [
                        {
                            "inputs": [],
                            "name": "totalSupply",
                            "outputs": [
                                {
                                    "internalType": "uint256",
                                    "name": "",
                                    "type": "uint256",
                                }
                            ],
                            "stateMutability": "view",
                            "type": "function",
                        },
                    ]
                    gauge_contract = web3.eth.contract(
                        address=web3.to_checksum_address(gauge_address),
                        abi=gauge_supply_abi,
                    )
                    gauge_total_supply_before = (
                        gauge_contract.functions.totalSupply().call(
                            block_identifier=block_number - 1
                        )
                    )
                    gauge_total_supply_before = gauge_total_supply_before / (10**asset_decimals)

                    gauge_total_supply_after = (
                        gauge_contract.functions.totalSupply().call(
                            block_identifier=block_number
                        )
                    )
                    gauge_total_supply_after = gauge_total_supply_after / (10**asset_decimals)
                except Exception as e:
                    logging.error(f"Error fetching gauge total supply: {e}")

            # Gauge TVL in USD (for APR calculations)
            gauge_tvl_before_usd = gauge_total_supply_before * asset_price
            gauge_tvl_after_usd = gauge_total_supply_after * asset_price

            # Get current gauge TVL from API for APR adjustment
            current_gauge_tvl_usd = float(gauge_data.get("totalSupplyUSD", "0"))

            # Keep backward compatible variable name for TVL display (vault TVL)
            total_supply_usd = vault_tvl_after_usd

            # Calculate APR components and projected APR
            # Uses shared utility that correctly applies 16.5% fee only to base rewards (CRV)
            apr_before = 0.0
            apr_after = 0.0
            apr_components_after = None  # Track for caching

            # Normalize vault address for cache key
            vault_address_lower = vault_address.lower()

            try:
                cached_apr = self.vault_apr_cache.get(vault_address_lower)

                if cached_apr is not None:
                    # Use cached APR from previous event on this vault
                    # This ensures Event N+1's "before" matches Event N's "after"
                    apr_before = cached_apr["apr_after"]
                    apr_components_before = cached_apr["apr_components"]
                    logging.debug(
                        f"Using cached APR for {vault_address_lower}: {apr_before:.4f}"
                    )
                else:
                    # No cache - calculate from hub API pre-computed APR (first event for this vault in this run)
                    apr_components_current = get_apr_components_from_hub_vault(vault.get("_hub", vault))
                    apr_components_before = adjust_apr_for_tvl(
                        apr_components_current,
                        current_gauge_tvl_usd,
                        gauge_tvl_before_usd,
                    )
                    apr_before = apr_components_before["total_apr"]

                # Calculate projected APR based on gauge TVL change from before -> after
                apr_after = calculate_projected_apr(
                    apr_components_before,
                    gauge_tvl_before_usd,
                    gauge_tvl_after_usd,
                )

                # Build apr_components_after for caching
                # Scale the components to reflect the new gauge TVL
                if gauge_tvl_before_usd > 0 and gauge_tvl_after_usd > 0:
                    tvl_ratio = gauge_tvl_before_usd / gauge_tvl_after_usd
                    apr_components_after = {
                        "dilutable_apr": apr_components_before["dilutable_apr"] * tvl_ratio,
                        "constant_apr": apr_components_before["constant_apr"],
                        "total_apr": apr_after,
                        "boost_multiplier": apr_components_before["boost_multiplier"],
                        "fee_deducted": apr_components_before["fee_deducted"] * tvl_ratio,
                    }
            except Exception as e:
                logging.debug(f"Error calculating APR: {e}")
                apr_before = 0.0
                apr_after = 0.0
                apr_components_after = None

            # Cache APR state for subsequent events on this vault
            if apr_after > 0 and apr_components_after is not None:
                self.vault_apr_cache[vault_address_lower] = {
                    "apr_after": apr_after,
                    "gauge_tvl_after_usd": gauge_tvl_after_usd,
                    "apr_components": apr_components_after,
                }
                logging.debug(
                    f"Cached APR for {vault_address_lower}: {apr_after:.4f} at gauge TVL ${gauge_tvl_after_usd:,.2f}"
                )

            # Calculate amounts
            amount_formatted = amount / (10**asset_decimals)
            amount_usd = amount_formatted * asset_price

            # Debug logging
            logging.info(
                f"Asset: {asset_name}, Price: ${asset_price}, Amount: {amount_formatted}, USD Value: ${amount_usd}"
            )

            # Skip transactions less than $10,000
            if amount_usd < 10000:
                logging.info(
                    f"Skipping transaction with amount ${amount_usd:.2f} (below $10,000 threshold)"
                )
                return True

            # Format numbers
            amount_str = format_amount(amount_formatted)
            amount_usd_str = format_amount(amount_usd)
            tvl_str = format_amount(total_supply_usd)
            apr_before_str = "{:.2f}".format(apr_before * 100)  # Convert to percentage
            apr_after_str = "{:.2f}".format(apr_after * 100)  # Convert to percentage

            # Extract domain name from explorer URL
            explorer_name = explorer_url.replace("https://", "").replace(
                "http://", ""
            )

            # Get gauge address if available
            gauge = vault.get("gauge", {})
            gauge_address = gauge.get("address", "")

            # Get protocol config for dynamic labels
            protocol_config = self.get_protocol_config(vault)
            optimizer_name = protocol_config["optimizer"]

            # Get TVL shares if we have a gauge
            tvl_shares_text = ""
            optimal_text = ""
            if gauge_address:
                tvl_shares = self.get_tvl_shares(
                    vault_address, gauge_address, web3
                )
                if (
                    tvl_shares["stakedao_percentage"] > 0
                    or tvl_shares["convex_percentage"] > 0
                ):
                    sd_pct = tvl_shares["stakedao_percentage"]
                    cvx_pct = tvl_shares["convex_percentage"]
                    opt_sd_pct = tvl_shares["optimized_stakedao_percentage"]
                    opt_cvx_pct = tvl_shares["optimized_convex_percentage"]

                    # Smart formatting based on values
                    if sd_pct == 100 and cvx_pct == 0:
                        # All in StakeDAO - simplified display
                        tvl_shares_text = "\nOnlyboost: 100% Stake DAO"
                    elif sd_pct == 0 and cvx_pct == 100:
                        # All in optimizer - simplified display
                        tvl_shares_text = f"\nOnlyboost: 100% {optimizer_name}"
                    else:
                        # Show current allocation
                        tvl_shares_text = f"\nOnlyboost: {sd_pct:.0f}% Stake DAO / {cvx_pct:.0f}% {optimizer_name}"
                        if abs(sd_pct - opt_sd_pct) >= 5.0:
                            optimal_text = f"\nOptimal allocation: {opt_sd_pct:.0f} / {opt_cvx_pct:.0f}"

            # Build message
            message = format_activity_header(
                protocol_config["name"].capitalize(), chain_name
            )

            # Check if this transaction involves an IPOR vault or Beefy vault
            ipor_label = event.get("ipor_label", "")
            beefy_label = event.get("beefy_label", "")

            # Build source suffix (prioritize IPOR, then Beefy)
            if ipor_label:
                source_suffix = f" via {ipor_label}"
            elif beefy_label:
                source_suffix = f" via {beefy_label}"
            else:
                source_suffix = ""

            # Check if this transaction came through Enso router
            if amount_usd >= 500_000:
                circle_count = 3
            elif amount_usd >= 100_000:
                circle_count = 2
            else:
                circle_count = 1
            deposit_circle = "🟢" * circle_count
            withdraw_circle = "🔴" * circle_count

            if event.get("is_enso", False):
                if event["is_deposit"]:
                    message += f"{deposit_circle} Deposited {amount_str} {asset_name} (${amount_usd_str}) via ENSO 🔄{source_suffix}\n"
                else:
                    message += f"{withdraw_circle} Withdraw {amount_str} {asset_name} (${amount_usd_str}) via ENSO 🔄{source_suffix}\n"
            else:
                if event["is_deposit"]:
                    message += f"{deposit_circle} Deposited {amount_str} {asset_name} (${amount_usd_str}){source_suffix}\n"
                else:
                    message += f"{withdraw_circle} Withdraw {amount_str} {asset_name} (${amount_usd_str}){source_suffix}\n"

            if apr_before > 0 and apr_after > 0:
                apr_display = f" | APR {apr_before_str}% -> {apr_after_str}%"
            elif apr_before > 0:
                apr_display = f" | APR {apr_before_str}%"
            else:
                apr_display = ""
            message += f"TVL ${tvl_str}{apr_display}{tvl_shares_text}{optimal_text}\n"
            # Abbreviate address using utility function
            display_name = format_eth_address(user)
            message += f"User : <a href='{explorer_url}/address/{user}'>{display_name}</a>\n"

            # Build Stake DAO URL with dynamic protocol
            protocol_name = protocol_config["name"]
            gauge_label = protocol_config["gauge_label"]
            stakedao_url = f"https://www.stakedao.org/strategy?protocol={protocol_name}&vault={chain_id}-{vault_address}"

            # Build links line
            message += f"Links : <a href='{stakedao_url}'>Stake DAO</a> | "

            if gauge_address:
                message += f"<a href='{explorer_url}/address/{gauge_address}'>{gauge_label}</a> | "

            message += f"<a href='{explorer_url}/address/{vault_address}'>Vault</a> | "
            message += (
                f"<a href='{explorer_url}/tx/{tx_hash}'>{explorer_name}</a>"
            )

            # Rate limit Telegram messages
            current_time = time.time()
            time_since_last = current_time - self.last_telegram_send
            if time_since_last < self.telegram_rate_limit:
                sleep_time = self.telegram_rate_limit - time_since_last
                logging.info(
                    f"Rate limiting: sleeping for {sleep_time:.2f} seconds"
                )
                time.sleep(sleep_time)

            if not self._deliver(message, event_id):
                return False
            # Update last send time
            self.last_telegram_send = time.time()
            return True

        except Exception as e:
            logging.error(f"Error processing vault event: {e}")
            return False

    def _deliver(self, message: str, event_id: str) -> bool:
        """Send the alert and record it as seen. Marking only happens after
        telegram confirmed delivery — send_telegram_message returns False on
        failure instead of raising, and a failed send marked as seen would be
        an alert lost forever."""
        if DRY_RUN:
            logging.info(f"DRY RUN - would send:\n{message}")
            return True

        sent = send_telegram_message(
            GlobalConstants.BOT_API_KEY,
            GlobalConstants.TELEGRAM_ACTIVITY_CHANNEL_ID,
            replace_double_quotes_with_single(message),
            parse_mode="HTML",
        )
        if not sent:
            logging.error(f"Telegram send failed for event {event_id}")
            return False

        if PROD:
            mark_event_seen(event_id)
        return True

    def run(self):
        """Main bot loop"""
        logging.info("Starting OnlyBoost V2 bot...")

        # Clear APR cache at start of each run
        self.vault_apr_cache = {}

        # Fetch vaults data
        vaults = self.fetch_vaults_data()
        logging.info(f"Found {len(vaults)} vaults")

        # Hook incentives and reward prices are already included in hub API's pre-computed APR

        # Group vaults by chain
        vaults_by_chain = {}
        for vault in vaults:
            # We only need the vault to have an address now
            if vault.get("address"):
                chain_id = vault["chainId"]
                if chain_id not in vaults_by_chain:
                    vaults_by_chain[chain_id] = []
                vaults_by_chain[chain_id].append(vault)

        logging.info(f"Processing vaults on {len(vaults_by_chain)} chains")

        # Process each chain
        for chain_id, chain_vaults in vaults_by_chain.items():
            try:
                self._process_chain(chain_id, chain_vaults)
            except Exception as e:
                logging.error(
                    f"Chain {chain_id}: failed, skipping. Error: {e}"
                )
                continue

        # Save last blocks to file if not in production mode
        if not PROD and self.processed_blocks:
            save_last_blocks_to_file(self.processed_blocks)

        logging.info("Bot run completed")

    def _process_chain(self, chain_id: int, chain_vaults: List[Dict]) -> None:
        """Wrapper that runs chain processing on the public RPC, retrying once
        on the alternate RPC if the public one fails partway through (e.g.
        publicnode 403'ing on eth_getLogs while serving eth_getBlockByNumber)."""
        public_web3 = self.get_web3(chain_id)
        if not public_web3:
            return

        try:
            self._process_chain_with_web3(chain_id, chain_vaults, public_web3)
            return
        except Exception as e:
            alt_web3 = self.get_alternate_web3(chain_id)
            same_endpoint = (
                alt_web3 is not None
                and alt_web3.provider.endpoint_uri
                == public_web3.provider.endpoint_uri
            )
            if not alt_web3 or same_endpoint:
                raise
            logging.warning(
                f"Chain {chain_id}: public RPC failed ({e}), retrying on alternate RPC "
                f"({alt_web3.provider.endpoint_uri})"
            )
            # Swap the chain's Web3 in shared caches so any downstream call
            # that re-fetches the connection gets the alternate RPC too.
            self.web3_service.w3[chain_id] = alt_web3
            self._public_web3_cache[chain_id] = alt_web3

        self._process_chain_with_web3(chain_id, chain_vaults, alt_web3)

    def _process_chain_with_web3(
        self, chain_id: int, chain_vaults: List[Dict], web3: Web3
    ) -> None:
        """Single attempt at processing a chain on the given Web3 connection."""
        from_block, current_block = (
            self.github_log_service.get_last_block_and_log(
                self.chain_ids_last_blocks, chain_id, web3
            )
        )

        if from_block == 0:
            logging.info(
                f"Chain {chain_id}: no checkpoint found, bootstrapping at block {current_block + 1}"
            )
            if PROD and not DRY_RUN:
                save_checkpoint_to_redis(
                    chain_id, current_block + 1, fail_loud=True
                )
            return

        # Cap backfill window to avoid hang when cron lags (arbitrum 784k-block disaster).
        max_lookback = MAX_LOOKBACK_BLOCKS.get(
            chain_id, DEFAULT_MAX_LOOKBACK
        )
        lookback_floor = current_block - max_lookback
        if from_block < lookback_floor:
            logging.warning(
                f"Chain {chain_id}: capping from_block {from_block} -> {lookback_floor} "
                f"(max lookback {max_lookback}, gap was {current_block - from_block})"
            )
            from_block = lookback_floor

        # Collect all events from all vaults on this chain
        logging.info(
            f"Processing {len(chain_vaults)} vaults on chain {chain_id} from block {from_block} to {current_block}"
        )

        # Bulk fetch: 2 eth_getLogs per chain (Deposit + Withdraw across all
        # vault addresses) instead of one pair per vault.
        all_chain_events = self.collect_chain_events(
            chain_vaults, web3, from_block, current_block, chain_id
        )

        # Sort all events chronologically
        all_chain_events.sort(
            key=lambda x: (
                x["blockNumber"],
                x["transactionIndex"],
                x["logIndex"],
            )
        )

        if all_chain_events:
            logging.info(
                f"Found {len(all_chain_events)} events to process on chain {chain_id}"
            )
            logging.info(
                f"Events span from block {all_chain_events[0]['blockNumber']} to {all_chain_events[-1]['blockNumber']}"
            )

        # Process events
        event_count = 0
        skipped_rebalances = 0
        chain_complete = True

        for event in all_chain_events:
            # Skip rebalance events
            if event.get("is_rebalance", False):
                skipped_rebalances += 1
                tx_hash = event["transactionHash"]
                tx_hash_str = (
                    tx_hash.hex() if hasattr(tx_hash, "hex") else tx_hash
                )
                logging.info(
                    f"Skipping rebalance transaction: {tx_hash_str}"
                )
                continue

            # Process normal deposits/withdrawals
            event_count += 1
            vault = event["vault"]
            event_type = "deposit" if event["is_deposit"] else "withdrawal"

            # Log if this is an Enso transaction
            enso_info = (
                " (via Enso router)" if event.get("is_enso", False) else ""
            )
            logging.info(
                f"Processing event {event_count}/{len(all_chain_events) - skipped_rebalances}: {event_type}{enso_info} at block {event['blockNumber']}"
            )

            if not self.process_vault_event(vault, event, web3):
                chain_complete = False
            time.sleep(0.1)

        if skipped_rebalances > 0:
            logging.info(f"Skipped {skipped_rebalances} rebalance events")

        # Track the current block for this chain
        self.processed_blocks[chain_id] = current_block
        if PROD and not DRY_RUN:
            if chain_complete:
                save_checkpoint_to_redis(chain_id, current_block + 1)
            else:
                # A send or processing failure: keep the old checkpoint so the
                # next run re-scans the range. Delivered alerts are in the
                # seen set, so only the failed ones get retried.
                logging.warning(
                    f"Chain {chain_id}: event failures, checkpoint not advanced"
                )


def main():
    try:
        bot = OnlyBoostV2Bot()
        bot.run()
    except Exception:
        logging.exception("Bot error")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
