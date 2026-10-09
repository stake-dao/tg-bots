NATIVE_TOKENS = frozenset({"ETH", "WETH", "BTC", "WBTC"})


def format_amount(
    amount_str: str | int | float,
    decimals: int = 0,
    *,
    token_type: str = "auto",
    symbol: str | None = None,
) -> str:
    """Format token amount with unified decimals and thousand separators.

    Args:
        amount_str: Raw amount (wei/smallest unit if decimals > 0, else float already)
        decimals: Token decimals (0 if amount_str is already scaled float)
        token_type: "auto" uses magnitude-based precision (0/2/4 decimals).
                    "native" forces 4 decimals (ETH/WETH/BTC/WBTC precision).
        symbol: Optional token symbol — overrides token_type to "native" when
                symbol in NATIVE_TOKENS.

    Returns:
        Formatted amount string. All outputs include thousand separators.
    """
    try:
        raw = int(amount_str) if decimals else float(amount_str)
        amount = raw / (10**decimals) if decimals else raw

        if symbol and symbol.upper() in NATIVE_TOKENS:
            token_type = "native"

        if token_type == "native":
            return f"{amount:,.4f}"

        abs_amount = abs(amount)
        if abs_amount >= 1_000_000:
            return f"{amount:,.0f}"
        if abs_amount >= 1:
            return f"{amount:,.2f}"
        return f"{amount:,.4f}"
    except Exception:
        return str(amount_str)
