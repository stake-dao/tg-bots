from eth_utils import is_address


from typing import Optional


import logging


logger = logging.getLogger(__name__)


_ens = None


def _get_ens():
    """Return the module-level ENS singleton, initializing it lazily if needed."""
    global _ens
    if _ens is None:
        from ens.auto import ns
        _ens = ns
    return _ens


def format_eth_address(
    address: str,
    check_label: bool = True,
    check_ens: bool = True,
    ens_instance: Optional[object] = None
) -> str:
    """
    Format an Ethereum address for display with ENS, label, or abbreviation.

    Priority order:
    1. ENS name (if enabled and available)
    2. Known label from ADDRESSES_TO_LABEL (if enabled)
    3. Abbreviated address (0x1234...5678)

    Args:
        address: The Ethereum address to format
        check_label: Whether to check for known labels (default: True)
        check_ens: Whether to check for ENS name (default: True)
        ens_instance: Optional ENS instance to use for resolution

    Returns:
        str: ENS name, label, or abbreviated address

    Raises:
        ValueError: If the provided string is not a valid Ethereum address
    """
    if not is_address(address):
        raise ValueError("Wrong Ethereum address")

    # Check for ENS name if enabled
    if check_ens:
        try:
            ens = ens_instance or _get_ens()
            ens_name = ens.name(address)
            if ens_name:
                return ens_name
        except Exception as e:
            # Log but don't fail - ENS resolution is optional
            logger.debug(f"ENS resolution failed for {address}: {e}")

    # Check for known label if enabled
    if check_label:
        try:
            from .constants import Common
            label = Common.ADDRESSES_TO_LABEL.get(address.lower())
            if label:
                return label
        except ImportError:
            # If constants can't be imported, just abbreviate
            pass

    # Return abbreviated address
    return f"{address[:6]}...{address[-4:]}"
