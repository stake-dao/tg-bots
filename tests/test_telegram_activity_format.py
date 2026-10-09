from types import SimpleNamespace

from bots.utils.telegram_format import format_activity_header


def test_format_activity_header_matches_compact_preview_format():
    assert (
        format_activity_header("frxUSD", "Ethereum", "🦋")
        == "🦋 frxUSD | Ethereum\n"
    )


def test_morpho_vault_header_uses_asset_symbol_without_brand_prefix():
    from bots.morpho.main import format_vault_header, VAULTS

    assert format_vault_header(VAULTS[1]) == "🦋 frxUSD | Ethereum\n"


def test_morpho_market_header_uses_pair_without_market_prefix():
    from bots.morpho.main import format_market_header

    market = SimpleNamespace(collateral_symbol="sdCRV", loan_symbol="USDC")

    assert format_market_header(market) == "🦋 sdCRV/USDC | Ethereum\n"


def test_asdcrv_header_uses_market_asset_without_lending_prefix():
    from bots.asdcrv.main import MSG_HEADER

    assert MSG_HEADER == "asdcrv | Arbitrum\n"
