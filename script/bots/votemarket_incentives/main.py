import json
import logging
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from shared.communication.telegram import send_telegram_message
from shared.constants import ContractRegistry, GlobalConstants
from shared.external.explorer import CHAIN_NAMES, L2_CHAIN_IDS
from shared.services.data_hub_service import fetch_gauge_tvls as _fetch_gauge_tvls
from shared.utils.formatters import format_amount
from shared.utils.globals import get_redis_client, get_token_price, replace_double_quotes_with_single

load_dotenv()
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

# Configuration
REDIS_KEY_SEEN = "tg-bot:votemarket-incentives:seen_ids"
REDIS_KEY_GAUGES = "tg-bot:votemarket-incentives:gauge_names"
REDIS_KEY_TVL = "tg-bot:votemarket-incentives:gauge_tvls"
INCENTIVES_URL = "https://raw.githubusercontent.com/stake-dao/merkl-toolkit/refs/heads/main/data/incentives.json"
VOTEMARKET_API_BASE = "https://votemarket-api.contact-69d.workers.dev"
PROTOCOLS = ["curve", "balancer", "fxn", "pendle", "yb"]
ONE_YEAR_SECONDS = 31536000
SECONDS_PER_DAY = 86400

# Hook addresses - tokens from these senders are bridged to Ethereum
HOOK_ADDRESSES = {
    ContractRegistry.HOOK_INCENTIVE_L2.lower(),
    ContractRegistry.HOOK_INCENTIVE_L2_V2.lower(),
    ContractRegistry.HOOK_INCENTIVE_MERKLE.lower(),
}


def load_seen_ids() -> set:
    """Load seen incentive IDs from Redis.

    Raises on Redis failure instead of returning an empty set: an empty set is
    only valid when Redis explicitly reports the key as absent. Swallowing a
    timeout into set() makes the job treat every incentive as new and spam
    notifications, so the caller must abort instead.
    """
    redis_client = get_redis_client()
    data = redis_client.get(REDIS_KEY_SEEN)
    if data:
        return set(json.loads(data))
    return set()


def save_seen_ids(seen_ids: set):
    """Save seen incentive IDs to Redis"""
    try:
        redis_client = get_redis_client()
        redis_client.set(REDIS_KEY_SEEN, json.dumps(list(seen_ids)))
    except Exception as e:
        logging.error(f"Error saving to Redis: {e}")


def fetch_gauge_data() -> dict:
    """Fetch gauge names and protocols from VoteMarket API.

    Returns dict mapping gauge address (lowercase) -> {"name": str, "protocol": str}
    """
    gauge_map = {}

    for protocol in PROTOCOLS:
        try:
            url = f"{VOTEMARKET_API_BASE}/{protocol}/gauges"
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                data = response.json()
                gauges = data.get("gauges", [])
                for g in gauges:
                    addr = g.get("gauge", "").lower()
                    name = g.get("name", "")
                    if addr and name:
                        gauge_map[addr] = {"name": name, "protocol": protocol}
                logging.info(f"Loaded {len(gauges)} gauges from {protocol}")
        except Exception as e:
            logging.warning(f"Failed to fetch gauges for {protocol}: {e}")

    return gauge_map


def load_gauge_data() -> dict:
    """Load gauge data from Redis cache, refresh if empty"""
    try:
        redis_client = get_redis_client()
        data = redis_client.get(REDIS_KEY_GAUGES)
        if data:
            return json.loads(data)
    except Exception as e:
        logging.error(f"Error loading gauge data from Redis: {e}")

    # Cache miss - fetch from API
    gauge_map = fetch_gauge_data()
    if gauge_map:
        try:
            redis_client = get_redis_client()
            # Cache for 24 hours
            redis_client.setex(REDIS_KEY_GAUGES, 86400, json.dumps(gauge_map))
            logging.info(f"Cached {len(gauge_map)} gauge entries")
        except Exception as e:
            logging.error(f"Error caching gauge data: {e}")

    return gauge_map


def get_gauge_info(gauge_address: str, gauge_map: dict) -> tuple[str, str]:
    """Get gauge name and protocol from map.

    Returns (name, protocol) tuple. Falls back to abbreviated address and 'curve'.
    """
    data = gauge_map.get(gauge_address.lower())
    if data and isinstance(data, dict):
        return data.get("name", ""), data.get("protocol", "curve")
    # Handle legacy format (just name string)
    if data and isinstance(data, str):
        return data, "curve"
    return "", "curve"


# Protocol display names for links
PROTOCOL_LABELS = {
    "curve": "Curve",
    "balancer": "Balancer",
    "fxn": "F(x)",
    "pendle": "Pendle",
    "yb": "YieldBasis",
}


def fetch_gauge_tvls() -> dict:
    """Fetch Stake DAO vault TVLs from DataHub API.

    Returns dict mapping gauge address (lowercase) -> vault TVL in USD.
    """
    try:
        tvl_map = _fetch_gauge_tvls()
        logging.info(f"Loaded TVL for {len(tvl_map)} gauges")
        return tvl_map
    except Exception as e:
        logging.warning(f"Failed to fetch gauge TVLs: {e}")
        return {}


def load_gauge_tvls() -> dict:
    """Load gauge TVLs from Redis cache, refresh if expired"""
    try:
        redis_client = get_redis_client()
        data = redis_client.get(REDIS_KEY_TVL)
        if data:
            return json.loads(data)
    except Exception as e:
        logging.error(f"Error loading gauge TVLs from Redis: {e}")

    # Cache miss - fetch from API
    tvl_map = fetch_gauge_tvls()
    if tvl_map:
        try:
            redis_client = get_redis_client()
            # Cache for 1 hour (TVL changes more frequently than names)
            redis_client.setex(REDIS_KEY_TVL, 3600, json.dumps(tvl_map))
            logging.info(f"Cached TVL for {len(tvl_map)} gauges")
        except Exception as e:
            logging.error(f"Error caching gauge TVLs: {e}")

    return tvl_map


def calculate_apr(amount_usd: float, tvl: float, duration_seconds: int) -> float:
    """Calculate estimated APR for an incentive.

    Formula: APR = (amountUsd × ONE_YEAR) / (TVL × durationSeconds)
    Returns APR as decimal (e.g., 0.05 for 5%)
    """
    if tvl <= 0 or duration_seconds <= 0:
        return 0
    return (amount_usd * ONE_YEAR_SECONDS) / (tvl * duration_seconds)


def fetch_incentives():
    """Fetch incentives data from GitHub"""
    try:
        response = requests.get(INCENTIVES_URL, timeout=30)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Failed to fetch incentives: HTTP {response.status_code}")
            return []
    except Exception as e:
        logging.error(f"Error fetching incentives: {e}")
        return []


def get_source_type(chain_id: int, sender: str = "") -> tuple[str, str, str]:
    """Determine source type and chain label for incentive notification.

    Returns (source_label, emoji, chain_name):
        - L2 origin (not from hook): VoteMarket + L2 chain name
        - L2 origin (from hook): VoteMarket + "Ethereum" (bridged to mainnet)
        - Mainnet origin: Direct Rewards + "Ethereum"
    """
    # Tokens from hook contracts are bridged to Ethereum
    from_hook = sender.lower() in HOOK_ADDRESSES if sender else False

    if chain_id in L2_CHAIN_IDS:
        chain_name = "Ethereum" if from_hook else CHAIN_NAMES.get(chain_id, "Unknown")
        return "Votemarket", "🗳️", chain_name
    return "Direct Rewards", "🎯", "Ethereum"


def format_date_short(timestamp) -> str:
    """Format timestamp to short date (e.g., '29 Jan')"""
    try:
        ts = int(timestamp)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%d %b")
    except (ValueError, TypeError):
        return "?"


def format_tvl(tvl: float) -> str:
    """Format TVL value with appropriate suffix"""
    if tvl >= 1_000_000:
        return f"${tvl/1_000_000:.2f}M"
    elif tvl >= 1_000:
        return f"${tvl/1_000:.2f}K"
    else:
        return f"${tvl:,.2f}"


def send_incentive_notification(incentive, gauge_map: dict, tvl_map: dict):
    """Send Telegram notification for a new incentive"""
    chain_id = int(incentive.get("fromChainId", 1))
    # Always use etherscan for links since rewards are distributed on mainnet
    explorer = "https://etherscan.io"

    gauge = incentive.get("gauge", "")
    vault = incentive.get("vault", "")
    reward_token = incentive.get("reward", "")
    reward_symbol = incentive.get("rewardSymbol", "TOKEN")
    reward_decimals = int(incentive.get("rewardDecimals", 18))
    amount = incentive.get("amount", "0")
    duration = int(incentive.get("duration", 0))
    start = incentive.get("start", "")
    end = incentive.get("end", "")
    sender = incentive.get("sender", "")

    # Format amount
    amount_formatted = format_amount(amount, reward_decimals)

    # Calculate duration in days
    duration_days = duration // SECONDS_PER_DAY
    duration_text = f"{duration_days} days" if duration_days != 1 else "1 day"
    duration_with_dates = f"{duration_text} ({format_date_short(start)} → {format_date_short(end)})"

    # Get USD value (use mainnet for price lookup)
    usd_value = ""
    amount_usd = 0.0
    if reward_token:
        try:
            _, price = get_token_price(
                chain_id=1,  # Mainnet for price
                token=reward_token,
                unformatted_amount=int(amount),
            )
            if price > 0:
                amount_usd = price
                usd_value = f" (~${price:,.0f})" if price >= 1 else f" (~${price:,.2f})"
        except Exception as e:
            logging.debug(f"Could not get price for {reward_symbol}: {e}")

    # Determine source type and chain label
    source_label, source_emoji, chain_name = get_source_type(chain_id, sender)

    # Get gauge name and protocol
    gauge_name, protocol = get_gauge_info(gauge, gauge_map)
    if not gauge_name:
        gauge_name = gauge[:10] + "..." + gauge[-6:]  # Abbreviated address
    protocol_label = PROTOCOL_LABELS.get(protocol, protocol.capitalize())

    # Get TVL and calculate APR
    tvl = tvl_map.get(gauge.lower(), 0)
    apr = calculate_apr(amount_usd, tvl, duration) if amount_usd > 0 and tvl > 0 else 0

    # Build message with new format
    msg = f"{source_emoji} {source_label} | {chain_name}\n\n"
    msg += f"Gauge: {gauge_name}\n"
    msg += f"Reward: {amount_formatted} {reward_symbol}{usd_value}\n"
    msg += f"Duration: {duration_with_dates}\n"

    # TVL and APR on same line
    if tvl > 0:
        tvl_formatted = format_tvl(tvl)
        if apr > 0:
            apr_pct = apr * 100
            msg += f"TVL: {tvl_formatted} | Est. APR: ~{apr_pct:.2f}%\n"
        else:
            msg += f"TVL: {tvl_formatted}\n"

    # Links section
    links = []
    if vault:
        links.append(f"<a href='https://www.stakedao.org/strategy?protocol={protocol}&vault=1-{vault}'>Stake DAO</a>")
        links.append(f"<a href='{explorer}/address/{gauge}'>{protocol_label} gauge</a>")
        links.append(f"<a href='{explorer}/address/{vault}'>Vault</a>")
    else:
        links.append(f"<a href='{explorer}/address/{gauge}'>{protocol_label} gauge</a>")

    if links:
        msg += f"Links: {' | '.join(links)}"

    send_telegram_message(
        GlobalConstants.BOT_VOTEMARKET_API_KEY,
        GlobalConstants.VOTEMARKET_CHANNEL_ID,
        replace_double_quotes_with_single(msg),
    )

    logging.info(f"Sent notification for incentive ID {incentive.get('id')}: {amount_formatted} {reward_symbol}")


def job(start_from_timestamp=None):
    """Main job function

    Args:
        start_from_timestamp: Optional unix timestamp to start tracking from.
                            If provided, will notify for all incentives started after this time.
    """
    try:
        seen_ids = load_seen_ids()
    except Exception as e:
        # Fail closed: if we can't read what we've already notified, do NOT
        # proceed — an empty set here would re-notify every incentive.
        logging.error(f"Aborting job: could not load seen IDs from Redis: {e}")
        return

    gauge_map = load_gauge_data()
    tvl_map = load_gauge_tvls()

    incentives = fetch_incentives()
    if not incentives:
        logging.warning("No incentives data fetched")
        return

    logging.info(f"Fetched {len(incentives)} total incentives, {len(seen_ids)} already seen, {len(gauge_map)} gauges, {len(tvl_map)} TVLs cached")

    # Sort by ID to process in order
    incentives_sorted = sorted(incentives, key=lambda x: int(x.get("id", 0)))

    new_count = 0
    for incentive in incentives_sorted:
        incentive_id = int(incentive.get("id", 0))

        # Skip already seen
        if incentive_id in seen_ids:
            continue

        # If start_from_timestamp provided, filter by start time
        if start_from_timestamp:
            start_time = int(incentive.get("start", 0))
            if start_time < start_from_timestamp:
                seen_ids.add(incentive_id)
                continue

        # Send notification
        try:
            send_incentive_notification(incentive, gauge_map, tvl_map)
            new_count += 1
            time.sleep(1)  # Rate limiting
        except Exception as e:
            logging.error(f"Error sending notification for incentive {incentive_id}: {e}")

        seen_ids.add(incentive_id)

    # Save to Redis
    save_seen_ids(seen_ids)

    logging.info(f"Job completed. Sent {new_count} new notifications.")


def main():
    """Entry point - normal run tracking new incentives"""
    job()


if __name__ == "__main__":
    main()
