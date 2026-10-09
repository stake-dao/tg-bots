import logging


import time


from typing import Dict


from shared.address import format_eth_address


from web3 import Web3


logger = logging.getLogger(__name__)


SAFE_GET_THRESHOLD_SELECTOR = "0xe75235b8"


_safe_cache: Dict[str, tuple] = {}  # {address: (is_safe, timestamp)}


_CACHE_TTL = 3600  # 1 hour


def _ttl_cache_get(address: str) -> tuple:
    """Get cached value if not expired."""
    key = address.lower()
    if key in _safe_cache:
        is_safe, cached_time = _safe_cache[key]
        if time.time() - cached_time < _CACHE_TTL:
            return (True, is_safe)  # (found, value)
    return (False, None)


def _ttl_cache_set(address: str, is_safe: bool) -> None:
    """Set cache value with current timestamp."""
    _safe_cache[address.lower()] = (is_safe, time.time())


def is_safe_multisig(address: str, web3: Web3) -> bool:
    """
    Check if an address is a Safe multisig by calling getThreshold().

    Safe contracts implement getThreshold() which returns the number of
    required signatures (>= 1). Non-Safe contracts will revert.

    Args:
        address: Ethereum address to check
        web3: Web3 instance for making RPC calls

    Returns:
        True if address is a Safe multisig, False otherwise
    """
    # Check cache first
    found, cached_value = _ttl_cache_get(address)
    if found:
        return cached_value

    try:
        result = web3.eth.call({
            "to": Web3.to_checksum_address(address),
            "data": SAFE_GET_THRESHOLD_SELECTOR
        })
        threshold = int.from_bytes(result, "big")
        is_safe = threshold >= 1
    except Exception as e:
        logger.debug(f"Safe check failed for {address}: {e}")
        is_safe = False

    _ttl_cache_set(address, is_safe)
    return is_safe


def format_user_info(
    owner: str,
    executor: str,
    is_safe: bool,
    etherscan_url: str = "https://etherscan.io"
) -> str:
    """
    Format user info for Telegram message with Safe detection.

    Args:
        owner: The owner address (Safe address or EOA)
        executor: The transaction executor address (tx.from)
        is_safe: Whether the owner is a Safe multisig
        etherscan_url: Base Etherscan URL for links

    Returns:
        Formatted HTML string:
        - If Safe: "🔒 Safe: <link> | Executor: <link>\n"
        - If EOA:  "User: <link>\n"
    """
    if is_safe:
        safe_formatted = format_eth_address(owner)
        executor_formatted = format_eth_address(executor) if executor else "Unknown"

        safe_part = f"🔒 Safe: <a href='{etherscan_url}/address/{owner}'>{safe_formatted}</a>"
        if executor:
            executor_part = f"Executor: <a href='{etherscan_url}/address/{executor}'>{executor_formatted}</a>"
            return f"{safe_part} | {executor_part}\n"
        return f"{safe_part}\n"
    else:
        user_formatted = format_eth_address(owner)
        return f"User: <a href='{etherscan_url}/address/{owner}'>{user_formatted}</a>\n"
