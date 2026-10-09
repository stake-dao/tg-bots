import logging

import requests
from shared.bot_runner import get_block_range
from dotenv import load_dotenv
from shared.address import format_eth_address
from shared.communication.telegram import send_telegram_message
from shared.utils.safe import format_user_info, is_safe_multisig
from shared.constants import (
    ZERO_ADDRESS,
    Bots,
    Common,
    ContractRegistry,
    GlobalConstants,
)
from shared.external.explorer import get_explorer_link
from shared.protocols.lockers import load_lockers
from shared.strings import abbreviate_number
from shared.utils.globals import load_json
from web3 import Web3
from shared.services.web3_service import get_web3_service

load_dotenv()
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

# Cowswap GPv2 Settlement contract
COWSWAP_SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
# Trade event topic: keccak256("Trade(address,address,address,uint256,uint256,uint256,bytes)")
COWSWAP_TRADE_TOPIC = "0xa07a543ab8a018198e99ca0184c93fe9050a79400a0a723441f84de1d972cc17"

# Github config
WORKFLOW_NAME = "curve-pools"

# ABIs
erc20ABI = load_json("abi/erc20")
curveStableSwapNGABI = load_json("abi/curveStableSwapNG")
curveStableSwapABI = load_json("abi/curveStableSwap")
pancakeV3PoolABI = load_json("abi/pancakeV3Pool")
balancerVaultABI = load_json("abi/balancerVault")
depositorABI = load_json("abi/depositor")
vePendleABI = load_json("abi/vePendle")
cakeDepositorABI = load_json("abi/cakeDepositor")
veCakeABI = load_json("abi/veCake")
veABI = load_json("abi/veCrv")
veSpectra = load_json("abi/veSPECTRA")
lockerABI = load_json("abi/locker")
crvLockerABI = load_json("abi/crvLocker")
stableCakePoolABI = load_json("abi/stableCakePool")

POOLS_PER_CHAIN = {
    252: [
        ContractRegistry.sdFXS_CURVE_POOL[252],  # sdFXS
    ],
    1: [
        ContractRegistry.sdCRV_POOL[1],  # new sdCRV
        ContractRegistry.sdPENDLE_POOL[1],  # sdPendle
        ContractRegistry.sdYFI_POOL[1],  # sdYFI
        "0x6788f608CfE5CfCD02e6152eC79505341E0774be",  # sdAPW
        ContractRegistry.sdFXN_POOL[1],  # sdFXN
        ContractRegistry.sdFXS_POOL[1],  # sdFXS
        "0x48fF31bBbD8Ab553Ebe7cBD84e1eA3dBa8f54957",  # sdAngle
        "0x9d259cA698746586107C234e9E9461d385ca1041",  # sdBPT
        "0x06c21B5d004604250a7f9639c4A3C28e73742261",  # sdFPIS
        ContractRegistry.sdBAL_POOL[1],  # sdBAL
        "0xf7b55c3732ad8b2c2da7c24f30a69f55c54fb717",  # old sdCRV
        "0x98b540fa89690969D111D045afCa575C91519B1A",  # sdYB/YB
    ],
    56: [
        ContractRegistry.sdCAKE_CAKE_V3[56],  # V3
        ContractRegistry.sdCAKE_CAKE_STABLE[56],  # sdCAKE stable
    ],
    8453: [
        ContractRegistry.SD_SPECTRA_POOL[8453],  # sdSpectra
    ],
}

BLOCKCHAIN_IDS = {
    252: "fraxtal",
    1: "ethereum",
    56: "bsc",
    324: "zkera",
    8453: "base",
    42161: "arbitrum",
    10: "op",
}

EVENT_TYPES = {
    "TOKEN_EXCHANGE": 0,
    "ADD_LIQUIDITY": 1,
    "REMOVE_LIQUIDITY": 2,
    "REMOVE_LIQUIDITY_ONE_COIN": 3,
    "SWAP_V3": 4,
    "DEPOSITED": 5,
    "SWAP_STABLE": 6,
    "ADD_LIQUIDITY_STABLE": 7,
    "DEPOSITED_V2": 8,
}


def send_telegram_message_curve(message, chainId=1, parse_mode="html"):
    """
    # DEBUG MODE: Commenting out telegram sends to test locally
    print("[DEBUG] Telegram message would be sent:")
    print("=" * 80)
    print(message)
    print("=" * 80)
    """
    send_telegram_message(
         GlobalConstants.BOT_API_KEY,
         GlobalConstants.TELEGRAM_ACTIVITY_CHANNEL_ID,
         message,
         parse_mode,
     )
    if chainId == 56:
         send_telegram_message(
             GlobalConstants.BOT_API_KEY,
             GlobalConstants.TELEGRAM_PUBLIC_CHINOIS_CHANNEL_ID,
             message,
             parse_mode,
         )


def getPool(poolAddress, allPools, blockchainId):
    for p in allPools:
        if (
            p["address"].lower() == poolAddress.lower()
            and p["blockchainId"] == blockchainId
        ):
            return p

    return None


def getPoolName(pool):
    prefix = ""
    suffix = ""

    if (
        "address" in pool
        and pool["address"].lower() == ContractRegistry.sdCRV_POOL[1].lower()
    ):
        prefix = "new "

    if "blockchainId" in pool and pool["blockchainId"] == "fraxtal":
        suffix = " Fraxtal"

    if "blockchainId" in pool and pool["blockchainId"] == "base":
        suffix = " Base"

    poolName = pool["coins"][len(pool["coins"]) - 1]["symbol"]

    return prefix + poolName + suffix


def getMsgUser(w3, event, explorerLink):
    """
    Generate a formatted message with user information and Safe detection.

    Args:
        w3: Web3 instance
        event: Transaction event containing transactionHash
        explorerLink: Base URL for the blockchain explorer

    Returns:
        str: Formatted HTML string with user address/label and explorer link
    """
    # Get transaction details
    try:
        tx = w3.eth.get_transaction(event["transactionHash"])
        executor = tx["from"]
        owner = executor  # Default: owner is executor

        # Check if transaction is from Cowswap settlement contract
        # If so, extract the real user from the Trade event
        if executor.lower() == COWSWAP_SETTLEMENT.lower():
            try:
                receipt = w3.eth.get_transaction_receipt(event["transactionHash"])
                for log in receipt["logs"]:
                    # Look for Trade event from Cowswap settlement
                    if (
                        log["address"].lower() == COWSWAP_SETTLEMENT.lower()
                        and len(log["topics"]) > 1
                        and log["topics"][0].hex().lower() == COWSWAP_TRADE_TOPIC.lower()
                    ):
                        # The owner (real user) is the first indexed parameter (topics[1])
                        owner_address = "0x" + log["topics"][1].hex()[-40:]
                        owner = Web3.to_checksum_address(owner_address)
                        break
            except Exception as e:
                print(f"[ERROR getMsgUser] Error parsing Cowswap Trade event: {e}")

        # Check if transaction calls harvestConcentratorCompounder function
        # Method ID: 0x04117561 for harvestConcentratorCompounder(address,uint256)
        input_data = tx.get("input", b"")

        # Convert to hex string properly (handle HexBytes, bytes, and str)
        if hasattr(input_data, 'hex'):
            # HexBytes or similar - .hex() returns string WITH 0x prefix
            hex_str = input_data.hex()
            input_hex = hex_str if hex_str.startswith("0x") else "0x" + hex_str
        elif isinstance(input_data, bytes):
            # Pure bytes - .hex() returns string WITHOUT 0x prefix
            input_hex = "0x" + input_data.hex()
        elif isinstance(input_data, str):
            # Already a string
            input_hex = input_data if input_data.startswith("0x") else "0x" + input_data
        else:
            input_hex = str(input_data)
        if input_hex.startswith("0x04117561"):
            # Display as Concentrator Harvest bot action
            return f"🚜 Concentrator Harvest\n"

        # Apply Safe detection
        is_safe = is_safe_multisig(owner, w3)
        return format_user_info(owner, executor, is_safe, explorerLink)
    except Exception as e:
        # Fallback if transaction fetch fails
        print(f"[ERROR getMsgUser] Error fetching transaction: {e}")
        return ""


def notSendSwapNotif(sell, poolAddress):
    if sell < 25000 and poolAddress.lower() == ContractRegistry.SD_SPECTRA_POOL[8453].lower():
        return True
    if sell < 1000 and (
        poolAddress.lower() == ContractRegistry.sdCAKE_CAKE_V3[56].lower()
        or poolAddress.lower() == ContractRegistry.sdCAKE_CAKE_STABLE[56].lower()
    ):
        return True
    if sell < 250 and poolAddress.lower() == ContractRegistry.sdFXS_CURVE_POOL[252].lower():
        return True
    return False


def notSendMintNotif(sell, depositor):
    if sell < 1000 and (depositor.lower() == ContractRegistry.CAKE_DEPOSITOR[56].lower()):
        return True
    return False


def manageSwapStable(w3, poolAddress, event, chainId):
    try:
        blockNumber = event["blockNumber"]

        args = event["args"]
        tokens_sold_amount = args["tokens_sold"]
        sold_id = args["sold_id"]
        tokens_bought_amount = args["tokens_bought"]
        bought_id = args["bought_id"]

        poolAddressChecksum = Web3.to_checksum_address(poolAddress)
        poolContract = w3.eth.contract(
            address=poolAddressChecksum, abi=stableCakePoolABI
        )
        tokenSoldAddress = poolContract.functions.coins(sold_id).call()

        tokenSoldContract = w3.eth.contract(
            address=Web3.to_checksum_address(tokenSoldAddress), abi=erc20ABI
        )
        tokenSoldDecimals = tokenSoldContract.functions.decimals().call()
        sell = tokens_sold_amount / 10**tokenSoldDecimals

        # Min sell 400
        if notSendSwapNotif(sell, poolAddress):
            return

        tokenBoughtAddress = poolContract.functions.coins(bought_id).call()
        tokenBoughtContract = w3.eth.contract(
            address=Web3.to_checksum_address(tokenBoughtAddress), abi=erc20ABI
        )

        tokenSoldBalancePool = tokenSoldContract.functions.balanceOf(
            poolAddressChecksum
        ).call(block_identifier=blockNumber)
        tokenBoughtBalancePool = tokenBoughtContract.functions.balanceOf(
            poolAddressChecksum
        ).call(block_identifier=blockNumber)
        totalBalance = tokenSoldBalancePool + tokenBoughtBalancePool

        tokenSoldPercentage = tokenSoldBalancePool * 100 / totalBalance
        tokenBoughtPercentage = tokenBoughtBalancePool * 100 / totalBalance

        tokenSoldSymbol = tokenSoldContract.functions.symbol().call()
        tokenBoughtSymbol = tokenBoughtContract.functions.symbol().call()

        tokenBoughtDecimals = tokenBoughtContract.functions.decimals().call()
        bought = tokens_bought_amount / 10**tokenBoughtDecimals

        formatted_tokens_sold = abbreviate_number(sell)
        formatted_tokens_bought = abbreviate_number(bought)

        formatted_tokenSoldPercentage = "{:,.2f}".format(tokenSoldPercentage)
        formatted_tokenBoughtPercentage = "{:,.2f}".format(
            tokenBoughtPercentage
        )

        formatted_tokenSoldBalance = abbreviate_number(
            tokenSoldBalancePool / 10**tokenSoldDecimals
        )
        formatted_tokenBoughtBalance = abbreviate_number(
            tokenBoughtBalancePool / 10**tokenBoughtDecimals
        )

        txHash = event["transactionHash"].hex()
        explorerLink = get_explorer_link(chainId)

        suffix = " "
        if poolAddress.lower() == ContractRegistry.sdCAKE_CAKE_V3[56]:
            suffix = " V3 "

        # Determine swap direction: Buy (🟩) or Sell (🟥)
        # Buy: TKN -> sdTKN (buying sdToken)
        # Sell: sdTKN -> TKN (selling sdToken)
        swap_indicator = ""
        if tokenBoughtSymbol.lower().startswith("sd"):
            swap_indicator = "🟩 "  # Buying sdToken
        elif tokenSoldSymbol.lower().startswith("sd"):
            swap_indicator = "🟥 "  # Selling sdToken

        msg = f"{swap_indicator}<a href='{explorerLink}/tx/{txHash}'>Exchange</a> on sdCAKE{suffix}pool\n"
        msg += f"{formatted_tokens_sold} {tokenSoldSymbol} for {formatted_tokens_bought} {tokenBoughtSymbol}\n\n"

        msg += f"Pool balance:\n"
        msg += f"{formatted_tokenSoldBalance} {tokenSoldSymbol} {formatted_tokenSoldPercentage}%\n"
        msg += f"{formatted_tokenBoughtBalance} {tokenBoughtSymbol} {formatted_tokenBoughtPercentage}%"

        send_telegram_message_curve(msg, chainId)

    except Exception as e:
        print(e)


def manageAddLiquidityStable(w3, poolAddress, event, chainId):
    try:
        blockNumber = event["blockNumber"]
        args = event["args"]
        token_amounts = args["token_amounts"]
        txHash = event["transactionHash"].hex()
        explorerLink = get_explorer_link(chainId)

        msg = f"<a href='{explorerLink}/tx/{txHash}'>Add</a> liquidity on sdCAKE pool\n\n"
        msg += getMsgUser(w3, event, explorerLink)
        msg += f"Deposited "

        poolAddressChecksum = Web3.to_checksum_address(poolAddress)
        poolContract = w3.eth.contract(
            address=poolAddressChecksum, abi=stableCakePoolABI
        )

        balancesData = []
        nCoins = poolContract.functions.N_COINS().call()
        for i in range(nCoins):
            token = poolContract.functions.coins(i).call()

            tokenContract = w3.eth.contract(address=token, abi=erc20ABI)
            tokenSymbol = tokenContract.functions.symbol().call()
            tokenDecimals = tokenContract.functions.decimals().call()

            token_amount = token_amounts[i]
            coinDeposited = token_amount / (10**tokenDecimals)
            if notSendSwapNotif(coinDeposited, poolAddress):
                return

            formatted_coinDeposited = abbreviate_number(coinDeposited)
            msg += f"{formatted_coinDeposited} {tokenSymbol}"

            if i < nCoins - 1:
                msg += " / "

            # Balance
            coinBalanceBN = poolContract.functions.balances(i).call(
                block_identifier=blockNumber
            )
            coinBalance = coinBalanceBN / (10**tokenDecimals)
            balancesData.append([coinBalance, tokenSymbol])

        msg += "\n\n"

        # Balances
        sumBalances = 0
        for i in range(len(balancesData)):
            sumBalances += balancesData[i][0]

        for i in range(len(balancesData)):
            coinBalance = balancesData[i][0]
            tokenSymbol = balancesData[i][1]
            percentage = coinBalance * 100 / sumBalances

            formatted_coinBalance = abbreviate_number(coinBalance)
            formatted_percentage = "{:,.2f}".format(percentage)
            msg += f"{formatted_coinBalance} {tokenSymbol} {formatted_percentage}%\n"

        send_telegram_message_curve(msg, chainId)

    except Exception as e:
        print(e)


def manageSwapV3(w3, poolAddress, event, chainId):
    try:
        blockNumber = event["blockNumber"]

        poolAddressChecksum = Web3.to_checksum_address(poolAddress)
        poolContract = w3.eth.contract(
            address=poolAddressChecksum, abi=pancakeV3PoolABI
        )
        token0Address = poolContract.functions.token0().call()
        token1Address = poolContract.functions.token1().call()

        token0Contract = w3.eth.contract(
            address=Web3.to_checksum_address(token0Address), abi=erc20ABI
        )
        token1Contract = w3.eth.contract(
            address=Web3.to_checksum_address(token1Address), abi=erc20ABI
        )

        token0Symbol = token0Contract.functions.symbol().call()
        token0Decimals = token0Contract.functions.decimals().call()
        token1Symbol = token1Contract.functions.symbol().call()
        token1Decimals = token1Contract.functions.decimals().call()

        token0BalancePool = token0Contract.functions.balanceOf(
            poolAddressChecksum
        ).call(block_identifier=blockNumber)
        token1BalancePool = token1Contract.functions.balanceOf(
            poolAddressChecksum
        ).call(block_identifier=blockNumber)
        totalBalance = token0BalancePool + token1BalancePool

        token0Percentage = token0BalancePool * 100 / totalBalance
        token1Percentage = token1BalancePool * 100 / totalBalance

        args = event["args"]
        amount0 = args["amount0"]
        amount1 = args["amount1"]

        sell = 0
        bought = 0
        soldTokenSymbol = ""
        boughtTokenSymbol = ""

        if amount0 < 0:
            sell = amount1 / 10**token1Decimals
            bought = amount0 / 10**token0Decimals
            soldTokenSymbol = token1Symbol
            boughtTokenSymbol = token0Symbol
        else:
            bought = amount1 / 10**token1Decimals
            sell = amount0 / 10**token0Decimals
            boughtTokenSymbol = token1Symbol
            soldTokenSymbol = token0Symbol

        if notSendSwapNotif(sell, poolAddress):
            return

        # Get abs because we have negative value in event
        sell = abs(sell)
        bought = abs(bought)

        formatted_tokens_sold = abbreviate_number(sell)
        formatted_tokens_bought = abbreviate_number(bought)

        formatted_token0Percentage = "{:,.2f}".format(token0Percentage)
        formatted_token1Percentage = "{:,.2f}".format(token1Percentage)

        formatted_token0Balance = abbreviate_number(
            token0BalancePool / 10**token0Decimals
        )
        formatted_token1Balance = abbreviate_number(
            token1BalancePool / 10**token1Decimals
        )

        txHash = event["transactionHash"].hex()
        explorerLink = get_explorer_link(chainId)

        suffix = " "
        if poolAddress.lower() == ContractRegistry.sdCAKE_CAKE_V3[56]:
            suffix = " V3 "

        # Determine swap direction: Buy (🟩) or Sell (🟥)
        # Buy: TKN -> sdTKN (buying sdToken)
        # Sell: sdTKN -> TKN (selling sdToken)
        swap_indicator = ""
        if boughtTokenSymbol.lower().startswith("sd"):
            swap_indicator = "🟩 "  # Buying sdToken
        elif soldTokenSymbol.lower().startswith("sd"):
            swap_indicator = "🟥 "  # Selling sdToken

        msg = f"{swap_indicator}<a href='{explorerLink}/tx/{txHash}'>Exchange</a> on sdCAKE{suffix}pool\n"
        msg += getMsgUser(w3, event, explorerLink)
        msg += f"{formatted_tokens_sold} {soldTokenSymbol} for {formatted_tokens_bought} {boughtTokenSymbol}\n\n"

        msg += f"Pool balance:\n"
        msg += f"{formatted_token0Balance} {token0Symbol} {formatted_token0Percentage}%\n"
        msg += f"{formatted_token1Balance} {token1Symbol} {formatted_token1Percentage}%"

        send_telegram_message_curve(msg, chainId)

    except Exception as e:
        print(e)


def manageTokenEchange(
    w3, poolAddress, event, allPools, blockchainId, chainId
):
    try:
        pool = getPool(poolAddress, allPools, blockchainId)
        if pool == None:
            return

        blockNumber = event["blockNumber"]

        args = event["args"]
        sold_id = args["sold_id"]
        tokens_sold = args["tokens_sold"]
        bought_id = args["bought_id"]
        tokens_bought = args["tokens_bought"]

        symbolSold = pool["coins"][sold_id]["symbol"]
        symbolBought = pool["coins"][bought_id]["symbol"]

        decimalsSold = int(pool["coins"][sold_id]["decimals"])
        decimalsBought = int(pool["coins"][bought_id]["decimals"])

        # Min sell for sdfxs on fraxtal
        sell = tokens_bought / 10**decimalsBought
        if notSendSwapNotif(sell, poolAddress):
            return

        poolName = getPoolName(pool)
        txHash = event["transactionHash"].hex()
        explorerLink = get_explorer_link(chainId)

        # Determine swap direction: Buy (🟩) or Sell (🟥)
        # Buy: TKN -> sdTKN (buying sdToken)
        # Sell: sdTKN -> TKN (selling sdToken)
        swap_indicator = ""
        if symbolBought.lower().startswith("sd"):
            swap_indicator = "🟩 "  # Buying sdToken
        elif symbolSold.lower().startswith("sd"):
            swap_indicator = "🟥 "  # Selling sdToken

        msg = f"{swap_indicator}<a href='{explorerLink}/tx/{txHash}'>Exchange</a> on {poolName} pool\n"
        msg += getMsgUser(w3, event, explorerLink)

        formatted_tokens_sold = abbreviate_number(
            tokens_sold / 10**decimalsSold
        )
        formatted_tokens_bought = abbreviate_number(sell)
        msg += f"{formatted_tokens_sold} {symbolSold} for {formatted_tokens_bought} {symbolBought}\n\n"

        msg += getPoolBalancesMsg(w3, poolAddress, pool, blockNumber)

        send_telegram_message_curve(msg, chainId)

    except Exception as e:
        print(e)


def manageAddLiquidity(
    w3, poolAddress, event, allPools, blockchainId, chainId
):
    manageLiquidity(
        w3, poolAddress, event, allPools, blockchainId, False, chainId
    )


def manageRemoveLiquidity(
    w3, poolAddress, event, allPools, blockchainId, chainId
):
    manageLiquidity(
        w3, poolAddress, event, allPools, blockchainId, True, chainId
    )


def manageLiquidity(
    w3, poolAddress, event, allPools, blockchainId, isRemove, chainId
):
    try:
        pool = getPool(poolAddress, allPools, blockchainId)
        if pool == None:
            return

        blockNumber = event["blockNumber"]

        args = event["args"]
        token_amounts = args["token_amounts"]

        label = "Add"
        action = "Deposited"
        if isRemove:
            label = "Remove"
            action = "Removed"

        poolName = getPoolName(pool)
        txHash = event["transactionHash"].hex()
        explorerLink = get_explorer_link(chainId)

        msg = f"<a href='{explorerLink}/tx/{txHash}'>{label}</a> liquidity on {poolName} pool\n\n"
        msg += getMsgUser(w3, event, explorerLink)
        msg += f"{action} "

        length = len(pool["coins"])
        for i in range(length):
            coin = pool["coins"][i]
            coinSymbol = coin["symbol"]
            token_amount = token_amounts[i]

            coinDeposited = token_amount / 10 ** int(coin["decimals"])

            # Min sell for sdfxs on fraxtal
            if notSendSwapNotif(coinDeposited, poolAddress):
                return

            formatted_coinDeposited = abbreviate_number(coinDeposited)
            msg += f"{formatted_coinDeposited} {coinSymbol}"

            if i < length - 1:
                msg += " / "

        msg += "\n\n"
        msg += getPoolBalancesMsg(w3, poolAddress, pool, blockNumber)

        send_telegram_message_curve(msg, chainId)

    except Exception as e:
        print(e)


def manageRemoveLiquidityOneCoin(
    w3, poolAddress, event, allPools, blockchainId, chainId
):
    try:
        pool = getPool(poolAddress, allPools, blockchainId)
        if pool == None:
            return

        blockNumber = event["blockNumber"]

        args = event["args"]

        decimals = 18
        coinSymbol = ""
        if "token_id" in args:
            # It's a curveStableSwap-NG
            # Query the pool contract directly to avoid API coin ordering mismatch
            poolContract = w3.eth.contract(
                address=Web3.to_checksum_address(poolAddress),
                abi=curveStableSwapNGABI,
            )
            coinAddress = poolContract.functions.coins(args["token_id"]).call()
            for coin in pool["coins"]:
                if coin["address"].lower() == coinAddress.lower():
                    decimals = coin["decimals"]
                    coinSymbol = coin["symbol"]
                    break
        else:
            # It's a curveStableSwap
            for coin in pool["coins"]:
                coinContract = w3.eth.contract(
                    address=Web3.to_checksum_address(coin["address"]),
                    abi=erc20ABI,
                )
                coinBalanceBeforeWithdraw = coinContract.functions.balanceOf(
                    Web3.to_checksum_address(poolAddress)
                ).call(block_identifier=blockNumber - 1)
                coinBalanceAfterWithdraw = coinContract.functions.balanceOf(
                    Web3.to_checksum_address(poolAddress)
                ).call(block_identifier=blockNumber)
                if coinBalanceAfterWithdraw < coinBalanceBeforeWithdraw:
                    decimals = coin["decimals"]
                    coinSymbol = coin["symbol"]
                    break

        coin_amount_received = args["coin_amount"] / 10 ** int(decimals)
        formatted_coin_amount_received = abbreviate_number(
            coin_amount_received
        )

        if notSendSwapNotif(coin_amount_received, poolAddress):
            return

        poolName = getPoolName(pool)
        txHash = event["transactionHash"].hex()
        explorerLink = get_explorer_link(chainId)

        msg = f"<a href='{explorerLink}/tx/{txHash}'>Remove</a> liquidity on {poolName} pool\n"
        msg += getMsgUser(w3, event, explorerLink)
        msg += f"Removed {formatted_coin_amount_received} {coinSymbol}\n\n"
        msg += getPoolBalancesMsg(w3, poolAddress, pool, blockNumber)

        send_telegram_message_curve(msg, chainId)

    except Exception as e:
        print(e)


def manageDeposit(w3, event, chainId, lockersApi):
    try:
        blockNumber = event["blockNumber"]

        args = event["args"]
        amount = args["amount"]
        depositor = event["address"]

        # Fetch depositor data
        lockerAddress = None
        veTokenAddress = None
        tokenSymbol = ""
        tokenDecimals = 0
        for locker in lockersApi:
            if ("modules" in locker) == False:
                continue

            modules = locker["modules"]
            token = locker["token"]
            if modules["depositor"].lower() == depositor.lower():
                lockerAddress = Web3.to_checksum_address(modules["locker"])
                veTokenAddress = Web3.to_checksum_address(modules["veToken"])
                tokenSymbol = token["symbol"]
                tokenDecimals = token["decimals"]
                break

        if tokenSymbol.lower() == "B-80BAL-20WETH".lower():
            tokenSymbol = "BAL"

        # Fix token symbol for Fraxtal sdFXS (API returns WFRAX instead of FXS)
        if chainId == 252 and tokenSymbol.lower() == "wfrax":
            tokenSymbol = "FXS"

        # Fetch supply locked
        supplyLocked = 0
        if chainId == 56:
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress), abi=veCakeABI
            )
            totalSupply = veContract.functions.locks(
                Web3.to_checksum_address(lockerAddress)
            ).call(block_identifier=blockNumber)
            supplyLocked = totalSupply[0] / 10**tokenDecimals
        elif tokenSymbol.lower() == "mav":
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress), abi=erc20ABI
            )
            totalSupply = veContract.functions.balanceOf(
                Web3.to_checksum_address(lockerAddress)
            ).call(block_identifier=blockNumber)
            supplyLocked = totalSupply / 10**tokenDecimals
        elif tokenSymbol.lower() == "pendle":
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress),
                abi=vePendleABI,
            )
            positionData = veContract.functions.positionData(
                Web3.to_checksum_address(lockerAddress)
            ).call(block_identifier=blockNumber)
            supplyLocked = positionData[0] / 10**tokenDecimals
        elif chainId == 252:
            # Fraxtal veFXS uses balanceOf instead of locked
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress), abi=erc20ABI
            )
            totalSupply = veContract.functions.balanceOf(
                Web3.to_checksum_address(lockerAddress)
            ).call(block_identifier=blockNumber)
            supplyLocked = totalSupply / 10**tokenDecimals
        else:
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress), abi=veABI
            )
            totalSupply = veContract.functions.locked(
                Web3.to_checksum_address(lockerAddress)
            ).call(block_identifier=blockNumber)
            supplyLocked = totalSupply[0] / 10**tokenDecimals

        coin_amount_locked = amount / 10**tokenDecimals

        # Check if we can send the Telegram notification
        if notSendMintNotif(coin_amount_locked, depositor):
            return

        formatted_coin_amount_received = abbreviate_number(coin_amount_locked)
        formatted_supply_locked = abbreviate_number(supplyLocked)

        explorerLink = get_explorer_link(chainId)
        txHash = event["transactionHash"].hex()

        msg = f"<a href='{explorerLink}/tx/{txHash}'>Mint :</a> {formatted_coin_amount_received} sd{tokenSymbol.upper()}\n"
        msg += getMsgUser(w3, event, explorerLink)
        msg += f"{tokenSymbol.upper()} locker : {formatted_supply_locked} sd{tokenSymbol.upper()}"

        send_telegram_message_curve(msg, chainId)

    except Exception as e:
        print(e)


def manageDepositWithTransfer(w3, event, chainId, lockersApi):
    try:
        blockNumber = event["blockNumber"]

        args = event["args"]
        amount = args["value"]
        token = event["address"]

        depositor = ""
        for depositor_transfer in Common.DEPOSITORS_TRANSFER[chainId]:
            if depositor_transfer["token"].lower() == token.lower():
                depositor = depositor_transfer["depositor"]
                break

        if len(depositor) == 0:
            return

        # Fetch depositor data
        lockerAddress = None
        veTokenAddress = None
        tokenSymbol = ""
        tokenDecimals = 18

        for locker in lockersApi:
            if ("modules" in locker) == False:
                continue

            modules = locker["modules"]
            token_locker = locker["token"]
            if modules["depositor"].lower() == depositor.lower():
                lockerAddress = Web3.to_checksum_address(modules["locker"])
                veTokenAddress = Web3.to_checksum_address(modules["veToken"])
                tokenSymbol = token_locker["symbol"]
                tokenDecimals = token_locker["decimals"]
                break

        if tokenSymbol.lower() == "B-80BAL-20WETH".lower():
            tokenSymbol = "BAL"

        if token.lower() == ContractRegistry.sdYND[1].lower():
            tokenSymbol = "YND"

        # Fetch supply locked
        supplyLocked = 0
        if chainId == 56:
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress), abi=veCakeABI
            )
            totalSupply = veContract.functions.locks(
                Web3.to_checksum_address(lockerAddress)
            ).call(block_identifier=blockNumber)
            supplyLocked = totalSupply[0] / 10**tokenDecimals
        elif tokenSymbol.lower() == "mav":
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress), abi=erc20ABI
            )
            totalSupply = veContract.functions.balanceOf(
                Web3.to_checksum_address(lockerAddress)
            ).call(block_identifier=blockNumber)
            supplyLocked = totalSupply / 10**tokenDecimals
        elif tokenSymbol.lower() == "pendle":
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress),
                abi=vePendleABI,
            )
            positionData = veContract.functions.positionData(
                Web3.to_checksum_address(lockerAddress)
            ).call(block_identifier=blockNumber)
            supplyLocked = positionData[0] / 10**tokenDecimals
        elif tokenSymbol.lower() == "spectra":
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress), abi=veSpectra
            )
            totalSupply = veContract.functions.locked(
                ContractRegistry.SD_SPECTRA_NFT_ID
            ).call(block_identifier=blockNumber)
            supplyLocked = totalSupply[0] / 10**tokenDecimals
        elif tokenSymbol.lower() == "ynd":
            sdYNDContract = w3.eth.contract(
                address=Web3.to_checksum_address(ContractRegistry.sdYND[1]), abi=erc20ABI
            )
            totalSupply = sdYNDContract.functions.totalSupply().call(
                block_identifier=blockNumber
            )
            supplyLocked = totalSupply / 10**tokenDecimals
        elif veTokenAddress is not None:
            veContract = w3.eth.contract(
                address=Web3.to_checksum_address(veTokenAddress), abi=veABI
            )
            totalSupply = veContract.functions.locked(
                Web3.to_checksum_address(lockerAddress)
            ).call(block_identifier=blockNumber)
            supplyLocked = totalSupply[0] / 10**tokenDecimals

        coin_amount_locked = amount / 10**tokenDecimals

        # Check if we can send the Telegram notification
        if notSendMintNotif(coin_amount_locked, depositor):
            return

        formatted_coin_amount_received = abbreviate_number(coin_amount_locked)
        formatted_supply_locked = abbreviate_number(supplyLocked)

        explorerLink = get_explorer_link(chainId)
        txHash = event["transactionHash"].hex()

        msg = f"<a href='{explorerLink}/tx/{txHash}'>Mint :</a> {formatted_coin_amount_received} sd{tokenSymbol.upper()}\n"
        msg += getMsgUser(w3, event, explorerLink)

        if supplyLocked > 0:
            msg += f"{tokenSymbol.upper()} locker : {formatted_supply_locked} sd{tokenSymbol.upper()}"

        send_telegram_message_curve(msg, chainId)

    except Exception as e:
        print(e)


def getPoolBalancesMsg(w3, poolAddress, pool, blockNumber):
    msg = f"Pool balance:\n"

    # First, collect all balances
    balances = []
    total_balance = 0

    for coin in pool["coins"]:
        coinContract = w3.eth.contract(
            address=Web3.to_checksum_address(coin["address"]), abi=erc20ABI
        )
        coinBalanceBN = coinContract.functions.balanceOf(
            Web3.to_checksum_address(poolAddress)
        ).call(block_identifier=blockNumber)

        coinBalance = coinBalanceBN / 10 ** int(coin["decimals"])
        total_balance += coinBalance

        balances.append({"balance": coinBalance, "symbol": coin["symbol"]})

    # Then calculate percentages based on token amounts
    for balance_data in balances:
        coinBalance = balance_data["balance"]
        coinSymbol = balance_data["symbol"]

        percentage = (
            (coinBalance * 100 / total_balance) if total_balance > 0 else 0
        )

        formatted_coinBalance = abbreviate_number(coinBalance)
        formatted_percentage = "{:,.2f}".format(percentage)

        msg += (
            f"{formatted_coinBalance} {coinSymbol} {formatted_percentage}%\n"
        )

    return msg


def copyEvent(event, type):
    return {
        "address": event["address"],
        "transactionHash": event["transactionHash"],
        "blockNumber": event["blockNumber"],
        "args": event["args"],
        "type": type,
        "logIndex": event["logIndex"],
    }


def get_pools_from_api():
    try:
        allPools = requests.get(f"{Bots.CURVE_API}/v1/getPools/all").json()[
            "data"
        ]["poolData"]
        return allPools
    except Exception:
        return load_json("bots/allPools")


def job():
    # config = loadConfigFromFile()
    lockersApi = load_lockers()

    checkpoints = {}

    web3_service = get_web3_service()
    for chainId in GlobalConstants.CHAIN_ID_TO_PUBLIC_RPC:
        if chainId not in BLOCKCHAIN_IDS:
            continue
        web3_service.add_chain(chainId)
        allEvents = []

        web3 = web3_service.get_w3(chainId)

        # Get the last block fetched
        from_block, current_block = get_block_range(WORKFLOW_NAME, chainId, web3, checkpoints)

        if from_block == 0:
            continue

        blockchainId = BLOCKCHAIN_IDS[chainId]

        if chainId in POOLS_PER_CHAIN:
            pools = POOLS_PER_CHAIN[chainId]
            for poolAddress in pools:
                if chainId == 56:
                    if ContractRegistry.sdCAKE_CAKE_V3[56].lower() == poolAddress.lower():
                        # Create pool contract
                        poolContract = web3.eth.contract(
                            address=Web3.to_checksum_address(poolAddress),
                            abi=pancakeV3PoolABI,
                        )

                        # Fetch all events
                        tokenEchanges = poolContract.events.Swap().get_logs(
                            from_block=from_block, to_block=current_block
                        )
                        for event in tokenEchanges:
                            allEvents.append(
                                copyEvent(event, EVENT_TYPES["SWAP_V3"])
                            )
                    if ContractRegistry.sdCAKE_CAKE_STABLE[56].lower() == poolAddress.lower():
                        # Create pool contract
                        poolContract = web3.eth.contract(
                            address=Web3.to_checksum_address(poolAddress),
                            abi=stableCakePoolABI,
                        )

                        # Fetch all events
                        tokenEchanges = (
                            poolContract.events.TokenExchange().get_logs(
                                from_block=from_block,
                                to_block=current_block,
                            )
                        )
                        for event in tokenEchanges:
                            allEvents.append(
                                copyEvent(event, EVENT_TYPES["SWAP_STABLE"])
                            )

                        addLiquidity = (
                            poolContract.events.AddLiquidity().get_logs(
                                from_block=from_block,
                                to_block=current_block,
                            )
                        )
                        for event in addLiquidity:
                            allEvents.append(
                                copyEvent(
                                    event, EVENT_TYPES["ADD_LIQUIDITY_STABLE"]
                                )
                            )

                if poolAddress.lower() == ContractRegistry.sdBAL_POOL[1].lower():
                    # https://etherscan.io/tx/0x3719c9d688bfc9c665341c39e1ce7608b4cfd8de8c51c717fcb68f206cbdea14#eventlog
                    continue
                else:
                    for abi in [curveStableSwapNGABI, curveStableSwapABI]:
                        # Create pool contract
                        poolContract = web3.eth.contract(
                            address=Web3.to_checksum_address(poolAddress),
                            abi=abi,
                        )

                        # Fetch all events
                        tokenEchanges = (
                            poolContract.events.TokenExchange().get_logs(
                                from_block=from_block,
                                to_block=current_block,
                            )
                        )
                        for event in tokenEchanges:
                            allEvents.append(
                                copyEvent(event, EVENT_TYPES["TOKEN_EXCHANGE"])
                            )

                        addLiquidities = (
                            poolContract.events.AddLiquidity().get_logs(
                                from_block=from_block,
                                to_block=current_block,
                            )
                        )
                        for event in addLiquidities:
                            allEvents.append(
                                copyEvent(event, EVENT_TYPES["ADD_LIQUIDITY"])
                            )

                        removeLiquidities = (
                            poolContract.events.RemoveLiquidity().get_logs(
                                from_block=from_block,
                                to_block=current_block,
                            )
                        )
                        for event in removeLiquidities:
                            allEvents.append(
                                copyEvent(
                                    event, EVENT_TYPES["REMOVE_LIQUIDITY"]
                                )
                            )

                        removeLiquiditiesOne = (
                            poolContract.events.RemoveLiquidityOne().get_logs(
                                from_block=from_block,
                                to_block=current_block,
                            )
                        )
                        for event in removeLiquiditiesOne:
                            allEvents.append(
                                copyEvent(
                                    event,
                                    EVENT_TYPES["REMOVE_LIQUIDITY_ONE_COIN"],
                                )
                            )

        if chainId in Common.DEPOSITORS:
            depositors = Common.DEPOSITORS[chainId]
            for depositor in depositors:
                if chainId == 56 or chainId == 252:
                    depositorContract = web3.eth.contract(
                        address=Web3.to_checksum_address(depositor),
                        abi=cakeDepositorABI,
                    )
                    deposits = depositorContract.events.Deposited().get_logs(
                        from_block=from_block, to_block=current_block
                    )
                    for event in deposits:
                        allEvents.append(
                            copyEvent(event, EVENT_TYPES["DEPOSITED"])
                        )
                else:
                    depositorContract = web3.eth.contract(
                        address=Web3.to_checksum_address(depositor),
                        abi=depositorABI,
                    )
                    deposits = depositorContract.events.Deposited().get_logs(
                        from_block=from_block, to_block=current_block
                    )
                    for event in deposits:
                        allEvents.append(
                            copyEvent(event, EVENT_TYPES["DEPOSITED"])
                        )

        # Fetch mints from transfer
        if chainId in Common.DEPOSITORS_TRANSFER:
            depositorsTransfers = Common.DEPOSITORS_TRANSFER[chainId]
            for depositorsTransfer in depositorsTransfers:
                tokenContract = web3.eth.contract(
                    address=Web3.to_checksum_address(
                        depositorsTransfer["token"]
                    ),
                    abi=erc20ABI,
                )
                deposits = tokenContract.events.Transfer().get_logs(
                    from_block=from_block,
                    to_block=current_block,
                    argument_filters={
                        "from": Web3.to_checksum_address(ZERO_ADDRESS)
                    },
                )

                for event in deposits:
                    allEvents.append(
                        copyEvent(event, EVENT_TYPES["DEPOSITED_V2"])
                    )

        # Remove same event
        eventsMap = {}
        for event in allEvents:
            hash = event["transactionHash"].hex()
            if hash not in eventsMap:
                eventsMap[hash] = {}

            logIndex = str(event["logIndex"])
            if logIndex not in eventsMap[hash]:
                eventsMap[hash][logIndex] = event

        allEvents = []
        for hash in eventsMap:
            for logIndex in eventsMap[hash]:
                allEvents.append(eventsMap[hash][logIndex])

        # Sort event by block number
        allEvents.sort(key=lambda x: x["blockNumber"])

        # Manage events
        allPools = get_pools_from_api()
        for event in allEvents:
            if event["type"] == EVENT_TYPES["TOKEN_EXCHANGE"]:
                manageTokenEchange(
                    web3,
                    event["address"],
                    event,
                    allPools,
                    blockchainId,
                    chainId,
                )
            elif event["type"] == EVENT_TYPES["ADD_LIQUIDITY"]:
                manageAddLiquidity(
                    web3,
                    event["address"],
                    event,
                    allPools,
                    blockchainId,
                    chainId,
                )
            elif event["type"] == EVENT_TYPES["REMOVE_LIQUIDITY"]:
                manageRemoveLiquidity(
                    web3,
                    event["address"],
                    event,
                    allPools,
                    blockchainId,
                    chainId,
                )
            elif event["type"] == EVENT_TYPES["REMOVE_LIQUIDITY_ONE_COIN"]:
                manageRemoveLiquidityOneCoin(
                    web3,
                    event["address"],
                    event,
                    allPools,
                    blockchainId,
                    chainId,
                )
            elif event["type"] == EVENT_TYPES["SWAP_V3"]:
                manageSwapV3(web3, event["address"], event, chainId)
            elif event["type"] == EVENT_TYPES["SWAP_STABLE"]:
                manageSwapStable(web3, event["address"], event, chainId)
            elif event["type"] == EVENT_TYPES["DEPOSITED"]:
                manageDeposit(web3, event, chainId, lockersApi)
            elif event["type"] == EVENT_TYPES["DEPOSITED_V2"]:
                manageDepositWithTransfer(web3, event, chainId, lockersApi)
            elif event["type"] == EVENT_TYPES["ADD_LIQUIDITY_STABLE"]:
                manageAddLiquidityStable(
                    web3, event["address"], event, chainId
                )


def main():
    try:
        job()
    except Exception as e:
        print(e)
        raise


__name__ == "__main__" and main()

# export PYTHONPATH=script/
# source env/bin/activate
