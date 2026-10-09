"""Stake DAO vlCVX delegation activity bot (ENG-2101).

Watches the Convex Delegation contract — the on-chain registry that replaced
cvx.eth Snapshot delegation when Convex moved its voting platform on-chain —
and posts to the activity channel every time a user's delegation to Stake DAO
changes: a new delegation, a delegation forwarded to another delegate, or a
delegation removed.
"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.address import format_eth_address
from shared.bot_runner import get_block_range, run_bot, setup_bot
from shared.communication.telegram import send_telegram_message
from shared.constants import ContractRegistry, GlobalConstants
from shared.services.etherscan_service import get_logs_by_address_and_topics
from shared.services.price_service import get_single_token_price
from shared.services.web3_service import get_web3_service
from shared.strings import abbreviate_number
from shared.utils.globals import pad_address
from shared.utils.safe import format_user_info, is_safe_multisig
from web3 import Web3

setup_bot()

WORKFLOW_NAME = "vlcvx-delegation"
CHAIN_ID = 1
ETHERSCAN_URL = "https://etherscan.io"

DELEGATION_ADDRESS = Web3.to_checksum_address(
    ContractRegistry.CONVEX_GAUGE_DELEGATION[1]
)
# Creation block of the Delegation contract — lower bound of the history scan
# below. Its seedDelegates() migration emitted a DelegateSet per user, so the
# event log alone carries the full delegate history.
DELEGATION_DEPLOY_BLOCK = 25478184

# Only the on-chain delegate counts as Stake DAO: it is the address that
# actually votes on the Convex platforms. The legacy Snapshot delegate
# (ContractRegistry.DELEGATION) still receives the occasional setDelegate, but
# that weight never reaches the on-chain votes.
STAKE_DAO_DELEGATE = Web3.to_checksum_address(ContractRegistry.DELEGATION_ONCHAIN[1])

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# DelegateSet(address indexed user, address indexed delegate) — the contract's
# only user-facing event. setDelegate() emits it for a first delegation, for a
# change of delegate, and for a clear (delegate == 0x0).
TOPIC_DELEGATE_SET = (
    "0x6ee10e9ed4d6ce9742703a498707862f4b00f1396a87195eb93267b3d7983981"
)

# Entries Etherscan returns at most for one getLogs call
ETHERSCAN_LOG_PAGE_SIZE = 1000

NEW_DELEGATION = "new"
FORWARDED = "forwarded"
REMOVED = "removed"

KIND_HEADERS = {
    NEW_DELEGATION: ("🟢", "New vlCVX delegation to Stake DAO"),
    FORWARDED: ("🔁", "vlCVX delegation forwarded away from Stake DAO"),
    REMOVED: ("🔴", "vlCVX delegation removed from Stake DAO"),
}

def _hex_int(value: Any) -> int:
    """Etherscan returns hex strings, and "0x" for a zero log index."""
    if isinstance(value, int):
        return value
    digits = str(value)[2:] if str(value).startswith("0x") else str(value)
    return int(digits, 16) if digits else 0


def _topic_to_address(topic: str) -> str:
    return Web3.to_checksum_address("0x" + topic[-40:])


def _log_order(log: dict) -> Tuple[int, int]:
    return _hex_int(log["blockNumber"]), _hex_int(log.get("logIndex", 0))


def _classify_transition(previous: Optional[str], delegate: str) -> Optional[str]:
    """
    Kind of the delegation change relative to Stake DAO, or None when the
    change never involved us (e.g. a user moving between two other delegates).
    """
    was_ours = previous == STAKE_DAO_DELEGATE
    is_ours = delegate == STAKE_DAO_DELEGATE
    if is_ours and not was_ours:
        return NEW_DELEGATION
    if was_ours and not is_ours:
        return REMOVED if delegate == ZERO_ADDRESS else FORWARDED
    return None


def _stake_dao_transitions(
    logs: List[dict], previous_delegate: Callable[[str], Optional[str]]
) -> List[Tuple[dict, str, str, str]]:
    """
    Folds the window's DelegateSet logs into (log, user, kind, delegate) tuples,
    keeping only the changes that involve Stake DAO.

    Events are replayed in chain order and the per-user state is carried along,
    so a user who removes then re-delegates inside the same window produces both
    messages. `previous_delegate` is only asked for the state at the start of
    the window, once per user.
    """
    known: Dict[str, Optional[str]] = {}
    transitions = []

    for log in sorted(logs, key=_log_order):
        user = _topic_to_address(log["topics"][1])
        delegate = _topic_to_address(log["topics"][2])
        if user not in known:
            known[user] = previous_delegate(user)

        kind = _classify_transition(known[user], delegate)
        known[user] = delegate
        if kind is not None:
            transitions.append((log, user, kind, delegate))

    return transitions


def _fetch_delegate_set_logs(
    from_block: int, to_block: int, topics: Dict[str, str]
) -> List[dict]:
    """
    DelegateSet logs over a block range, split as needed.

    Etherscan caps a getLogs response at 1000 entries and
    get_logs_by_address_and_topics does not paginate, so a wide range comes
    back silently truncated — and a truncated history is worse than no
    history here: the fold would read a stale delegate and misclassify (or
    drop) the change. A full page is therefore assumed to be capped and its
    range re-read in halves.
    """
    logs = get_logs_by_address_and_topics(
        DELEGATION_ADDRESS, from_block, to_block, topics, CHAIN_ID
    )
    if len(logs) < ETHERSCAN_LOG_PAGE_SIZE:
        return logs
    if from_block >= to_block:
        # A single block over the cap cannot be split any further
        logging.warning(
            f"DelegateSet logs capped at {ETHERSCAN_LOG_PAGE_SIZE} on block {from_block}"
        )
        return logs

    middle = (from_block + to_block) // 2
    return _fetch_delegate_set_logs(from_block, middle, topics) + (
        _fetch_delegate_set_logs(middle + 1, to_block, topics)
    )


def _last_delegate_before(user: str, block: int) -> Optional[str]:
    """
    Delegate of `user` just before `block`, read from the contract's own event
    history rather than from delegateHistory().

    setDelegate() overwrites the tail record when the user changes delegate
    twice in the same epoch, so on-chain storage cannot always tell who the
    previous delegate was. The logs always can.
    """
    if block <= DELEGATION_DEPLOY_BLOCK:
        return None

    logs = _fetch_delegate_set_logs(
        DELEGATION_DEPLOY_BLOCK,
        block - 1,
        # Etherscan matches topics case-insensitively, so the checksummed
        # address pad_address returns filters just as well as a lowercase one
        {"0": TOPIC_DELEGATE_SET, "1": pad_address(user)},
    )
    if not logs:
        return None
    return _topic_to_address(max(logs, key=_log_order)["topics"][2])


def _format_amount(weight_wei: int, cvx_price: Optional[float]) -> str:
    """
    Weight the delegation carries. Empty when the user has nothing locked for
    that epoch: an expired position delegates no voting power, and
    "0.00 vlCVX" reads like a bug rather than as the fact it is.
    """
    if weight_wei <= 0:
        return ""
    amount = weight_wei / 10**18
    suffix = f" : {abbreviate_number(amount, True)} vlCVX"
    if cvx_price and cvx_price > 0:
        suffix += f" (~${abbreviate_number(amount * cvx_price, True)})"
    return suffix


def _format_total(total_wei: int) -> str:
    """Weight delegated to Stake DAO once this change applies."""
    if total_wei <= 0:
        return ""
    return (
        f"Total delegated to Stake DAO : "
        f"{abbreviate_number(total_wei / 10**18, True)} vlCVX"
    )


def _build_message(
    kind: str,
    tx_hash: str,
    amount_suffix: str,
    user_line: str,
    delegate_line: str = "",
    total_line: str = "",
) -> str:
    emoji, label = KIND_HEADERS[kind]
    message = (
        f"{emoji} <a href='{ETHERSCAN_URL}/tx/{tx_hash}'>{label}</a>"
        f"{amount_suffix}\n"
    )
    message += user_line
    message += delegate_line
    message += total_line
    return message


def _weight_epoch(vlcvx_contract, now: int) -> int:
    """
    vlCVX epoch the delegation changes of this run apply to.

    setDelegate() writes the weights from the NEXT epoch on, and vlCVX counts a
    fresh lock from the next epoch too: read at the current epoch, someone who
    locks and delegates in the same transaction shows up with 0 vlCVX. The
    epoch is capped to what vlCVX has checkpointed so far, since
    balanceAtEpochOf reverts past the end of its epoch array.
    """
    epoch_count = vlcvx_contract.functions.epochCount().call()
    current_epoch = vlcvx_contract.functions.findEpochId(now).call()
    return min(current_epoch + 1, epoch_count - 1)


def job():
    web3 = get_web3_service(CHAIN_ID)
    w3 = web3.get_w3(CHAIN_ID)
    from_block, current_block = get_block_range(WORKFLOW_NAME, CHAIN_ID, w3)

    if from_block == 0:
        return

    logs = _fetch_delegate_set_logs(
        from_block, current_block, {"0": TOPIC_DELEGATE_SET}
    )
    if not logs:
        return

    transitions = _stake_dao_transitions(
        logs, lambda user: _last_delegate_before(user, from_block)
    )
    if not transitions:
        return

    vlcvx_contract = web3.get_contract(
        Web3.to_checksum_address(ContractRegistry.CONVEX_CVX_LOCKER[1]), "vlCVX"
    )
    delegation_contract = web3.get_contract(DELEGATION_ADDRESS, "convex_delegation")
    cvx_price = get_single_token_price(CHAIN_ID, ContractRegistry.CVX[1])
    epoch = _weight_epoch(vlcvx_contract, int(time.time()))
    # Read once: every weight here is the live one, i.e. after ALL the changes
    # of this window. Reading it per message would repeat the same call and
    # still report that same total.
    total_line = _format_total(
        delegation_contract.functions.balanceAtEpochOf(
            epoch, STAKE_DAO_DELEGATE
        ).call()
    )

    for log, user, kind, delegate in transitions:
        tx_hash = log["transactionHash"]
        # The user's own vlCVX weight, not the Delegation contract's copy of
        # it: that copy is wiped when the user leaves, so a removal would
        # report the delegation it just lost as 0.
        weight = vlcvx_contract.functions.balanceAtEpochOf(epoch, user).call()
        executor = w3.eth.get_transaction(tx_hash)["from"]
        user_line = format_user_info(
            user, executor, is_safe_multisig(user, w3), ETHERSCAN_URL
        )

        delegate_line = ""
        if kind == FORWARDED:
            # ens_instance: format_eth_address otherwise builds its own ENS on
            # the default provider, which the bot doesn't configure — the
            # delegate would print as another hex string instead of votium.eth
            delegate_line = (
                f"New delegate: <a href='{ETHERSCAN_URL}/address/{delegate}'>"
                f"{format_eth_address(delegate, ens_instance=w3.ens)}</a>\n"
            )

        send_telegram_message(
            GlobalConstants.BOT_API_KEY,
            GlobalConstants.TELEGRAM_ACTIVITY_CHANNEL_ID,
            _build_message(
                kind,
                tx_hash,
                _format_amount(weight, cvx_price),
                user_line,
                delegate_line,
                total_line,
            ),
        )


def main():
    run_bot(job, WORKFLOW_NAME)


__name__ == "__main__" and main()

# export PYTHONPATH=script/
