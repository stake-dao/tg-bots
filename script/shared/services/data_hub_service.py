import asyncio


import logging


import os


import time


from typing import Any, Dict, List, Optional, Tuple


import httpx


import requests


from dotenv import load_dotenv


load_dotenv()


logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "https://data-hub.contact-69d.workers.dev"


CACHE_TTL_SECONDS = 300


MAX_RETRIES = 3


BACKOFF_FACTOR = 2


INITIAL_DELAY = 1


REQUEST_TIMEOUT = 30


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class DataHubError(Exception):
    pass


class DataHubService:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or os.getenv("DATA_HUB_URL", DEFAULT_BASE_URL)
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: dict[str, tuple[float, Any]] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(REQUEST_TIMEOUT),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept": "application/json",
                },
            )
        return self._client

    def _get_cached(self, key: str) -> Optional[Any]:
        if key in self._cache:
            timestamp, value = self._cache[key]
            if time.time() - timestamp < CACHE_TTL_SECONDS:
                return value
            del self._cache[key]
        return None

    def _set_cached(self, key: str, value: Any) -> None:
        self._cache[key] = (time.time(), value)

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        client = self._get_client()
        last_exc: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code not in RETRYABLE_STATUS_CODES:
                    raise DataHubError(
                        f"HTTP {e.response.status_code} on {method} {path}"
                    ) from e
                logger.warning(
                    "data_hub_request_retry",
                    extra={
                        "attempt": attempt + 1,
                        "status": e.response.status_code,
                        "path": path,
                    },
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_exc = e
                logger.warning(
                    "data_hub_request_retry",
                    extra={
                        "attempt": attempt + 1,
                        "error": str(e),
                        "path": path,
                    },
                )

            if attempt < MAX_RETRIES - 1:
                delay = INITIAL_DELAY * (BACKOFF_FACTOR ** attempt)
                await asyncio.sleep(delay)

        raise DataHubError(f"Failed after {MAX_RETRIES} retries on {path}") from last_exc

    async def get_vaults(self, chain_id: Optional[int] = None) -> list[dict]:
        cache_key = f"vaults:{chain_id or 'all'}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        data = await self._request("GET", "/v1/vaults")
        if not isinstance(data, list):
            raise DataHubError(f"Expected list from /v1/vaults, got {type(data).__name__}")

        if chain_id is not None:
            data = [v for v in data if v.get("chainId") == chain_id]

        self._set_cached(cache_key, data)
        return data

    async def get_adapted_vaults(self, chain_id: Optional[int] = None) -> List[dict]:
        from shared.utils.vault_adapter import adapt_hub_vault

        hub_vaults = await self.get_vaults(chain_id=chain_id)
        return [adapt_hub_vault(v) for v in hub_vaults]

    async def get_vault_aprs(self, chain_id: Optional[int] = None) -> Dict[str, float]:
        from shared.utils.vault_adapter import get_apr_from_hub_vault

        hub_vaults = await self.get_vaults(chain_id=chain_id)
        result: Dict[str, float] = {}
        for hv in hub_vaults:
            addr = hv.get("vault", "")
            if addr:
                result[addr.lower()] = get_apr_from_hub_vault(hv)
        return result

    async def get_gauge_tvls(self, chain_id: Optional[int] = None) -> Dict[str, float]:
        from shared.utils.vault_adapter import adapt_hub_vault

        hub_vaults = await self.get_vaults(chain_id=chain_id)
        result: Dict[str, float] = {}
        for hv in hub_vaults:
            v = adapt_hub_vault(hv)
            gauge = v.get("gauge", {})
            addr = gauge.get("address", "").lower()
            tvl = float(v.get("totalSupplyUSD", 0) or 0)
            if addr and tvl > 0:
                result[addr] = tvl
        return result

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


def _run_async(coro):
    return asyncio.run(coro)


def fetch_adapted_vaults(chain_id: Optional[int] = None) -> List[dict]:
    async def _do():
        async with DataHubService() as hub:
            return await hub.get_adapted_vaults(chain_id=chain_id)
    return _run_async(_do())


def fetch_vault_aprs(chain_id: Optional[int] = None) -> Dict[str, float]:
    async def _do():
        async with DataHubService() as hub:
            return await hub.get_vault_aprs(chain_id=chain_id)
    return _run_async(_do())


def fetch_gauge_tvls(chain_id: Optional[int] = None) -> Dict[str, float]:
    async def _do():
        async with DataHubService() as hub:
            return await hub.get_gauge_tvls(chain_id=chain_id)
    return _run_async(_do())


PRICES_CACHE_TTL_SECONDS = 60


_prices_cache: Dict[int, Tuple[float, Dict[str, float]]] = {}


_decimals_cache: Dict[Tuple[int, str], int] = {}


_ERC20_DECIMALS_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    }
]


def fetch_token_prices(chain_id: int) -> Dict[str, float]:
    """
    Fetch all USD prices for a chain from the Data Hub.

    Returns mapping {address_lower: usd_price}. Uses an in-process TTL cache
    so the call is shared across all callers within the process.
    """
    now = time.time()
    cached = _prices_cache.get(chain_id)
    if cached and now - cached[0] < PRICES_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        response = requests.get(
            f"{os.getenv('DATA_HUB_URL', DEFAULT_BASE_URL)}/v1/prices",
            params={"chainId": chain_id},
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(
            f"Data hub price fetch failed for chain {chain_id}: {exc}"
        )
        return cached[1] if cached else {}

    prices: Dict[str, float] = {}
    if isinstance(data, list):
        for item in data:
            address = (item.get("address") or "").lower()
            usd_price = item.get("usdPrice")
            if not address or usd_price is None:
                continue
            try:
                prices[address] = float(usd_price)
            except (TypeError, ValueError):
                continue

    _prices_cache[chain_id] = (now, prices)
    return prices


def get_token_price_usd(chain_id: int, token_address: str) -> Optional[float]:
    """Return the Data Hub USD price for a token, or None if unknown."""
    if not token_address:
        return None
    return fetch_token_prices(chain_id).get(token_address.lower())


def get_token_decimals(chain_id: int, token_address: str) -> Optional[int]:
    """
    Return ERC20 `decimals()` for a token. Result is cached forever in-process.

    Used as a fallback when an upstream price source (Data Hub) does not
    return decimals so we can convert raw amounts to USD.
    """
    if not token_address:
        return None

    key = (chain_id, token_address.lower())
    if key in _decimals_cache:
        return _decimals_cache[key]

    try:
        from web3 import Web3

        from shared.services.web3_service import get_web3_service

        w3 = get_web3_service().get_w3(chain_id)
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=_ERC20_DECIMALS_ABI,
        )
        decimals = int(contract.functions.decimals().call())
    except Exception as exc:
        logger.warning(
            f"decimals() call failed for {chain_id}:{token_address}: {exc}"
        )
        return None

    _decimals_cache[key] = decimals
    return decimals
