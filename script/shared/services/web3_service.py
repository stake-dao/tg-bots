from functools import wraps


from time import time


from typing import Any, Dict, List, Optional, Tuple


import requests


from requests.adapters import HTTPAdapter


from urllib3.util.retry import Retry


from shared.constants import GlobalConstants


from shared.utils.globals import load_json


from w3multicall.multicall import W3Multicall


from web3 import HTTPProvider, Web3


try:  # web3.py 6.x
    from web3._utils.request import (
        cache_and_return_session as _cache_w3_session_module,
    )
except ImportError:  # web3.py 7.x — handled via provider._request_session_manager
    _cache_w3_session_module = None


def _make_retry_session() -> requests.Session:
    """Create a requests session with retry logic for transient errors."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=None,  # Retry JSON-RPC POST requests in addition to idempotent methods
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class FallbackHTTPProvider(HTTPProvider):
    """HTTPProvider that rotates through a list of RPC endpoints.

    On a request failure that looks transient (HTTP 429/5xx, connection drop,
    timeout) it advances to the next endpoint and retries. The current endpoint
    is sticky across requests, so once a healthy one is promoted it stays in use
    until it too fails.
    """

    def __init__(self, endpoint_uris: List[str], **kwargs: Any) -> None:
        # Deduplicate while preserving order; drop empties.
        self._endpoints: List[str] = [u for u in dict.fromkeys(endpoint_uris) if u]
        if not self._endpoints:
            raise ValueError(
                "FallbackHTTPProvider requires at least one endpoint URI"
            )
        self._endpoint_index = 0
        # Let HTTPProvider create/cache the session for the first endpoint, then
        # register a retry session for every endpoint we might rotate to.
        kwargs.pop("session", None)
        super().__init__(self._endpoints[0], session=_make_retry_session(), **kwargs)
        for uri in self._endpoints[1:]:
            self._register_retry_session(uri)

    def _register_retry_session(self, uri: str) -> None:
        """Best-effort: bind a retrying requests session to ``uri``."""
        session = _make_retry_session()
        # web3.py 7.x: per-provider session manager.
        manager = getattr(self, "_request_session_manager", None)
        if manager is not None and hasattr(manager, "cache_and_return_session"):
            try:
                manager.cache_and_return_session(uri, session)
                return
            except Exception:  # pragma: no cover - defensive
                pass
        # web3.py 6.x: module-level session cache.
        if _cache_w3_session_module is not None:
            try:
                _cache_w3_session_module(uri, session)
            except Exception:  # pragma: no cover - defensive
                pass

    def _advance(self) -> None:
        self._endpoint_index = (self._endpoint_index + 1) % len(self._endpoints)
        self.endpoint_uri = self._endpoints[self._endpoint_index]

    def make_request(self, method: Any, params: Any) -> Any:
        last_error: Optional[BaseException] = None
        for _ in range(len(self._endpoints)):
            try:
                return super().make_request(method, params)
            except (
                requests.exceptions.HTTPError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as error:
                last_error = error
                self._advance()
        assert last_error is not None
        raise last_error


try:
    from web3.middleware import geth_poa_middleware
except ImportError:
    try:
        from web3.middleware import (
            ExtraDataToPOAMiddleware as geth_poa_middleware,
        )
    except ImportError:
        # For even newer versions
        from web3.middleware import ExtraDataToPOAMiddleware

        geth_poa_middleware = ExtraDataToPOAMiddleware


def build_web3(chain_id: int, override: Optional[str] = None) -> Web3:
    """Build a fallback-aware Web3 instance for a chain.

    The endpoint order comes from ``GlobalConstants.rpc_endpoints`` (RouteMesh ->
    Alchemy -> public -> chainlist backups). ``override`` is an optional explicit
    endpoint prepended ahead of that list, for the rare caller that needs a
    specific node (e.g. an archive/debug RPC). POA middleware is injected for
    BSC/Polygon. This is the single construction point every connection uses.
    """
    endpoints = GlobalConstants.rpc_endpoints(chain_id)
    if override and override not in endpoints:
        endpoints = [override, *endpoints]
    w3 = Web3(FallbackHTTPProvider(endpoints))
    if chain_id in (56, 137):  # BSC and Polygon require POA middleware
        try:
            if hasattr(w3, "middleware_onion"):
                w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        except Exception:
            try:
                w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except AttributeError:
                pass
    return w3


def ttl_cache(ttl_seconds: int = 300):
    """
    A simple TTL cache decorator.

    Caches the result of the decorated function for a specified number of seconds.

    Args:
        ttl_seconds (int): Time-to-live in seconds.
    """

    def decorator(func):
        cache: Dict[str, Tuple[Any, float]] = {}

        @wraps(func)
        def wrapper(*args, **kwargs):
            key = str(args) + str(kwargs)
            now = time()
            if key in cache:
                result, timestamp = cache[key]
                if now - timestamp < ttl_seconds:
                    return result
            result = func(*args, **kwargs)
            cache[key] = (result, now)
            return result

        return wrapper

    return decorator


class Web3Service:
    """
    A service class for managing Web3 connections and interactions.

    This class allows you to add multiple blockchain networks, retrieve the
    corresponding Web3 instance, and cache common queries (such as latest block,
    token metadata, contract data, balances, etc.).
    """

    def __init__(
        self,
        default_chain_id: int = 1,
        default_rpc_url: Optional[str] = None,
    ) -> None:
        """
        Initialize the Web3Service with a default chain.

        Args:
            default_chain_id (int): The default blockchain chain ID.
            default_rpc_url (Optional[str]): Optional explicit RPC override for
                the default chain; normally None so the standard failover chain
                (RouteMesh -> Alchemy -> public) is used.
        """
        self.w3: Dict[int, Web3] = {}
        self.initialize(default_chain_id, default_rpc_url)
        global web3_service
        web3_service = self

    def initialize(
        self, default_chain_id: int, default_rpc_url: Optional[str] = None
    ) -> None:
        """
        Initialize the service by setting the default chain and creating caches.

        Args:
            default_chain_id (int): The default chain ID.
            default_rpc_url (Optional[str]): Optional explicit RPC override for
                the default chain.
        """
        self.default_chain_id = default_chain_id
        self.add_chain(default_chain_id, default_rpc_url)
        # Initialize caches
        self._latest_block_cache: Dict[
            Tuple[Optional[int], str], Dict[str, Any]
        ] = {}
        self._token_info_cache: Dict[Tuple[int, str], Dict[str, Any]] = {}
        self._contract_cache: Dict[Tuple[int, str, str], Any] = {}
        self._erc20_balance_cache: Dict[Tuple[int, str, str], int] = {}
        self._multiple_balances_cache: Dict[
            Tuple[int, str, Tuple[str, ...], bool], Dict[str, int]
        ] = {}
        self._block_cache: Dict[int, Dict[str, Any]] = {}
        self._gwei_cache: Dict[int, float] = {}
        self._tx_receipt_cache: Dict[
            Tuple[str, Optional[int]], Dict[str, Any]
        ] = {}

    def add_chain(self, chain_id: int, rpc_url: Optional[str] = None) -> None:
        """
        Add a new blockchain network.

        Delegates to :func:`build_web3` so every connection in the repo is
        constructed the same way: a ``FallbackHTTPProvider`` over the standard
        failover chain (RouteMesh -> Alchemy -> public -> backups). ``rpc_url``
        is an optional explicit override, prepended ahead of that chain.

        Args:
            chain_id (int): The blockchain network's chain ID.
            rpc_url (Optional[str]): Optional explicit RPC override.
        """
        self.w3[chain_id] = build_web3(chain_id, rpc_url)

    def get_w3(self, chain_id: Optional[int] = None) -> Web3:
        """
        Retrieve the Web3 instance for a specific chain.

        Args:
            chain_id (Optional[int]): The chain ID. If None, uses the default chain.

        Returns:
            Web3: The Web3 instance for the chain.

        Raises:
            ValueError: If the requested chain is not initialized.
        """
        if chain_id is None:
            chain_id = self.default_chain_id
        if chain_id not in self.w3:
            self.add_chain(chain_id)
        return self.w3[chain_id]


    @ttl_cache(ttl_seconds=3600)
    def get_token_info(
        self, token_addresses: List[str], chain_id: Optional[int] = None
    ) -> Dict[str, Dict[str, Any]]:
        if chain_id is None:
            chain_id = self.default_chain_id

        # find which addresses *for this chain* are uncached
        uncached = [
            addr
            for addr in token_addresses
            if (chain_id, addr) not in self._token_info_cache
        ]
        if uncached:
            multicall = W3Multicall(self.get_w3(chain_id))
            for addr in uncached:
                multicall.add(W3Multicall.Call(addr, "name()(string)", []))
                multicall.add(W3Multicall.Call(addr, "symbol()(string)", []))
                multicall.add(W3Multicall.Call(addr, "decimals()(uint8)", []))
            results = multicall.call()
            for i, addr in enumerate(uncached):
                self._token_info_cache[(chain_id, addr)] = {
                    "name": results[i * 3],
                    "symbol": results[i * 3 + 1],
                    "decimals": results[i * 3 + 2],
                }

        # return only the entries for this chain
        return {
            addr: self._token_info_cache[(chain_id, addr)]
            for addr in token_addresses
        }


    def get_block(
        self, block_identifier: int, chain_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Retrieve a block by its number or identifier.

        Args:
            block_identifier (int): The block number.
            chain_id (Optional[int]): The chain ID.

        Returns:
            Dict[str, Any]: The block data.
        """
        if block_identifier not in self._block_cache:
            self._block_cache[block_identifier] = self.get_w3(
                chain_id
            ).eth.get_block(block_identifier)
        return self._block_cache[block_identifier]

    def get_contract(
        self, address: str, abi_name: str, chain_id: Optional[int] = None
    ) -> Any:
        """
        Retrieve a contract instance given its address and ABI name.

        Args:
            address (str): The contract address.
            abi_name (str): The name of the ABI file (without path).
            chain_id (Optional[int]): The chain ID.

        Returns:
            Any: The contract instance.
        """
        if chain_id is None:
            chain_id = self.default_chain_id
        key = (chain_id, address, abi_name)
        if key not in self._contract_cache:
            abi = load_json("abi/" + abi_name)
            self._contract_cache[key] = self.get_w3(chain_id).eth.contract(
                address=Web3.to_checksum_address(address.lower()), abi=abi
            )
        return self._contract_cache[key]


    @ttl_cache(ttl_seconds=300)  # Cache for 5 minutes
    def get_transaction_receipt(
        self, tx_hash: str, chain_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve the transaction receipt for a given transaction hash.

        Args:
            tx_hash (str): The transaction hash.
            chain_id (Optional[int]): The chain ID. Defaults to default chain.

        Returns:
            Optional[Dict[str, Any]]: The transaction receipt data or None if not
                found.
        """
        key = (tx_hash, chain_id)
        if key not in self._tx_receipt_cache:
            try:
                receipt = self.get_w3(chain_id).eth.get_transaction_receipt(
                    tx_hash
                )
                self._tx_receipt_cache[key] = receipt
            except Exception:
                # Transaction might not be mined yet or could be invalid
                return None
        return self._tx_receipt_cache[key]


web3_service: Optional[Web3Service] = None


def get_web3_service(
    default_chain_id: Optional[int] = 1,
    default_rpc_url: Optional[str] = None,
) -> Web3Service:
    """
    Get the global Web3Service instance.

    If not already initialized, create a default instance. ``default_rpc_url`` is
    an optional explicit override for the default chain; normally None so the
    standard failover chain (RouteMesh -> Alchemy -> public) is used.

    Args:
        default_chain_id (Optional[int]): The default chain ID to use if
            initialization is needed.
        default_rpc_url (Optional[str]): Optional explicit RPC override.

    Returns:
        Web3Service: The global instance.

    Raises:
        RuntimeError: If not initialized and no default chain id provided.
    """
    global web3_service
    if web3_service is None:
        if default_chain_id is None:
            raise RuntimeError(
                "Web3Service not initialized. Please create a Web3Service instance first."
            )
        web3_service = Web3Service(default_chain_id, default_rpc_url)
    return web3_service
