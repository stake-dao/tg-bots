import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from shared.services.morpho_api import VaultDailyData, get_morpho_api_service
from shared.bot_runner import run_bot, setup_bot
from shared.communication.telegram import send_telegram_message
from shared.constants import ContractRegistry, GlobalConstants
from shared.strings import abbreviate_number

setup_bot()

LENDING_URL = "https://lending.stakedao.org/earn"


@dataclass
class VaultConfig:
    address: str
    name: str


VAULTS = [
    VaultConfig(
        address=ContractRegistry.STAKEDAO_MORPHO_USDC_V2[1],
        name="Stake DAO USDC",
    ),
    VaultConfig(
        address=ContractRegistry.STAKEDAO_MORPHO_FRXUSD_V2[1],
        name="Stake DAO frxUSD",
    ),
]


def format_tvl_delta(current: float, yesterday: Optional[float]) -> str:
    if yesterday is None:
        return ""
    delta = current - yesterday
    sign = "+" if delta >= 0 else "-"
    return f" ({sign}${abbreviate_number(abs(delta))})"


def format_message(rows: List[Tuple[VaultConfig, VaultDailyData]]) -> str:
    msgs = ["<u>Stake DAO Morpho :</u>\n"]

    total_tvl = sum(d.tvl_usd for _, d in rows)

    delta_sum = None
    for _, d in rows:
        if d.tvl_usd_yesterday is not None:
            if delta_sum is None:
                delta_sum = 0.0
            delta_sum += d.tvl_usd - d.tvl_usd_yesterday

    chain_tvl_formatted = abbreviate_number(total_tvl)
    if delta_sum is not None:
        sign = "+" if delta_sum >= 0 else "-"
        chain_delta = f" ({sign}${abbreviate_number(abs(delta_sum))})"
    else:
        chain_delta = ""

    msgs.append(f"<b>Ethereum</b> - TVL: ${chain_tvl_formatted}{chain_delta}")

    for vault, data in rows:
        tvl_formatted = abbreviate_number(data.tvl_usd)
        apy_pct = data.net_apy * 100
        msgs.append(
            f"  - <a href='{LENDING_URL}'>{vault.name}</a>: ${tvl_formatted} | APY {apy_pct:.2f}%"
        )

    msgs.append("")
    total_formatted = abbreviate_number(total_tvl)
    msgs.append(f"<b>Total TVL: ${total_formatted}</b>")

    return "\n".join(msgs)


def job():
    logging.info("Fetching Morpho vault data...")
    morpho_api = get_morpho_api_service()

    rows: List[Tuple[VaultConfig, VaultDailyData]] = []
    missing: List[str] = []
    for vault in VAULTS:
        data = morpho_api.get_vault_full_data(vault.address)
        if data is None or data.tvl_usd == 0:
            logging.warning(f"Missing {vault.name}: no data or zero TVL")
            missing.append(vault.name)
            continue
        logging.info(
            f"  {vault.name}: TVL=${data.tvl_usd:,.0f} | APY={data.net_apy*100:.2f}%"
        )
        rows.append((vault, data))

    if missing:
        logging.error(f"Aborting: missing vault data for {', '.join(missing)}")
        return

    rows.sort(key=lambda x: x[1].tvl_usd, reverse=True)

    message = format_message(rows)
    logging.info(f"Sending message ({len(message)} chars):\n{message}")

    send_telegram_message(
        GlobalConstants.BOT_API_KEY,
        GlobalConstants.TELEGRAM_ACTIVITY_CHANNEL_ID,
        message,
        parse_mode="HTML",
    )


def main():
    run_bot(job, "morpho_daily")


if __name__ == "__main__":
    main()
