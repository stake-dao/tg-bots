from typing import TypedDict


class AprComponents(TypedDict):
    dilutable_apr: float
    constant_apr: float
    total_apr: float
    boost_multiplier: float
    fee_deducted: float


def adjust_apr_for_tvl(
    apr_components: AprComponents,
    current_tvl: float,
    historical_tvl: float,
) -> AprComponents:
    """
    Adjust APR components from current TVL to a historical TVL.

    Dilutable APR scales inversely with TVL.
    Constant APR (trading fees) remains unchanged.
    """
    if current_tvl <= 0 or historical_tvl <= 0:
        return apr_components

    tvl_ratio = current_tvl / historical_tvl

    return {
        "dilutable_apr": apr_components["dilutable_apr"] * tvl_ratio,
        "constant_apr": apr_components["constant_apr"],
        "total_apr": (
            apr_components["dilutable_apr"] * tvl_ratio
            + apr_components["constant_apr"]
            - apr_components["fee_deducted"] * tvl_ratio
        ),
        "boost_multiplier": apr_components["boost_multiplier"],
        "fee_deducted": apr_components["fee_deducted"] * tvl_ratio,
    }


def calculate_projected_apr(
    apr_components: AprComponents,
    tvl_before: float,
    tvl_after: float,
) -> float:
    """
    Calculate projected APR after a TVL change (deposit/withdrawal).

    Dilutable APR scales inversely with TVL.
    Constant APR remains unchanged.
    Fees scale with the dilutable APR they were deducted from.
    """
    if tvl_after <= 0 or tvl_before <= 0:
        return apr_components["total_apr"]

    tvl_ratio = tvl_before / tvl_after
    projected_dilutable = apr_components["dilutable_apr"] * tvl_ratio
    projected_constant = apr_components["constant_apr"]
    projected_fee = apr_components["fee_deducted"] * tvl_ratio

    return projected_dilutable + projected_constant - projected_fee
