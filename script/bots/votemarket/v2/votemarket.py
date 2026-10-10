import asyncio
import logging
import time

from shared.bot_runner import get_block_range
from dotenv import load_dotenv
from eth_utils import to_checksum_address
from shared.services.stakedao_api import StakeDAOApiService
from shared.services.web3_service import get_web3_service
from shared.communication.telegram import send_telegram_message
from shared.constants import (
    ZERO_ADDRESS,
    Common,
    ContractRegistry,
    GlobalConstants,
)
from shared.external.explorer import get_explorer_link
from shared.utils.formatters import format_amount
from shared.utils.globals import (
    getHistoricalTokenPrice,
    load_json,
    replace_double_quotes_with_single,
)
from w3multicall.multicall import W3Multicall
from web3 import Web3

load_dotenv()
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

WORKFLOW_NAME = "votemarket-v2"
# Arbitrum RPC rejects getLogs queries spanning more than 30,000 blocks.
LOG_BLOCK_BATCH_SIZE = 30000

KNOW_ADDRESSES = [
    # Curve
    {"address": ContractRegistry.CONVEX_VOTER[1], "label": "vlCVX"},
    {"address": ContractRegistry.CRV_LL[1], "label": "sdCRV"},
    {"address": ContractRegistry.YEARN_VOTER[1], "label": "yCRV"},
    # Balancer
    {"address": ContractRegistry.BALANCER_LOCKER[1], "label": "sdBAL"},
    {"address": ContractRegistry.TETU_LOCKER[1], "label": "tetuBAL"},
    # FXN
    {"address": ContractRegistry.FXN_LOCKER[1], "label": "sdFXN"},
    {"address": ContractRegistry.FXN_CONVEX_LL[1], "label": "cvxFXN"},
    # Pendle
    {"address": ContractRegistry.PENDLE_LOCKER[1], "label": "sdPENDLE"},
    # Yield Basis
    {"address": ContractRegistry.YB_LOCKER[1], "label": "sdYB"},
]

abis = [
    load_json("abi/platformVmV2"),
    load_json("abi/platformVmV2_V2"),
]

web3_service = get_web3_service(42161)  # Default Arbitrum

gauges_endpoint_data_cache = {}


def get_abi(platform_address):
    if (
        platform_address.lower()
        == "0x5e5C922a5Eeab508486eB906ebE7bDFFB05D81e5".lower()
    ):
        return abis[0]
    return abis[1]


def fetch_token_price(native_token_address, chain_id, token_address):
    real_token_address = native_token_address
    chain_name = ""
    if native_token_address.lower() == ZERO_ADDRESS.lower():
        real_token_address = token_address
        if chain_id == 42161:
            chain_name = "arbitrum:"
        elif chain_id == 8453:
            chain_name = "base:"
        elif chain_id == 137:
            chain_name = "polygon:"
        else:
            chain_name = "op:"
    else:
        chain_name = "ethereum:"

    return getHistoricalTokenPrice(
        real_token_address, int(time.time()), chain_name
    )


def remove_p_prefix(native_token_address, symbol):
    if native_token_address.lower() != ZERO_ADDRESS.lower():
        # It's a wrapped token, remove the 'p'
        if symbol[0] == "p":
            return symbol[1:]
    return symbol


async def get_gauge_info(w3, platform_address, gauges_endpoint, gauge_address):
    """Get gauge name and chain ID from gauge info"""
    protocol = gauges_endpoint[to_checksum_address(platform_address)]
    gauge_endpoint = f"{protocol}/gauges"
    gauge_name = ""
    gauge_chain_id = None  # Default to None - no chain ID found

    if gauge_endpoint not in gauges_endpoint_data_cache:
        gauges_endpoint_data_cache[gauge_endpoint] = await StakeDAOApiService(
            w3
        ).get_gauges_data(protocol)

    gauges_data = gauges_endpoint_data_cache[gauge_endpoint]

    # Check if there's a default chainId in the response
    if isinstance(gauges_data, dict):
        if "chainId" in gauges_data:
            gauge_chain_id = gauges_data["chainId"]

        # Look for the specific gauge
        if "gauges" in gauges_data:
            for gauge in gauges_data["gauges"]:
                if gauge["gauge"].lower() == gauge_address.lower():
                    gauge_name = gauge.get("name", "")
                    # Check if individual gauge has chainId (priority order)
                    if "chainId" in gauge:
                        gauge_chain_id = gauge["chainId"]
                    elif "gaugeChainId" in gauge:
                        gauge_chain_id = gauge["gaugeChainId"]
                    break

    return gauge_name, gauge_chain_id


async def get_campaigns_created(
    web3,
    chain_id,
    platform_address,
    from_block,
    to_block,
    names,
    gauges_endpoint,
):
    platformContract = web3.eth.contract(
        address=Web3.to_checksum_address(platform_address),
        abi=get_abi(platform_address),
    )

    campaign_created_logs = []
    for start in range(from_block, to_block + 1, LOG_BLOCK_BATCH_SIZE):
        campaign_created_logs.extend(
            platformContract.events.CampaignCreated().get_logs(
                from_block=start,
                to_block=min(start + LOG_BLOCK_BATCH_SIZE - 1, to_block),
            )
        )

    multicall = W3Multicall(web3)

    for log in campaign_created_logs:
        reward_token = log["args"]["rewardToken"]
        campaign_id = log["args"]["campaignId"]

        # Fetch token decimals & symbol
        multicall.add(W3Multicall.Call(reward_token, "decimals()(uint8)", []))

        # Fetch directly on-chain
        multicall.add(W3Multicall.Call(reward_token, "symbol()(string)", []))

        multicall.add(
            W3Multicall.Call(
                to_checksum_address(
                    ContractRegistry.get_address(
                        "TOKEN_FACTORY_VM_V2", chain_id
                    )
                ),
                "nativeTokens(address)(address)",
                [reward_token],
            )
        )

        multicall.add(
            W3Multicall.Call(
                platform_address,
                "getAddressesByCampaign(uint256)(address[])",
                [campaign_id],
            )
        )

        multicall.add(
            W3Multicall.Call(
                platform_address, "whitelistOnly(uint256)(bool)", [campaign_id]
            )
        )

    multicall_responses = multicall.call()

    for i in range(len(campaign_created_logs)):
        log = campaign_created_logs[i]
        args = log["args"]

        campaign_id = args["campaignId"]
        gauge = args["gauge"]
        reward_token = args["rewardToken"]
        number_of_periods = args["numberOfPeriods"]
        total_reward_amount = args["totalRewardAmount"]

        decimals = multicall_responses[0]
        symbol = multicall_responses[1]
        native_token_address = multicall_responses[2]
        blacklist = multicall_responses[3]
        whitelist_only = multicall_responses[4]

        multicall_responses = multicall_responses[5:]

        # It's a wrapped token, remove the 'p'
        symbol = remove_p_prefix(native_token_address, symbol)

        # Fetch token price
        token_price = fetch_token_price(
            native_token_address, chain_id, reward_token
        )

        token_amount = total_reward_amount / (10**decimals)
        reward_per_round = token_amount / number_of_periods

        token_amount_formatted = format_amount(token_amount, symbol=symbol)
        reward_per_round_formatted = format_amount(reward_per_round, symbol=symbol)
        token_amount_price_formatted = format_amount(token_price * token_amount)
        reward_per_round_price_formatted = format_amount(token_price * reward_per_round)

        tx_link = f'<a href="{get_explorer_link(chain_id)}/tx/{Web3.to_hex(log["transactionHash"])}">🔗 Tx Hash</a>'
        gauge_name, gauge_chain_id = await get_gauge_info(
            web3, platform_address, gauges_endpoint, gauge
        )

        platform_name = names[to_checksum_address(platform_address)]
        protocol = gauges_endpoint[to_checksum_address(platform_address)]

        # Only create link if we have a gauge chain ID
        if gauge_chain_id is not None:
            campaign_link = f'<a href="https://votemarket.stakedao.org/{protocol}/gauge/{gauge_chain_id}-{gauge}">campaign {campaign_id}</a>'
            msg = f"Created {platform_name} {campaign_link} : {tx_link}\n\n"
        else:
            msg = f"Created {platform_name} campaign {campaign_id} : {tx_link}\n\n"

        if len(gauge_name) > 0:
            msg += f"Gauge name : {gauge_name}\n"

        msg += f"Gauge address : {gauge}\n"
        msg += f"Number of weeks : {number_of_periods}\n"
        msg += f"Token amount : {token_amount_formatted} {symbol} / ${token_amount_price_formatted}\n"
        msg += f"Rewards per week : {reward_per_round_formatted} {symbol} / ${reward_per_round_price_formatted}\n"

        # Filter out executor address from voter display
        blacklist = [
            addr for addr in blacklist
            if addr.lower() != ContractRegistry.EXECUTOR[1].lower()
        ]

        # Check blacklist / whitelist
        voterLabels = []
        if whitelist_only:
            for addressBlacklist in blacklist:
                label = ""
                for know_address in KNOW_ADDRESSES:
                    if (
                        know_address["address"].lower()
                        == addressBlacklist.lower()
                    ):
                        label = know_address["label"]
                        break

                if len(label) == 0:
                    # Unknow address
                    label = f"{addressBlacklist[0:6]}...{addressBlacklist[len(addressBlacklist)-4:]}"
                voterLabels.append(label)

            votersLabel = " & ".join(voterLabels)
            msg += f"Voters : {votersLabel}"
        elif platform_name == "Curve":
            is_vlcvx_blacklisted = False
            for addressBlacklist in blacklist:
                label = ""

                is_convex = (
                    addressBlacklist.lower() == ContractRegistry.CONVEX_VOTER[1].lower()
                )
                if is_vlcvx_blacklisted == False:
                    is_vlcvx_blacklisted = is_convex

                if is_convex:
                    continue

                for know_address in KNOW_ADDRESSES:
                    if (
                        know_address["address"].lower()
                        == addressBlacklist.lower()
                    ):
                        label = know_address["label"]
                        break

                if len(label) == 0:
                    # Unknow address
                    label = f"{addressBlacklist[0:6]}...{addressBlacklist[len(addressBlacklist)-4:]}"
                voterLabels.append(label)

            msg += "Voters : "
            if is_vlcvx_blacklisted:
                msg += f"veCRV"
            else:
                msg += "veCRV & vlCVX"

            if len(voterLabels) > 0:
                votersLabel = " & ".join(voterLabels)
                msg += f" (blacklisted : {votersLabel})"
        elif platform_name == "Balancer":
            for addressBlacklist in blacklist:
                label = ""

                for know_address in KNOW_ADDRESSES:
                    if (
                        know_address["address"].lower()
                        == addressBlacklist.lower()
                    ):
                        label = know_address["label"]
                        break

                if len(label) == 0:
                    label = f"{addressBlacklist[0:6]}...{addressBlacklist[len(addressBlacklist)-4:]}"
                voterLabels.append(label)

            msg += "Voters : veBAL"

            if len(voterLabels) > 0:
                votersLabel = " & ".join(voterLabels)
                msg += f" (blacklisted : {votersLabel})"
        elif len(blacklist) > 0:
            for addressBlacklist in blacklist:
                label = ""
                for know_address in KNOW_ADDRESSES:
                    if (
                        know_address["address"].lower()
                        == addressBlacklist.lower()
                    ):
                        label = know_address["label"]
                        break

                if len(label) == 0:
                    label = f"{addressBlacklist[0:6]}...{addressBlacklist[len(addressBlacklist)-4:]}"
                voterLabels.append(label)

            votersLabel = " & ".join(voterLabels)
            msg += f"Blacklisted : {votersLabel}"


        send_telegram_message(
            GlobalConstants.BOT_VOTEMARKET_API_KEY,
            GlobalConstants.VOTEMARKET_CHANNEL_ID,
            replace_double_quotes_with_single(msg),
        )


async def get_campaigns_increased(
    web3,
    chain_id,
    platform_address,
    from_block,
    to_block,
    names,
    gauges_endpoint,
):
    platformContract = web3.eth.contract(
        address=Web3.to_checksum_address(platform_address),
        abi=get_abi(platform_address),
    )

    campaign_increased_logs = []
    for start in range(from_block, to_block + 1, LOG_BLOCK_BATCH_SIZE):
        campaign_increased_logs.extend(
            platformContract.events.CampaignUpgradeQueued().get_logs(
                from_block=start,
                to_block=min(start + LOG_BLOCK_BATCH_SIZE - 1, to_block),
            )
        )

    multicall = W3Multicall(web3)

    # Fetch campaign data
    previous_campaign_upgrade_by_ids = []
    for log in campaign_increased_logs:
        blockNumber = log["blockNumber"]
        args = log["args"]
        compaign_id = args["campaignId"]
        epoch_updated = args["epoch"]

        # Fetch campaign in queue + campaign
        previousCampaignUpgradeById = (
            platformContract.functions.campaignUpgradeById(
                compaign_id, epoch_updated
            ).call(block_identifier=blockNumber - 1)
        )
        previous_campaign_upgrade_by_ids.append(previousCampaignUpgradeById)

        multicall.add(
            W3Multicall.Call(
                platform_address,
                "campaignUpgradeById(uint256,uint256)((uint8,uint256,uint256,uint256))",
                [compaign_id, epoch_updated],
            )
        )

        # Fetch directly on-chain
        multicall.add(
            W3Multicall.Call(
                platform_address,
                "campaignById(uint256)((uint256,address,address,address,uint8,uint256,uint256,uint256,uint256,uint256,address))",
                [compaign_id],
            )
        )

    campaign_multicall_responses = multicall.call()

    # Tokens data
    multicall = W3Multicall(web3)
    i = 0
    for log in campaign_increased_logs:
        campaign = campaign_multicall_responses[i + 1]
        reward_token = campaign[3]
        # Fetch token decimals & symbol
        multicall.add(W3Multicall.Call(reward_token, "decimals()(uint8)", []))

        # Fetch directly on-chain
        multicall.add(W3Multicall.Call(reward_token, "symbol()(string)", []))

        multicall.add(
            W3Multicall.Call(
                to_checksum_address(
                    ContractRegistry.get_address(
                        "TOKEN_FACTORY_VM_V2", chain_id
                    )
                ),
                "nativeTokens(address)(address)",
                [reward_token],
            )
        )

        i += 2

    tokens_multicall_responses = multicall.call()

    for log in campaign_increased_logs:
        args = log["args"]
        compaign_id = args["campaignId"]
        epoch_updated = args["epoch"]

        # Campaign data
        previous_campaign_upgrade_by_id = previous_campaign_upgrade_by_ids[0]
        previous_campaign_upgrade_by_ids = previous_campaign_upgrade_by_ids[1:]

        campaign_upgraded = campaign_multicall_responses[0]
        campaign = campaign_multicall_responses[1]
        campaign_multicall_responses = campaign_multicall_responses[2:]

        # Token data
        decimals = tokens_multicall_responses[0]
        symbol = tokens_multicall_responses[1]
        native_token_address = tokens_multicall_responses[2]
        tokens_multicall_responses = tokens_multicall_responses[3:]

        # It's a wrapped token, remove the 'p'
        symbol = remove_p_prefix(native_token_address, symbol)

        # Fetch token price
        token_price = fetch_token_price(
            native_token_address, chain_id, campaign[3]
        )

        if previous_campaign_upgrade_by_id[0] > 0:
            reward_added = (
                campaign_upgraded[1] - previous_campaign_upgrade_by_id[1]
            ) / (10**decimals)
            period_added = (
                campaign_upgraded[0] - previous_campaign_upgrade_by_id[0]
            )
        else:
            reward_added = (campaign_upgraded[1] - campaign[6]) / (
                10**decimals
            )
            period_added = campaign_upgraded[0] - campaign[4]

        reward_added_formatted = format_amount(reward_added, symbol=symbol)
        reward_added_price_formatted = format_amount(reward_added * token_price)

        tx_link = f'<a href="{get_explorer_link(chain_id)}/tx/{Web3.to_hex(log["transactionHash"])}">🔗 Tx Hash</a>'

        gauge = campaign[1]
        gauge_name, gauge_chain_id = await get_gauge_info(
            web3, platform_address, gauges_endpoint, gauge
        )

        platform_name = names[to_checksum_address(platform_address)]
        protocol = gauges_endpoint[to_checksum_address(platform_address)]

        # Only create link if we have a gauge chain ID
        if gauge_chain_id is not None:
            campaign_link = f'<a href="https://votemarket.stakedao.org/{protocol}/gauge/{gauge_chain_id}-{gauge}">campaign {compaign_id}</a>'
            msg = f"{platform_name} {campaign_link} increased : {tx_link}\n\n"
        else:
            msg = f"{platform_name} campaign {compaign_id} increased : {tx_link}\n\n"

        if len(gauge_name) > 0:
            msg += f"Gauge name : {gauge_name}\n"

        msg += f"Gauge address : {gauge}\n"

        # If the max price changed, log it
        new_max_price = campaign_upgraded[2] / (10**decimals)
        new_max_price_formatted = "{:,.6f}".format(new_max_price)
        if campaign[5] != campaign_upgraded[2]:
            msg += f"New max price : {new_max_price_formatted} {symbol}\n"
        else:
            msg += f"Max price : {new_max_price_formatted} {symbol}\n"

        if period_added > 0:
            msg += f"Periods added : {period_added}\n"

        if reward_added > 0:
            msg += f"Reward added : {reward_added_formatted} {symbol} / ${reward_added_price_formatted}"
        send_telegram_message(
            GlobalConstants.BOT_VOTEMARKET_API_KEY,
            GlobalConstants.VOTEMARKET_CHANNEL_ID,
            replace_double_quotes_with_single(msg),
        )


def get_all_platforms():
    ALL_PLATFORMS = [
        ContractRegistry.PENDLE_VOTEMARKET_V2,
        ContractRegistry.CURVE_VOTEMARKET_V2,
        ContractRegistry.BALANCER_VOTEMARKET_V2,
        ContractRegistry.FXN_VOTEMARKET_V2,
        ContractRegistry.YB_VOTEMARKET_V2,
    ]

    names = {}
    gauges_endpoint = {}

    for chain_id in ContractRegistry.BALANCER_VOTEMARKET_V2:
        contracts = ContractRegistry.BALANCER_VOTEMARKET_V2[chain_id]
        if contracts is not None:
            for contract in contracts:
                names[to_checksum_address(contract)] = "Balancer"
                gauges_endpoint[to_checksum_address(contract)] = "balancer"

    for chain_id in ContractRegistry.FXN_VOTEMARKET_V2:
        contracts = ContractRegistry.FXN_VOTEMARKET_V2[chain_id]
        if contracts is not None:
            for contract in contracts:
                names[to_checksum_address(contract)] = "F(x)"
                gauges_endpoint[to_checksum_address(contract)] = "fxn"

    for chain_id in ContractRegistry.CURVE_VOTEMARKET_V2:
        contracts = ContractRegistry.CURVE_VOTEMARKET_V2[chain_id]
        if contracts is not None:
            for contract in contracts:
                names[to_checksum_address(contract)] = "Curve"
                gauges_endpoint[to_checksum_address(contract)] = "curve"

    for chain_id in ContractRegistry.PENDLE_VOTEMARKET_V2:
        contracts = ContractRegistry.PENDLE_VOTEMARKET_V2[chain_id]
        if contracts is not None:
            for contract in contracts:
                names[to_checksum_address(contract)] = "Pendle"
                gauges_endpoint[to_checksum_address(contract)] = "pendle"

    for chain_id in ContractRegistry.YB_VOTEMARKET_V2:
        contracts = ContractRegistry.YB_VOTEMARKET_V2[chain_id]
        if contracts is not None:
            for contract in contracts:
                names[to_checksum_address(contract)] = "Yield Basis"
                gauges_endpoint[to_checksum_address(contract)] = "yb"

    return ALL_PLATFORMS, names, gauges_endpoint


async def job():
    checkpoints = {}

    # Last blocks fetch per chain
    ALL_PLATFORMS, names, gauges_endpoint = get_all_platforms()

    for platform in ALL_PLATFORMS:
        for chain_id in platform:
            if chain_id == 137:
                continue

            platform_addresses = platform[chain_id]
            if platform_addresses is None:
                continue

            if chain_id not in web3_service.w3:
                web3_service.add_chain(chain_id)

            try:
                chain_name = Common.chains_ids_to_name[chain_id]
            except:
                print(f"Chain id {chain_id} not available")
                continue

            web3 = web3_service.get_w3(chain_id)

            block_min, block_max = (
                get_block_range(WORKFLOW_NAME, chain_id, web3, checkpoints)
            )

            """
            # Setting block min to a specific timestamp (TEMP)
            if block_min == 0:
                block_min = get_closest_block_timestamp(chain_name, 1764082800)
            """

            print(f"Processing chain {chain_name} ({chain_id}) from block {block_min} to block {block_max}")


            if block_min == 0:
                continue

            for platform_address in platform_addresses:
                await get_campaigns_created(
                    web3,
                    chain_id,
                    platform_address,
                    block_min,
                    block_max,
                    names,
                    gauges_endpoint,
                )

                # Rate limit
                time.sleep(5)

                await get_campaigns_increased(
                    web3,
                    chain_id,
                    platform_address,
                    block_min,
                    block_max,
                    names,
                    gauges_endpoint,
                )

                # Rate limit
                time.sleep(5)


async def main():
    try:
        await job()
    except Exception as e:
        print(e)
        raise


if __name__ == "__main__":
    asyncio.run(main())

# export PYTHONPATH=script/
# source env/bin/activate
