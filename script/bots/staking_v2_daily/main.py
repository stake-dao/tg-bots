import logging
from typing import Dict, List

from dotenv import load_dotenv
from shared.services.data_hub_service import fetch_adapted_vaults
from shared.communication.telegram import send_telegram_message
from shared.constants import GlobalConstants, Protocol
from shared.external.explorer import CHAIN_NAMES
from shared.strings import abbreviate_number
from shared.utils.vault_adapter import get_apr_from_hub_vault

load_dotenv()
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

# Prefixes to remove from asset names
ASSET_NAME_PREFIXES = [
    "Curve.fi Factory Plain Pool: ",
    "Curve.fi Factory Crypto Pool: ",
    "Curve.fi Factory USD Metapool: ",
    "Curve.fi Factory Pool: ",
    "Curve.fi ",
]


def clean_asset_name(asset_name: str) -> str:
    """Clean asset name by removing Curve.fi Factory prefixes"""
    for prefix in ASSET_NAME_PREFIXES:
        if asset_name.startswith(prefix):
            return asset_name[len(prefix):]
    return asset_name


def fetch_vaults_data() -> List[Dict]:
    """Fetch all vaults data from DataHub API"""
    try:
        return fetch_adapted_vaults()
    except Exception as e:
        logging.error(f"Error fetching vaults data: {e}")
        return []


def calculate_chain_stats(vaults: List[Dict]) -> Dict[int, Dict]:
    """
    Calculate stats per chain:
    - Total TVL (sum of vault totalSupplyUSD)
    - Top 5 strategies by TVL
    - Stake DAO share per strategy
    """
    chain_stats: Dict[int, Dict] = {}

    for vault in vaults:
        chain_id = vault.get("chainId")
        if chain_id is None:
            continue

        # Initialize chain stats if not exists
        if chain_id not in chain_stats:
            chain_stats[chain_id] = {
                "total_tvl": 0.0,
                "strategies": []
            }

        # Get Stake DAO TVL (root totalSupplyUSD)
        sd_tvl = float(vault.get("totalSupplyUSD", 0) or 0)

        # Get total gauge TVL (gauge.totalSupplyUSD)
        gauge = vault.get("gauge", {})
        gauge_tvl = float(gauge.get("totalSupplyUSD", 0) or 0) if gauge else 0

        # Get asset name
        asset = vault.get("asset", {})
        asset_name = asset.get("name", "Unknown") if asset else "Unknown"

        # Calculate total APR using shared utility
        # Fee is only applied to base protocol rewards (CRV), not trading fees or other incentives
        total_apr = get_apr_from_hub_vault(vault.get("_hub", vault))

        # Add to chain total TVL
        chain_stats[chain_id]["total_tvl"] += sd_tvl

        # Add strategy info
        chain_stats[chain_id]["strategies"].append({
            "name": asset_name,
            "sd_tvl": sd_tvl,
            "gauge_tvl": gauge_tvl,
            "apr": total_apr,
            "address": vault.get("address", ""),
            "chain_id": chain_id
        })

    # Sort strategies by SD TVL and keep top 5 per chain
    for chain_id in chain_stats:
        chain_stats[chain_id]["strategies"].sort(
            key=lambda x: x["sd_tvl"],
            reverse=True
        )
        chain_stats[chain_id]["top_strategies"] = chain_stats[chain_id]["strategies"][:5]

    return chain_stats


def format_message(chain_stats: Dict[int, Dict], protocol: str = "curve") -> str:
    """
    Format the stats into a Telegram message.

    Args:
        chain_stats: Chain statistics dictionary
        protocol: Protocol name ("curve" or "balancer")

    Returns:
        Formatted message string
    """
    protocol_display = protocol.capitalize()
    msgs = [f"<u>Stake DAO Staking V2 ({protocol_display}) :</u>\n"]

    # Calculate grand total
    grand_total = sum(stats["total_tvl"] for stats in chain_stats.values())

    # Sort chains by TVL
    sorted_chains = sorted(
        chain_stats.items(),
        key=lambda x: x[1]["total_tvl"],
        reverse=True
    )

    for chain_id, stats in sorted_chains:
        chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
        chain_tvl = stats["total_tvl"]

        # Skip chains with no TVL
        if chain_tvl <= 1000:
            continue

        chain_tvl_formatted = abbreviate_number(chain_tvl)
        msgs.append(f"<b>{chain_name}</b> - TVL: ${chain_tvl_formatted}")

        # Top 5 strategies
        for strat in stats["top_strategies"]:
            if strat["sd_tvl"] <= 0:
                continue

            # Clean asset name
            name = clean_asset_name(strat["name"])
            sd_tvl = strat["sd_tvl"]
            gauge_tvl = strat["gauge_tvl"]
            apr = strat["apr"]
            address = strat["address"]
            strat_chain_id = strat["chain_id"]

            # Build strategy URL with correct protocol
            strat_url = f"https://www.stakedao.org/strategy?protocol={protocol}&vault={strat_chain_id}-{address}"

            # Calculate SD share percentage
            sd_share = (sd_tvl / gauge_tvl * 100) if gauge_tvl > 0 else 0

            sd_tvl_formatted = abbreviate_number(sd_tvl)
            apr_formatted = f"{apr * 100:.2f}%" if apr > 0 else ""

            # Format: <a href='url'>Name</a>: $TVL (X% share) | APR X%
            apr_display = f" APR {apr_formatted}" if apr > 0 else ""

            msgs.append(f"  - <a href='{strat_url}'>{name}</a>: ${sd_tvl_formatted} |{apr_display}")

        msgs.append("")  # Empty line between chains

    # Add grand total
    grand_total_formatted = abbreviate_number(grand_total)
    msgs.append(f"<b>Total TVL: ${grand_total_formatted}</b>")

    return "\n".join(msgs)


def job():
    """Main job function"""
    logging.info("Fetching vaults data...")
    vaults = fetch_vaults_data()

    if not vaults:
        logging.error("No vaults data fetched")
        return

    logging.info(f"Fetched {len(vaults)} vaults")

    curve_vaults = [v for v in vaults if v.get("protocolId", Protocol.CURVE) == Protocol.CURVE]
    logging.info(f"Curve vaults: {len(curve_vaults)}")

    excluded_chains = {146}

    # Excluding vaults from some chains
    curve_vaults = [v for v in curve_vaults if v.get("chainId") not in excluded_chains]

    if curve_vaults:
        curve_stats = calculate_chain_stats(curve_vaults)
        curve_message = format_message(curve_stats, protocol="curve")
        send_telegram_message(
            GlobalConstants.BOT_API_KEY,
            GlobalConstants.TELEGRAM_ACTIVITY_CHANNEL_ID,
            curve_message,
            parse_mode="HTML",
        )
        logging.info(f"Sent Curve message ({len(curve_message)} chars)")


def main():
    job()


if __name__ == "__main__":
    main()
