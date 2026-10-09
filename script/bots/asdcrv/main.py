from shared.services.etherscan_service import get_logs_by_address_and_topics
from shared.services.web3_service import get_web3_service
from shared.address import format_eth_address
from shared.bot_runner import get_block_range, run_bot, setup_bot
from shared.communication.telegram import send_telegram_message
from shared.constants import ContractRegistry, GlobalConstants
from shared.utils.formatters import format_amount
from shared.utils.globals import decode_hex_data, replace_double_quotes_with_single
from shared.utils.safe import format_user_info, is_safe_multisig
from bots.utils.telegram_format import format_activity_header
from web3 import Web3

setup_bot()

WORKFLOW_NAME = "asdcrv"

TOPIC_BORROW = (
    "0xe1979fe4c35e0cef342fef5668e2c8e7a7e9f5d5d1ca8fee0ac6c427fa4153af"
)
TOPIC_REMOVE_COLLATERAL = (
    "0xe25410a4059619c9594dc6f022fe231b02aaea733f689e7ab0cd21b3d4d0eb54"
)
TOPIC_REPAY = (
    "0x77c6871227e5d2dec8dadd5354f78453203e22e669cd0ec4c19d9a8c5edb31d0"
)
TOPIC_LIQUIDATION = (
    "0x642dd4d37ddd32036b9797cec464c0045dd2118c549066ae6b0f88e32240c2d0"
)
LLAMALEND_MARKET_URL = (
    "https://lend.curve.fi/#/arbitrum/markets/one-way-market-13/create"
)

MSG_HEADER = format_activity_header("asdcrv", "Arbitrum")


def fetch_borrows(lastBlock, currentBlock, web3):
    logs = get_logs_by_address_and_topics(
        Web3.to_checksum_address(ContractRegistry.LLAMALEND_ASDCRV[42161]),
        lastBlock,
        currentBlock,
        {
            "0": TOPIC_BORROW,
        },
        42161,
    )

    borrows = []
    for log in logs:
        txHash = log["transactionHash"]
        user = "0x" + log["topics"][1][-40:]
        tx = web3.eth.get_transaction(txHash)
        executor = tx["from"]
        is_safe = is_safe_multisig(user, web3)

        data = log["data"][2:]
        res = decode_hex_data(data, ["uint256", "uint256"])

        collateral = res[0] / (10**18)
        loan = res[1] / (10**18)
        formatted_collateral = format_amount(collateral)
        formatted_loan = format_amount(loan)

        message = MSG_HEADER

        if loan == 0 and collateral > 0:
            message += (
                f"🚀 Added {formatted_collateral} asdcrv in collateral\n"
            )
        elif collateral == 0 and loan > 0:
            message += f"🚀 Borrowed {formatted_loan} crvUSD\n"
        elif collateral > 0 and loan > 0:
            message += f"🚀 Deposited {formatted_collateral} asdcrv / Received {formatted_loan} crvUSD\n"

        message += format_user_info(user, executor, is_safe, "https://arbiscan.io")
        message += f"Links : <a href='{LLAMALEND_MARKET_URL}'>Llamalend market</a> | <a href='https://arbiscan.io/tx/{txHash}'>etherscan.io</a>"

        borrows.append({"message": message, "blockNumber": log["blockNumber"]})
    return borrows


def fetch_remove_collaterals(lastBlock, currentBlock, web3):
    logs = get_logs_by_address_and_topics(
        Web3.to_checksum_address(ContractRegistry.LLAMALEND_ASDCRV[42161]),
        lastBlock,
        currentBlock,
        {
            "0": TOPIC_REMOVE_COLLATERAL,
        },
        42161,
    )

    remove_collaterals = []
    for log in logs:
        txHash = log["transactionHash"]
        user = "0x" + log["topics"][1][-40:]
        tx = web3.eth.get_transaction(txHash)
        executor = tx["from"]
        is_safe = is_safe_multisig(user, web3)

        data = log["data"][2:]
        res = decode_hex_data(data, ["uint256"])

        collateral = res[0] / (10**18)
        formatted_collateral = format_amount(collateral)

        message = MSG_HEADER
        message += f"🚀 Removed {formatted_collateral} asdcrv\n"
        message += format_user_info(user, executor, is_safe, "https://arbiscan.io")
        message += f"Links : <a href='{LLAMALEND_MARKET_URL}'>Llamalend market</a> | <a href='https://arbiscan.io/tx/{txHash}'>etherscan.io</a>"

        remove_collaterals.append(
            {"message": message, "blockNumber": log["blockNumber"]}
        )
    return remove_collaterals


def fetch_repays(lastBlock, currentBlock, web3):
    logs = get_logs_by_address_and_topics(
        Web3.to_checksum_address(ContractRegistry.LLAMALEND_ASDCRV[42161]),
        lastBlock,
        currentBlock,
        {
            "0": TOPIC_REPAY,
        },
        42161,
    )

    repays = []
    for log in logs:
        txHash = log["transactionHash"]
        user = "0x" + log["topics"][1][-40:]
        tx = web3.eth.get_transaction(txHash)
        executor = tx["from"]
        is_safe = is_safe_multisig(user, web3)

        data = log["data"][2:]
        res = decode_hex_data(data, ["uint256", "uint256"])

        # collateral_decrease = res[0] / (10**18)
        # formatted_collateral_decrease = "{:,.2f}".format(collateral_decrease)

        loan_decrease = res[1] / (10**18)
        formatted_loan_decrease = format_amount(loan_decrease)

        message = MSG_HEADER
        message += f"🚀 Repay {formatted_loan_decrease} crvUSD\n"
        message += format_user_info(user, executor, is_safe, "https://arbiscan.io")
        message += f"Links : <a href='{LLAMALEND_MARKET_URL}'>Llamalend market</a> | <a href='https://arbiscan.io/tx/{txHash}'>etherscan.io</a>"

        repays.append({"message": message, "blockNumber": log["blockNumber"]})
    return repays


def fetch_liquidations(lastBlock, currentBlock, web3):
    logs = get_logs_by_address_and_topics(
        Web3.to_checksum_address(ContractRegistry.LLAMALEND_ASDCRV[42161]),
        lastBlock,
        currentBlock,
        {
            "0": TOPIC_LIQUIDATION,
        },
        42161,
    )

    repays = []
    for log in logs:
        txHash = log["transactionHash"]
        liquidator = "0x" + log["topics"][0][-40:]
        user = "0x" + log["topics"][1][-40:]
        tx = web3.eth.get_transaction(txHash)
        executor = tx["from"]
        is_user_safe = is_safe_multisig(user, web3)
        is_liquidator_safe = is_safe_multisig(liquidator, web3)

        data = log["data"][2:]
        res = decode_hex_data(data, ["uint256", "uint256", "uint256"])

        collateral_received = res[0] / (10**18)
        stablecoin_received = res[1] / (10**18)
        debt = res[2] / (10**18)

        amount_sent_to_liquidate = debt - stablecoin_received

        formatted_collateral_received = format_amount(collateral_received)
        formatted_amount_sent_to_liquidate = format_amount(amount_sent_to_liquidate)

        # Format user line
        user_formatted = format_eth_address(user)
        if is_user_safe:
            user_part = f"🔒 Safe <a href='https://arbiscan.io/address/{user}'>{user_formatted}</a>"
        else:
            user_part = f"User <a href='https://arbiscan.io/address/{user}'>{user_formatted}</a>"

        # Format liquidator line
        liquidator_formatted = format_eth_address(liquidator)
        if is_liquidator_safe:
            executor_formatted = format_eth_address(executor) if executor else "Unknown"
            liquidator_line = f"🔒 Liquidator (Safe): <a href='https://arbiscan.io/address/{liquidator}'>{liquidator_formatted}</a> | Executor: <a href='https://arbiscan.io/address/{executor}'>{executor_formatted}</a>\n"
        else:
            liquidator_line = f"Liquidator: <a href='https://arbiscan.io/address/{liquidator}'>{liquidator_formatted}</a>\n"

        message = MSG_HEADER
        message += f"🚀 {user_part} hard-liquidated {formatted_collateral_received} asdcrv with {formatted_amount_sent_to_liquidate} crvUSD\n"
        message += liquidator_line
        message += f"Links : <a href='{LLAMALEND_MARKET_URL}'>Llamalend market</a> | <a href='https://arbiscan.io/tx/{txHash}'>etherscan.io</a>"

        repays.append({"message": message, "blockNumber": log["blockNumber"]})
    return repays


def job():
    web3 = get_web3_service(42161)
    from_block, current_block = get_block_range(WORKFLOW_NAME, 42161, web3.get_w3(42161))

    if from_block == 0:
        return

    w3 = web3.get_w3(42161)
    borrows = fetch_borrows(from_block, current_block, w3)
    remove_collateral = fetch_remove_collaterals(from_block, current_block, w3)
    repays = fetch_repays(from_block, current_block, w3)
    liquidations = fetch_liquidations(from_block, current_block, w3)

    allEvents = borrows + remove_collateral + repays + liquidations

    # Sort by block number
    allEvents.sort(key=lambda x: x["blockNumber"])

    for event in allEvents:
        send_telegram_message(
            GlobalConstants.BOT_API_KEY,
            GlobalConstants.TELEGRAM_ACTIVITY_CHANNEL_ID,
            replace_double_quotes_with_single(event["message"]),
            parse_mode="HTML",
        )

    from_block = current_block + 1


def main():
    run_bot(job, "asdcrv")


__name__ == "__main__" and main()

# export PYTHONPATH=script/
# source env/bin/activate
