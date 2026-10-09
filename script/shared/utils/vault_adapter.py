from typing import Dict


from shared.constants import Protocol


PROTOCOL_TO_ID: Dict[str, str] = {
    "curve": Protocol.CURVE,
    "balancer": Protocol.BALANCER,
}


def _extract_sidecar(hub_vault: dict) -> str:
    implementations = (hub_vault.get("onlyboost") or {}).get("implementations", [])
    for impl in implementations:
        if impl.get("key") in ("convex", "aura"):
            return impl.get("address", "")
    return ""


def adapt_hub_vault(hub_vault: dict) -> dict:
    onlyboost = hub_vault.get("onlyboost") or {}
    gauge = hub_vault.get("gauge") or {}
    lp_token = hub_vault.get("lpToken") or {}

    return {
        "address": hub_vault.get("vault", ""),
        "chainId": hub_vault.get("chainId", 1),
        "protocolId": PROTOCOL_TO_ID.get(hub_vault.get("protocol", ""), ""),
        "protocol": hub_vault.get("protocol", ""),
        "totalSupply": hub_vault.get("totalSupply", "0"),
        "totalSupplyUSD": str(hub_vault.get("tvl", 0)),
        "sidecar": _extract_sidecar(hub_vault),
        "sidecarBalance": str(onlyboost.get("sidecar", {}).get("supply", "0")),
        "rewardReceiver": hub_vault.get("rewardReceiver", ""),
        "lpPriceInUsd": hub_vault.get("lpPriceInUsd", 0),
        "asset": {
            "name": lp_token.get("name", ""),
            "symbol": lp_token.get("symbol", ""),
            "address": lp_token.get("address", ""),
            "decimals": lp_token.get("decimals", 18),
        },
        "gauge": {
            "address": gauge.get("address", ""),
            "totalSupply": gauge.get("totalSupply", "0"),
            "totalSupplyUSD": str(gauge.get("totalSupplyUsd", 0)),
        },
        "_hub": hub_vault,
    }


def get_apr_from_hub_vault(hub_vault: dict) -> float:
    pct = float(
        hub_vault.get("apr", {})
        .get("current", {})
        .get("total", 0) or 0
    )
    return pct / 100.0


def get_apr_components_from_hub_vault(hub_vault: dict) -> dict:
    apr = hub_vault.get("apr", {})
    current = apr.get("current", {})
    total_pct = float(current.get("total", 0) or 0)

    trading_apy_pct = 0.0
    dilutable_pct = 0.0
    for detail in current.get("details", []):
        label = detail.get("label", "")
        values = detail.get("value", [])
        value = sum(values) if isinstance(values, list) else float(values or 0)
        if "Trading Fees" in label:
            trading_apy_pct += value
        else:
            dilutable_pct += value

    return {
        "dilutable_apr": dilutable_pct / 100.0,
        "constant_apr": trading_apy_pct / 100.0,
        "total_apr": total_pct / 100.0,
        "boost_multiplier": apr.get("boost", 1.0),
        "fee_deducted": 0.0,
    }
