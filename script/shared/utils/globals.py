import json


import logging


import os


import time


from typing import Optional


import redis


import requests


from dotenv import load_dotenv


from eth_utils.address import to_checksum_address


from shared.constants import Common, GlobalConstants


from shared.utils.rate_limiter import api_rate_limiters


load_dotenv(override=True)


_price_cache = {}


_cache_ttl = 300  # 5 minutes TTL


def is_production() -> bool:
    """
    Check if running in production mode (case-insensitive).

    Returns True only when PROD environment variable is explicitly set
    to "true" (case-insensitive). This ensures test mode (PROD not set)
    doesn't trigger production-only actions like workflow triggers.

    Examples:
        PROD=True    -> True
        PROD=true    -> True
        PROD=TRUE    -> True
        PROD=False   -> False
        PROD=false   -> False
        PROD=        -> False
        (not set)    -> False
    """
    return os.getenv("PROD", "").lower() == "true"


def pad_address(address):
    # Remove the '0x' prefix
    address = address[2:]
    # Pad the address to 64 characters with zeros
    padded_address = address.zfill(64)
    # Add the '0x' prefix back
    padded_address = "0x" + padded_address
    return padded_address


def load_json(name):
    with open("json/" + name + ".json", "r") as f:
        return json.load(f)


def decode_hex_data(hex_data, types):
    # Define the size of each type in bytes
    type_sizes = {
        "address": 64,
        "uint256": 64,
        "int128": 64,
    }

    # Initialize the starting index
    start = 0

    # Initialize the result dictionary
    result = []

    for type_name in types:
        # Calculate the end index
        end = start + type_sizes[type_name]

        # Extract and decode the data
        data = hex_data[start:end]
        if type_name == "address":
            data = "0x" + data[24:]  # remove leading zeros and add '0x' prefix
        elif type_name == "uint256" or type_name == "int128":
            data = int(data, 16)

        # Add the decoded data to the result
        result.append(data)

        # Move the start index to the end
        start = end

    return result


def load_contract_w3(w3, addr, abi):
    return w3.eth.contract(address=addr, abi=load_json("abi/" + abi))


def get_token_price(
    chain_id: int,
    token: str | None = None,
    unformatted_amount: int = 10**18,
    timestamp: int | None = None,
    is_native: bool = False,
):
    """
    Fetches the price of any token (native or ERC20) in USD using Defillama API.

    Args:
        chain_id (int): The chain ID where the token is deployed.
        token (str, optional): The token address. If None or is_native=True, gets native token price.
        unformatted_amount (int): The amount of the token (unformatted). Defaults to 10**18.
        timestamp (int, optional): The timestamp for historical price.
        is_native (bool): If True, gets the native token price regardless of token address.

    Returns:
        tuple: A tuple containing the formatted price string and the price as a float.
    """
    network = Common.chains_ids_to_name.get(chain_id)
    if not network:
        return "0.00", 0.0

    # For native tokens, use address 0
    if is_native or token is None:
        token_address = "0x0000000000000000000000000000000000000000"
    else:
        token_address = to_checksum_address(token.lower())

    all_params = f"{network}:{token_address}"

    # Check cache first
    cache_key = f"{all_params}:{timestamp or 'current'}"
    current_time = time.time()

    # Source of truth: Data Hub. Skip for native tokens (address 0) and
    # for historical lookups (Data Hub only exposes current prices).
    if (
        not is_native
        and token_address != "0x0000000000000000000000000000000000000000"
        and timestamp is None
    ):
        try:
            from shared.services.data_hub_service import (
                get_token_decimals as _hub_decimals,
                get_token_price_usd as _hub_price,
            )

            hub_price = _hub_price(chain_id, token_address)
            if hub_price is not None and hub_price > 0:
                cached_decimals: Optional[int] = None
                if cache_key in _price_cache:
                    cached_data = _price_cache[cache_key]
                    if isinstance(cached_data, tuple) and len(cached_data) >= 3:
                        cached_decimals = cached_data[2]

                decimals = (
                    cached_decimals
                    if cached_decimals is not None
                    else _hub_decimals(chain_id, token_address)
                )
                if decimals is not None:
                    amount = int(unformatted_amount)
                    price = hub_price * (amount / 10**decimals)
                    _price_cache[cache_key] = (
                        hub_price,
                        current_time,
                        decimals,
                    )
                    return "{:,.2f}".format(price), price
        except Exception as exc:
            logging.warning(f"Data hub primary lookup failed: {exc}")

    if cache_key in _price_cache:
        cached_data = _price_cache[cache_key]
        if isinstance(cached_data, tuple) and len(cached_data) >= 2:
            cached_price, cached_time = cached_data[0], cached_data[1]
            # Check if we also cached decimals (new format)
            cached_decimals = cached_data[2] if len(cached_data) > 2 else 18

            if current_time - cached_time < _cache_ttl:
                # Use cached price
                if cached_price > 0:
                    amount = int(unformatted_amount)
                    price = cached_price * (amount / 10**cached_decimals)
                    return "{:,.2f}".format(price), price

    # Determine API endpoint
    if timestamp:
        all_uris = (
            f"https://coins.llama.fi/prices/historical/"
            f"{timestamp}/{all_params}"
        )
    else:
        all_uris = f"https://coins.llama.fi/prices/current/{all_params}"

    try:
        # Apply rate limiting for Defillama
        defillama_limiter = api_rate_limiters.get_limiter("defillama")
        defillama_limiter.wait_if_needed()

        response = requests.get(all_uris, timeout=30)
        response.raise_for_status()
        all_prices = response.json()

        if (
            "error" in all_prices
            or "coins" not in all_prices
            or len(all_prices["coins"]) == 0
        ):
            price = 0
        else:
            prices = all_prices["coins"]
            price_info = prices.get(all_params)
            if price_info:
                decimals = int(price_info["decimals"])
                amount = int(unformatted_amount)
                token_price = float(price_info["price"])
                price = token_price * (amount / 10**decimals)
                # Cache the price per token with decimals
                _price_cache[cache_key] = (token_price, current_time, decimals)
            else:
                price = 0

        # If Defillama returns 0 and it's not a native token, try GeckoTerminal
        # API as fallback. Skip for historical lookups: GeckoTerminal returns
        # current spot price only, which would silently corrupt historical data.
        if (
            price == 0
            and not is_native
            and timestamp is None
            and token_address != "0x0000000000000000000000000000000000000000"
        ):
            try:
                # Get chain abbreviation for GeckoTerminal
                chain_abbr = Common.chains_ids_to_abbreviation.get(chain_id)
                if chain_abbr:
                    base_url = (
                        "https://api.geckoterminal.com/api/v2/simple/networks"
                    )
                    token_url = f"{chain_abbr}/token_price/{token_address}"
                    gecko_url = f"{base_url}/{token_url}"

                    # Apply rate limiting for GeckoTerminal
                    gecko_limiter = api_rate_limiters.get_limiter(
                        "geckoterminal"
                    )
                    gecko_limiter.wait_if_needed()

                    gecko_response = requests.get(gecko_url, timeout=30)
                    gecko_response.raise_for_status()
                    gecko_data = gecko_response.json()

                    token_prices = (
                        gecko_data.get("data", {})
                        .get("attributes", {})
                        .get("token_prices", {})
                    )
                    if token_prices:
                        token_price_str = token_prices.get(
                            token_address.lower()
                        )
                        if token_price_str:
                            token_price = float(token_price_str)
                            # Fallback to 18 decimals if not in Defillama
                            decimals = 18
                            amount = int(unformatted_amount)
                            price = token_price * (amount / 10**decimals)
                            # Cache the price
                            _price_cache[cache_key] = (
                                token_price,
                                current_time,
                                decimals,
                            )
            except Exception as e:
                logging.warning(f"GeckoTerminal API error: {e}")

        return "{:,.2f}".format(price), price

    except Exception as e:
        logging.error(
            f"Error fetching price for {network}:{token_address}: {str(e)}"
        )
        # Return cached price if available even if expired
        if cache_key in _price_cache:
            cached_data = _price_cache[cache_key]
            if isinstance(cached_data, tuple) and len(cached_data) >= 2:
                cached_price = cached_data[0]
                cached_decimals = (
                    cached_data[2] if len(cached_data) > 2 else 18
                )
                if cached_price > 0:
                    amount = int(unformatted_amount)
                    price = cached_price * (amount / 10**cached_decimals)
                    return "{:,.2f}".format(price), price

        return "0.00", 0.0


def replace_double_quotes_with_single(html_content):
    """
    Replaces all double quotes in a string with single quotes.

    :param html_content: The HTML content as a string.
    :return: The modified HTML content with double quotes replaced by single quotes.
    """
    return html_content.replace('"', "'")


def get_redis_client():
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        password=os.environ["REDIS_PASSWORD"],
        ssl=True,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=30,
    )


def getHistoricalTokenPrice(tokenAddress, timestamp, chain):
    key = chain + tokenAddress.lower()
    response = requests.get(
        "https://coins.llama.fi/prices/historical/"
        + str(timestamp)
        + "/"
        + key
    )

    time.sleep(0.2)  # Sleep 200ms

    if response.status_code != 200:
        return 0

    resp = response.json()
    if ("coins" in resp) == False:
        return 0

    if (key in resp["coins"]) == False:
        return 0

    if ("price" in resp["coins"][key]) == False:
        return 0

    return resp["coins"][key]["price"]
