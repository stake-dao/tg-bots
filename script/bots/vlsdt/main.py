from datetime import datetime

from shared.bot_runner import get_block_range, run_bot, setup_bot
from shared.communication.telegram import send_telegram_message
from shared.constants import ContractRegistry, GlobalConstants
from shared.services.etherscan_service import get_logs_by_address_and_topics
from shared.services.price_service import get_single_token_price
from shared.services.web3_service import get_web3_service
from shared.strings import abbreviate_number
from shared.utils.globals import decode_hex_data
from shared.utils.safe import format_user_info, is_safe_multisig
from web3 import Web3

setup_bot()

WORKFLOW_NAME = "vlsdt"

TOPIC_STAKED = (
    "0x5dac0c1b1112564a045ba943c9d50270893e8e826c49be8e7073adc713ab7bd7"
)
TOPIC_UNSTAKE_REQUESTED = (
    "0xcbb10a3603d92dc1f9db6996b88539fbd521bb4144891e34c75b05c341c18379"
)
TOPIC_UNSTAKE_CANCELLED = (
    "0xcf885437736ba39cdb528b80fcffb71f1ca33b9432dfcbf27e9da2cb2ddbe500"
)
TOPIC_WITHDRAWN = (
    "0xe5df19de43c8c04fd192bc68e484b2593570925fbb6ad8c07ccafbc2aa5c37a1"
)
TOPIC_WITHDRAWN_EARLY = (
    "0xace1ee10d4ede99bef651eea743ec50b71136eb8592a713d8b199ef599d6e9e3"
)
TOPIC_VESDT_MIGRATE = (
    "0xd616b8856fa5febbdb06f07dd8d624380d02864619f3b796002f43bc36a4d1bc"
)


def _topic_to_address(topic: str) -> str:
    return Web3.to_checksum_address("0x" + topic[-40:])


def _build_message(label: str, suffix: str, log: dict, owner: str, web3) -> str:
    tx_hash = log["transactionHash"]
    msg = f"<a href='https://etherscan.io/tx/{tx_hash}'>{label}</a>{suffix}\n"
    w3 = web3.get_w3(1)
    executor = w3.eth.get_transaction(tx_hash)["from"]
    is_safe = is_safe_multisig(owner, w3)
    msg += format_user_info(owner, executor, is_safe, "https://etherscan.io")
    return msg


def _format_usd(value_wei: int, sdt_price: float | None) -> str:
    if not sdt_price or sdt_price <= 0:
        return ""
    usd = (value_wei / 10**18) * sdt_price
    return f" (~${abbreviate_number(usd, True)})"


def _format_amount_suffix(value_wei: int, sdt_price: float | None = None) -> str:
    return (
        f" : {abbreviate_number(value_wei / 10**18, True)} SDT"
        f"{_format_usd(value_wei, sdt_price)}"
    )


def _handle_staked(
    log,
    web3,
    vlsdt_supply_provider,
    sdt_price=None,
    migration_info=None,
    prior_balance_provider=None,
) -> str:
    recipient = _topic_to_address(log["topics"][2])
    data = decode_hex_data(log["data"][2:], ["uint256"])
    amount = data[0]
    block_number = int(log["blockNumber"], 16)

    if migration_info is not None:
        migrate_sender, migrate_user = migration_info
        is_force = migrate_sender.lower() != migrate_user.lower()
        label = (
            "🔁 Force-migrate veSDT → vlSDT"
            if is_force
            else "🔁 veSDT → vlSDT migration"
        )
        msg = _build_message(
            label, _format_amount_suffix(amount, sdt_price), log, migrate_user, web3
        )
        if is_force:
            sender_short = f"{migrate_sender[:6]}…{migrate_sender[-4:]}"
            msg += (
                f"Triggered by : <a href='https://etherscan.io/address/{migrate_sender}'>"
                f"{sender_short}</a>\n"
            )
    else:
        prior_balance = (
            prior_balance_provider(recipient, block_number - 1)
            if prior_balance_provider is not None
            else 0
        )
        label = "Lock increased" if prior_balance > 0 else "New stake"
        msg = _build_message(
            label, _format_amount_suffix(amount, sdt_price), log, recipient, web3
        )

    total = vlsdt_supply_provider(block_number)
    if total > 0:
        total_line = f"Total vlSDT staked : {abbreviate_number(total / 10**18, True)}"
        total_line += _format_usd(total, sdt_price)
        msg += total_line
    return msg


def _handle_unstake_requested(log, web3, sdt_price=None) -> str:
    owner = _topic_to_address(log["topics"][2])
    data = decode_hex_data(log["data"][2:], ["uint256", "uint256"])
    amount, deadline = data
    deadline_str = datetime.fromtimestamp(deadline).strftime("%Y-%m-%d")

    msg = _build_message(
        "Unstake requested", _format_amount_suffix(amount, sdt_price), log, owner, web3
    )
    msg += f"Unlocks : {deadline_str}"
    return msg


def _handle_unstake_cancelled(log, web3, sdt_price=None) -> str:
    owner = _topic_to_address(log["topics"][2])
    data = decode_hex_data(log["data"][2:], ["uint256"])
    amount = data[0]
    return _build_message(
        "Unstake cancelled",
        _format_amount_suffix(amount, sdt_price),
        log,
        owner,
        web3,
    )


def _handle_withdrawn(log, web3, sdt_price=None) -> str:
    owner = _topic_to_address(log["topics"][2])
    data = decode_hex_data(log["data"][2:], ["uint256"])
    amount = data[0]
    return _build_message(
        "Withdrawn", _format_amount_suffix(amount, sdt_price), log, owner, web3
    )


def _handle_withdrawn_early(log, web3, sdt_price=None) -> str:
    owner = _topic_to_address(log["topics"][2])
    data = decode_hex_data(log["data"][2:], ["uint256", "uint256"])
    amount, penalty = data
    formatted_amount = abbreviate_number(amount / 10**18, True)
    formatted_penalty = abbreviate_number(penalty / 10**18, True)
    suffix = (
        f" : {formatted_amount} SDT{_format_usd(amount, sdt_price)}"
        f" (penalty {formatted_penalty} SDT{_format_usd(penalty, sdt_price)})"
    )
    return _build_message("⚠ Early exit", suffix, log, owner, web3)


HANDLERS = {
    TOPIC_STAKED: ("staked", _handle_staked),
    TOPIC_UNSTAKE_REQUESTED: ("unstake_requested", _handle_unstake_requested),
    TOPIC_UNSTAKE_CANCELLED: ("unstake_cancelled", _handle_unstake_cancelled),
    TOPIC_WITHDRAWN: ("withdrawn", _handle_withdrawn),
    TOPIC_WITHDRAWN_EARLY: ("withdrawn_early", _handle_withdrawn_early),
}


def job():
    web3 = get_web3_service(1)
    from_block, current_block = get_block_range(WORKFLOW_NAME, 1, web3.get_w3(1))

    if from_block == 0:
        return

    vlsdt_address = Web3.to_checksum_address(ContractRegistry.VLSDT[1])
    vlsdt_contract = web3.get_contract(vlsdt_address, "erc20")

    sdt_price = get_single_token_price(1, ContractRegistry.SDT[1])

    def supply_at(block_number: int) -> int:
        return vlsdt_contract.functions.totalSupply().call(
            block_identifier=block_number
        )

    def balance_at(account: str, block_number: int) -> int:
        return vlsdt_contract.functions.balanceOf(account).call(
            block_identifier=block_number
        )

    migration_logs = get_logs_by_address_and_topics(
        Web3.to_checksum_address(ContractRegistry.veSDT[1]),
        from_block,
        current_block,
        {"0": TOPIC_VESDT_MIGRATE},
    )
    migration_by_tx = {
        log["transactionHash"]: (
            _topic_to_address(log["topics"][1]),
            _topic_to_address(log["topics"][2]),
        )
        for log in migration_logs
    }

    for topic, (_name, handler) in HANDLERS.items():
        logs = get_logs_by_address_and_topics(
            vlsdt_address,
            from_block,
            current_block,
            {"0": topic},
        )

        for log in logs:
            if topic == TOPIC_STAKED:
                migration_info = migration_by_tx.get(log["transactionHash"])
                msg = handler(
                    log, web3, supply_at, sdt_price, migration_info, balance_at
                )
            else:
                msg = handler(log, web3, sdt_price)

            send_telegram_message(
                GlobalConstants.BOT_API_KEY,
                GlobalConstants.TELEGRAM_ACTIVITY_CHANNEL_ID,
                msg,
            )


def main():
    run_bot(job, WORKFLOW_NAME)


__name__ == "__main__" and main()
