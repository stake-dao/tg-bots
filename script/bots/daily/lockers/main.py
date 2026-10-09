import logging

import requests
from dotenv import load_dotenv
from shared.communication.telegram import send_telegram_message
from shared.constants import ZERO_ADDRESS, ContractRegistry, GlobalConstants
from shared.protocols.lockers import load_lockers
from shared.services.etherscan_service import get_logs_by_address_and_topics
from shared.services.price_service import get_single_token_price
from shared.strings import abbreviate_number
from shared.utils.globals import load_contract_w3, load_json
from web3 import Web3
from shared.services.web3_service import get_web3_service

load_dotenv()
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

LOCKERS_BLACKLISTED = ["angle", "fpis", "cake", "ynd"]
ACTIVE_LOCKERS = {"crv", "yb", "fxn", "fxs", "spectra"}
DEPRECATED_LOCKERS = {"bal", "pendle", "yfi", "zero", "mav", "bpt"}

# ABIs
erc20ABI = load_json("abi/erc20")
veABI = load_json("abi/veCrv")
veSpectra = load_json("abi/veSPECTRA")
veYB = load_json("abi/veYB")
veBoostV2ABI = load_json("abi/veBoostV2")
vePendleABI = load_json("abi/vePendle")
veYNDABI = load_json("abi/veynd")
vlCVXABI = load_json("abi/vlCVX")

SDT_LOCKERS = {
    "0x6cee94bfcd5a7defdbef337bf79fe31d0982cf2a": "cvgSDT",
}

# Snapshot delegation registry (Gnosis) — used by stakedao.eth space
SNAPSHOT_DELEGATION_REGISTRY = "0x469788fE6E9E9681C6ebF3bF78e7Fd26Fc015446"
SNAPSHOT_REGISTRY_DEPLOY_BLOCK = 11225329
TOPIC_SET_DELEGATE = (
    "0xa9a7fd460f56bddb880a465a9c3e9730389c70bc53108148f16d55a87a6c468e"
)
TOPIC_CLEAR_DELEGATE = (
    "0x9c4f00c4291262731946e308dc2979a56bd22cce8f95906b975065e96cd5a064"
)
# bytes32("stakedao.eth") right-padded with zeros
SPACE_ID_STAKEDAO = (
    "0x7374616b6564616f2e6574680000000000000000000000000000000000000000"
)
KNOWN_DELEGATEES = {
    "0x6cee94bfcd5a7defdbef337bf79fe31d0982cf2a": "cvgSDT",
}
TOP_DELEGATEES = 5

# YND constants
VE_YND_ADDRESS = "0xD666B56EE7786Cc918fDdcEade542d013de0E4F1"
SD_YND_NFT_ID = 223
YND_NFT_OWNER = "0x8396A782cc966661cd818a4DaC19C3E0aA893012"

# BAL veBoost v2 contract
VE_BOOST_V2_ADDRESS = "0x2cf8e145Bdfe7c52b49AD9bB3c294a31B2736c59"


def _get_web3_for_chain(chain_id):
    """
    Get a Web3 instance for a specific chain.

    Args:
        chain_id: The chain ID (1 for Ethereum, 8453 for Base, etc.)

    Returns:
        Web3: A Web3 instance connected to the chain's RPC
    """
    rpc_url = GlobalConstants.CHAIN_ID_TO_PUBLIC_RPC.get(chain_id)
    if not rpc_url:
        raise ValueError(f"No RPC URL configured for chain ID {chain_id}")

    web3_service = get_web3_service()
    web3_service.add_chain(chain_id)
    return web3_service.get_w3(chain_id)


def fetch_vlcvx_delegations(web3):
    """
    vlCVX standing with the Stake DAO on-chain delegate as of the current
    epoch, on the gauge-weight and DAO-proposal Delegation contracts — a
    holder can delegate to us on one and not the other.

    balanceOf and totalSupply both read findEpochId(block.timestamp), the
    epoch a Convex proposal stamps at creation; balanceAtEpochOf(epochCount()
    - 1) would instead read the epoch vlCVX pre-creates a week ahead. The
    three reads share one block, so they cannot straddle the Thursday
    rollover.

    Returns:
        tuple: (gauge_balance, dao_balance, vlcvx_supply) in raw wei units
    """
    delegate = Web3.to_checksum_address(ContractRegistry.DELEGATION_ONCHAIN[1])
    block = web3.eth.block_number

    def _delegated(address):
        contract = load_contract_w3(
            web3, Web3.to_checksum_address(address), "convex_delegation"
        )
        return contract.functions.balanceOf(delegate).call(
            block_identifier=block
        )

    gauge_balance = _delegated(ContractRegistry.CONVEX_GAUGE_DELEGATION[1])
    dao_balance = _delegated(ContractRegistry.CONVEX_DAO_DELEGATION[1])

    vlcvx_contract = web3.eth.contract(
        address=Web3.to_checksum_address(
            ContractRegistry.CONVEX_CVX_LOCKER[1]
        ),
        abi=vlCVXABI,
    )
    vlcvx_supply = vlcvx_contract.functions.totalSupply().call(
        block_identifier=block
    )

    logging.info(
        f"vlCVX delegated — gauge: {gauge_balance / 10**18:,.2f}, "
        f"dao: {dao_balance / 10**18:,.2f}, "
        f"supply: {vlcvx_supply / 10**18:,.2f}"
    )
    return gauge_balance, dao_balance, vlcvx_supply


def _format_vlcvx_delegation_line(label, balance, supply):
    amount = abbreviate_number(balance / 10**18)
    share = abbreviate_number(balance * 100 / supply, True)
    return f"vlCVX {label}: {amount} delegated ({share}%)"


def _send_vlcvx_delegations(web3):
    """Skipped on a read failure, not sent as a misleading 0 delegated."""
    try:
        gauge_balance, dao_balance, supply = fetch_vlcvx_delegations(web3)
    except Exception:
        logging.exception("Error fetching vlCVX delegations")
        return

    _send(
        "<u>Stake DAO Delegations :</u>\n\n"
        + _format_vlcvx_delegation_line("gauge", gauge_balance, supply)
        + "\n"
        + _format_vlcvx_delegation_line("DAO", dao_balance, supply)
    )


def computeLocker(
    lockerId, lockerChainId, veTokenAddress, lockerAddress, delegator
):


    web3_service = get_web3_service()
    web3_service.add_chain(lockerChainId)
    web3 = web3_service.get_w3(lockerChainId)

    veTokenAddress = Web3.to_checksum_address(veTokenAddress)
    veContract = web3.eth.contract(address=veTokenAddress, abi=veABI)

    if lockerId == "spectra":
        _veContract = web3.eth.contract(address=veTokenAddress, abi=veSpectra)
        lockerBalance = _veContract.functions.locked(
            ContractRegistry.SD_SPECTRA_NFT_ID
        ).call()[0]
    elif lockerId == "yb":
        _veContract = web3.eth.contract(address=veTokenAddress, abi=veYB)
        locked = _veContract.functions.locked(ContractRegistry.YB_LOCKER[1]).call()
        lockerBalance = locked[0]
    elif lockerId == "bal":
        # Use veBoost v2 contract for BAL
        veBoostV2Contract = web3.eth.contract(
            address=Web3.to_checksum_address(VE_BOOST_V2_ADDRESS), abi=veBoostV2ABI
        )
        lockerAddress = Web3.to_checksum_address(lockerAddress)
        totalBalance = veBoostV2Contract.functions.balanceOf(lockerAddress).call()
        delegatedBalance = veBoostV2Contract.functions.received_balance(lockerAddress).call()

        # Non-delegated balance = total - delegated
        nonDelegatedBalance = totalBalance - delegatedBalance

        # For BAL, we need to get totalSupply from the original veToken
        veTotalSupply = veContract.functions.totalSupply().call()

        # Return: non-delegated balance, total supply, delegated balance
        return nonDelegatedBalance, veTotalSupply, delegatedBalance
    else:
        lockerBalance = veContract.functions.balanceOf(
            Web3.to_checksum_address(lockerAddress)
        ).call()
    veTotalSupply = 0
    boost = 0

    if lockerId == "pendle":
        veContract = web3.eth.contract(address=veTokenAddress, abi=vePendleABI)
        veTotalSupply = veContract.functions.totalSupplyStored().call()
    elif lockerId == "yb":
        veContract = web3.eth.contract(address=veTokenAddress, abi=veYB)
        veTotalSupply = veContract.functions.totalVotes().call()
    else:
        veTotalSupply = veContract.functions.totalSupply().call()

    if len(delegator) > 0:
        boostDelegatorContract = web3.eth.contract(
            address=Web3.to_checksum_address(delegator), abi=erc20ABI
        )
        boost = boostDelegatorContract.functions.totalSupply().call()

    return lockerBalance, veTotalSupply, boost


def _format_locker_line(locker):
    lockerId = locker["id"]
    lockerChainId = locker["chainId"]
    veTokenAddress = locker["modules"]["veToken"]
    lockerAddress = locker["modules"]["locker"]
    delegator = ""
    if "veBoost" in locker and "delegator" in locker["veBoost"]:
        delegator = locker["veBoost"]["delegator"]

    balance, supply, boost = computeLocker(
        lockerId, lockerChainId, veTokenAddress, lockerAddress, delegator
    )
    totalLockerBalance = balance + boost
    totalSupply = supply

    if "extensions" in locker and "sideChains" in locker["extensions"]:
        for sideChain in locker["extensions"]["sideChains"]:
            sideBalance, sideSupply, _ = computeLocker(
                lockerId,
                sideChain["chainId"],
                sideChain["veToken"],
                sideChain["locker"],
                "",
            )
            totalLockerBalance += sideBalance
            totalSupply += sideSupply

    share = totalLockerBalance * 100 / totalSupply if totalSupply > 0 else 0
    shareFormatted = abbreviate_number(share, True)
    lockerBalanceFormatted = abbreviate_number(totalLockerBalance / 10**18)

    symbol = locker["token"]["symbol"]
    if lockerId == "bal":
        symbol = "BAL"

    line = (
        f"<a href='https://lockers.stakedao.org/lockers/{lockerId}'>"
        f"{lockerId.upper()} LL</a> : {lockerBalanceFormatted} ve{symbol} "
        f"({shareFormatted}%)"
    )
    if boost > 0:
        boost_formatted = abbreviate_number(boost / 10**18)
        line += f" (included {boost_formatted} ve{symbol} delegated)"
    return line


def _send(text):
    send_telegram_message(
        GlobalConstants.BOT_API_KEY,
        GlobalConstants.TELEGRAM_ACTIVITY_CHANNEL_ID,
        text,
    )


def _topic_to_addr(topic: str) -> str:
    return Web3.to_checksum_address("0x" + topic[-40:])


def fetch_stakedao_delegations(latest_block: int) -> dict:
    """
    Resolve current delegator → delegate map for the stakedao.eth space by
    replaying SetDelegate / ClearDelegate events from the Snapshot delegation
    registry.
    """
    set_logs = get_logs_by_address_and_topics(
        SNAPSHOT_DELEGATION_REGISTRY,
        SNAPSHOT_REGISTRY_DEPLOY_BLOCK,
        latest_block,
        {"0": TOPIC_SET_DELEGATE, "2": SPACE_ID_STAKEDAO},
    )
    clear_logs = get_logs_by_address_and_topics(
        SNAPSHOT_DELEGATION_REGISTRY,
        SNAPSHOT_REGISTRY_DEPLOY_BLOCK,
        latest_block,
        {"0": TOPIC_CLEAR_DELEGATE, "2": SPACE_ID_STAKEDAO},
    )

    events = []
    for log in set_logs:
        events.append((
            int(log["blockNumber"], 16),
            int(log["transactionIndex"], 16),
            int(log["logIndex"], 16),
            "set",
            _topic_to_addr(log["topics"][1]),
            _topic_to_addr(log["topics"][3]),
        ))
    for log in clear_logs:
        events.append((
            int(log["blockNumber"], 16),
            int(log["transactionIndex"], 16),
            int(log["logIndex"], 16),
            "clear",
            _topic_to_addr(log["topics"][1]),
            _topic_to_addr(log["topics"][3]),
        ))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    delegator_to_delegate: dict = {}
    for _blk, _txi, _lgi, kind, delegator, delegate in events:
        if kind == "set":
            delegator_to_delegate[delegator] = delegate
        else:
            if delegator_to_delegate.get(delegator) == delegate:
                del delegator_to_delegate[delegator]
    return delegator_to_delegate


def fetch_delegate_voting_power(addresses: list) -> dict:
    """
    Query Snapshot Score API for delegated vlSDT voting power per delegate
    on the stakedao.eth space. Returns {address_lower: vp_in_tokens}.
    """
    if not addresses:
        return {}
    payload = {
        "jsonrpc": "2.0",
        "method": "scores",
        "params": {
            "space": "stakedao.eth",
            "strategies": [
                {
                    "name": "erc20-balance-of-delegation",
                    "params": {
                        "symbol": "vlSDT",
                        "address": ContractRegistry.VLSDT[1],
                        "decimals": 18,
                    },
                    "network": "1",
                }
            ],
            "network": "1",
            "addresses": addresses,
            "snapshot": "latest",
        },
    }
    response = requests.post(
        "https://score.snapshot.org/", json=payload, timeout=60
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise Exception(f"Score API error: {data['error']}")
    scores = data.get("result", {}).get("scores", [{}])
    if not scores:
        return {}
    return {addr.lower(): vp for addr, vp in scores[0].items()}


def fetch_top_vlsdt_delegatees(web3, top_n: int = TOP_DELEGATEES) -> list:
    """
    Returns top N delegatees of stakedao.eth ranked by delegated vlSDT VP.
    Each entry: (address_checksum, vp_tokens).
    """
    try:
        latest_block = web3.eth.block_number
        delegator_to_delegate = fetch_stakedao_delegations(latest_block)
        unique_delegates = list({d for d in delegator_to_delegate.values()})
        if not unique_delegates:
            return []
        scores = fetch_delegate_voting_power(unique_delegates)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out = []
        for addr_lower, vp in ranked:
            if vp <= 0:
                continue
            out.append((Web3.to_checksum_address(addr_lower), vp))
            if len(out) >= top_n:
                break
        return out
    except Exception as e:
        logging.warning(f"Error fetching vlSDT delegatees: {e}")
        return []


def job():
    lockers = load_lockers()

    active_lines = []
    deprecated_lines = []
    for locker in lockers:
        lockerId = locker["id"]
        if lockerId in LOCKERS_BLACKLISTED:
            continue
        line = _format_locker_line(locker)
        if lockerId in ACTIVE_LOCKERS:
            active_lines.append(line)
        elif lockerId in DEPRECATED_LOCKERS:
            deprecated_lines.append(line)

    web3_service = get_web3_service()
    web3_service.add_chain(1)
    web3Mainnet = web3_service.get_w3(1)

    # YND locker (not in API, manual fetch — appended to active section)
    try:
        veYNDContract = web3Mainnet.eth.contract(
            address=Web3.to_checksum_address(VE_YND_ADDRESS), abi=veYNDABI
        )
        yndLockerBalance = veYNDContract.functions.locked(SD_YND_NFT_ID).call()[0]
        yndTotalSupply = veYNDContract.functions.totalLocked().call()
    except Exception as e:
        logging.warning(f"Error fetching YND data: {e}")
        yndLockerBalance = 0
        yndTotalSupply = 0

    if yndTotalSupply > 0:
        yndShare = yndLockerBalance * 100 / yndTotalSupply
        yndShareFormatted = abbreviate_number(yndShare, True)
        yndBalanceFormatted = abbreviate_number(yndLockerBalance / 10**18)
        active_lines.append(
            f"<a href='https://www.stakedao.org/lockers/ynd/prelaunch'>YND LL</a> : "
            f"{yndBalanceFormatted} veYND ({yndShareFormatted}%)"
        )

    # Msg 1: active LLs
    _send("<u>Stake DAO Liquid Lockers :</u>\n\n" + "\n".join(active_lines))

    # Msg 2: deprecated soon
    if deprecated_lines:
        _send("<u>Deprecated Soon :</u>\n\n" + "\n".join(deprecated_lines))

    # Msg 3: vlCVX delegations
    _send_vlcvx_delegations(web3Mainnet)

    # Msg 4: vlSDT delegations
    sdtContract = web3Mainnet.eth.contract(
        address=Web3.to_checksum_address(ContractRegistry.SDT[1]), abi=erc20ABI
    )
    vlSdtContract = web3Mainnet.eth.contract(
        address=Web3.to_checksum_address(ContractRegistry.VLSDT[1]), abi=erc20ABI
    )

    sdtTotalSupply = sdtContract.functions.totalSupply().call()
    sdt_in_vesdt = sdtContract.functions.balanceOf(
        Web3.to_checksum_address(ContractRegistry.veSDT[1])
    ).call()
    sdt_in_vlsdt = vlSdtContract.functions.totalSupply().call()

    sdt_price = get_single_token_price(1, ContractRegistry.SDT[1]) or 0

    def _usd_suffix(value_wei: int) -> str:
        if sdt_price <= 0:
            return ""
        usd = (value_wei / 10**18) * sdt_price
        return f" (~${abbreviate_number(usd, True)})"

    vlsdt_lines = []
    top_delegatees = fetch_top_vlsdt_delegatees(web3Mainnet)
    vlsdt_supply_tokens = sdt_in_vlsdt / 10**18 if sdt_in_vlsdt > 0 else 0

    if top_delegatees:
        vlsdt_lines.append("<b>Top Delegatees :</b>")
        for addr, vp in top_delegatees:
            label = KNOWN_DELEGATEES.get(addr.lower())
            if not label:
                short = f"{addr[:6]}…{addr[-4:]}"
                label = (
                    f"<a href='https://etherscan.io/address/{addr}'>{short}</a>"
                )
            share = vp * 100 / vlsdt_supply_tokens if vlsdt_supply_tokens > 0 else 0
            vlsdt_lines.append(
                f"{label} : {abbreviate_number(vp)} SDT "
                f"({abbreviate_number(share, True)}% of vlSDT)"
            )
        vlsdt_lines.append("")

    total_locked_sdt = sdt_in_vesdt + sdt_in_vlsdt
    migration_pct_formatted = (
        abbreviate_number(sdt_in_vlsdt * 100 / total_locked_sdt, True)
        if total_locked_sdt > 0
        else "0.00"
    )
    supply_share = (
        total_locked_sdt * 100 / sdtTotalSupply if sdtTotalSupply > 0 else 0
    )

    vlsdt_lines.append(
        f"vlSDT staked : {abbreviate_number(sdt_in_vlsdt / 10**18)} SDT"
        f"{_usd_suffix(sdt_in_vlsdt)}"
    )
    vlsdt_lines.append(
        f"veSDT remaining : {abbreviate_number(sdt_in_vesdt / 10**18)} SDT"
        f"{_usd_suffix(sdt_in_vesdt)}"
    )
    vlsdt_lines.append(f"Migration progress : {migration_pct_formatted}%")
    vlsdt_lines.append(
        f"Total locked : {abbreviate_number(total_locked_sdt / 10**18)} SDT"
        f"{_usd_suffix(total_locked_sdt)} "
        f"({abbreviate_number(supply_share, True)}% of supply)"
    )

    _send("<u>vlSDT Delegations :</u>\n\n" + "\n".join(vlsdt_lines))


def main():
    job()


__name__ == "__main__" and main()

# export PYTHONPATH=script/
# source env/bin/activate
