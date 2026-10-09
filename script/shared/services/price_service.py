import logging


import os


import time


from typing import Dict, List, Optional


import requests


from dotenv import load_dotenv


from shared.models.token_info import TokenIdentifier


load_dotenv(override=True)


logger = logging.getLogger(__name__)


class PriceCache:
    """Simple in-memory price cache with TTL."""

    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, tuple[float, float]] = {}  # key -> (price, timestamp)
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[float]:
        """Get price from cache if not expired."""
        if key in self._cache:
            price, cached_time = self._cache[key]
            if time.time() - cached_time < self._ttl:
                return price
        return None

    def set(self, key: str, price: float):
        """Set price in cache."""
        self._cache[key] = (price, time.time())

    def get_batch(self, keys: List[str]) -> Dict[str, float]:
        """Get multiple prices from cache."""
        result = {}
        for key in keys:
            price = self.get(key)
            if price is not None:
                result[key] = price
        return result

    def set_batch(self, prices: Dict[str, float]):
        """Set multiple prices in cache."""
        current_time = time.time()
        for key, price in prices.items():
            self._cache[key] = (price, current_time)


class RateLimiter:
    """Simple rate limiter for API calls."""

    def __init__(self, calls_per_second: float = 5.0):
        self._min_interval = 1.0 / calls_per_second
        self._last_call = 0.0

    def wait(self):
        """Wait if necessary to respect rate limit."""
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()


class PriceService:
    """
    Multi-source price fetching service.

    Fetches token prices with automatic fallback:
    1. DefiLlama (primary) - batch fetching supported
    2. GeckoTerminal (fallback) - for missing prices
    3. CoinGecko (fallback) - for historical prices (requires API key)
    """

    DEFILLAMA_BASE_URL = "https://coins.llama.fi/prices"
    GECKO_TERMINAL_BASE_URL = "https://api.geckoterminal.com/api/v2"
    COINGECKO_BASE_URL = "https://pro-api.coingecko.com/api/v3"

    # DefiLlama returns a `confidence` field in [0,1]. Anything below this is
    # treated as "no price" so downstream filters fail open (keep the token)
    # instead of computing a near-zero USD value. Without this, a low-confidence
    # stale price for a newly-listed token can silently drop it from a batch
    # for the entire 9-day cache TTL.
    DEFILLAMA_MIN_CONFIDENCE = 0.5

    def __init__(
        self,
        cache_ttl: int = 300,
        use_fallback: bool = True,
        coingecko_api_key: Optional[str] = None,
    ):
        """
        Initialize price service.

        Args:
            cache_ttl: Cache time-to-live in seconds (default 5 minutes)
            use_fallback: Whether to use fallback sources for missing prices
            coingecko_api_key: Optional CoinGecko API key for historical prices
        """
        self._cache = PriceCache(ttl_seconds=cache_ttl)
        self._use_fallback = use_fallback
        self._coingecko_api_key = coingecko_api_key or os.getenv("COINGECKO_API_KEY")

        # Rate limiters for each API
        self._defillama_limiter = RateLimiter(calls_per_second=5.0)
        self._gecko_limiter = RateLimiter(calls_per_second=0.5)  # 30/min
        self._coingecko_limiter = RateLimiter(calls_per_second=0.5)

    def get_token_prices(
        self,
        tokens: List[TokenIdentifier],
        use_fallback: Optional[bool] = None,
    ) -> Dict[str, float]:
        """
        Get current prices for multiple tokens.

        Sources, in order of priority:
            1. Data Hub (source of truth)
            2. DefiLlama
            3. GeckoTerminal (only if use_fallback)

        Args:
            tokens: List of TokenIdentifier objects
            use_fallback: Override default fallback behavior

        Returns:
            Dict mapping "network:address" -> price in USD
            Missing prices are not included in the result
        """
        if not tokens:
            return {}

        use_fallback = use_fallback if use_fallback is not None else self._use_fallback

        # Check cache first
        all_keys = [t.key for t in tokens]
        cached = self._cache.get_batch(all_keys)

        # Find tokens not in cache
        uncached_tokens = [t for t in tokens if t.key not in cached]

        if not uncached_tokens:
            return cached

        prices = dict(cached)

        # 1. Data Hub primary lookup
        hub_prices = self._fetch_data_hub_prices(uncached_tokens)
        if hub_prices:
            prices.update(hub_prices)
            self._cache.set_batch(hub_prices)
            uncached_tokens = [
                t for t in uncached_tokens if t.key not in hub_prices
            ]

        # 2. DefiLlama fallback
        if uncached_tokens:
            defillama_prices = self._fetch_defillama_prices(uncached_tokens)
            prices.update(defillama_prices)
            self._cache.set_batch(defillama_prices)

            # 3. GeckoTerminal fallback
            if use_fallback:
                missing_tokens = [
                    t for t in uncached_tokens if t.key not in prices
                ]
                if missing_tokens:
                    gecko_prices = self._fetch_gecko_terminal_prices(
                        missing_tokens
                    )
                    prices.update(gecko_prices)
                    self._cache.set_batch(gecko_prices)

        return prices

    def _fetch_data_hub_prices(
        self, tokens: List[TokenIdentifier]
    ) -> Dict[str, float]:
        """Fetch current prices from the Data Hub (source of truth)."""
        from shared.services.data_hub_service import (
            fetch_token_prices as _hub_prices,
        )

        prices: Dict[str, float] = {}
        chains: Dict[int, List[TokenIdentifier]] = {}
        for token in tokens:
            chains.setdefault(token.chain_id, []).append(token)

        for chain_id, chain_tokens in chains.items():
            try:
                hub_map = _hub_prices(chain_id)
            except Exception as exc:
                logger.warning(
                    f"Data hub fetch failed for chain {chain_id}: {exc}"
                )
                continue
            for token in chain_tokens:
                price = hub_map.get(token.address.lower())
                if price is not None and price > 0:
                    prices[token.key] = price

        return prices

    def get_historical_token_prices(
        self,
        tokens: List[TokenIdentifier],
        timestamp: int,
        use_fallback: Optional[bool] = None,
    ) -> Dict[str, float]:
        """
        Get historical prices for multiple tokens at a specific timestamp.

        Args:
            tokens: List of TokenIdentifier objects
            timestamp: Unix timestamp for historical price
            use_fallback: Override default fallback behavior

        Returns:
            Dict mapping "network:address" -> price in USD
        """
        if not tokens:
            return {}

        use_fallback = use_fallback if use_fallback is not None else self._use_fallback

        # For historical prices, we use a timestamp-specific cache key
        cache_suffix = f":{timestamp}"
        all_keys = [t.key + cache_suffix for t in tokens]
        cached_raw = self._cache.get_batch(all_keys)

        # Convert cache keys back to standard format
        cached = {k.replace(cache_suffix, ""): v for k, v in cached_raw.items()}

        uncached_tokens = [t for t in tokens if t.key not in cached]

        if not uncached_tokens:
            return cached

        # Fetch from DefiLlama (primary)
        prices = dict(cached)
        defillama_prices = self._fetch_defillama_historical_prices(
            uncached_tokens, timestamp
        )
        prices.update(defillama_prices)

        # Cache with timestamp suffix
        self._cache.set_batch({k + cache_suffix: v for k, v in defillama_prices.items()})

        # Fallback to CoinGecko for missing historical prices
        if use_fallback and self._coingecko_api_key:
            missing_tokens = [t for t in uncached_tokens if t.key not in prices]
            if missing_tokens:
                coingecko_prices = self._fetch_coingecko_historical_prices(
                    missing_tokens, timestamp
                )
                prices.update(coingecko_prices)
                self._cache.set_batch(
                    {k + cache_suffix: v for k, v in coingecko_prices.items()}
                )

        return prices

    def get_single_price(
        self,
        chain_id: int,
        address: str,
        timestamp: Optional[int] = None,
    ) -> Optional[float]:
        """
        Convenience method to get a single token price.

        Args:
            chain_id: Chain ID
            address: Token address
            timestamp: Optional timestamp for historical price

        Returns:
            Price in USD or None if not found
        """
        token = TokenIdentifier(chain_id=chain_id, address=address)
        if timestamp:
            prices = self.get_historical_token_prices([token], timestamp)
        else:
            prices = self.get_token_prices([token])
        return prices.get(token.key)

    def get_token_prices_with_decimals(
        self,
        tokens: List[TokenIdentifier],
        timestamp: Optional[int] = None,
    ) -> Dict[str, Dict[str, float]]:
        """
        Get prices for multiple tokens with decimals included.

        Sources, in order of priority:
            1. Data Hub (current prices only) — decimals resolved on-chain
            2. DefiLlama (returns price + decimals together)

        Args:
            tokens: List of TokenIdentifier objects
            timestamp: Optional timestamp for historical prices

        Returns:
            Dict mapping "network:address" -> {"price": float, "decimals": int}
            Missing tokens are not included
        """
        if not tokens:
            return {}

        results: Dict[str, Dict[str, float]] = {}
        remaining = tokens

        # 1. Data Hub for current prices. Skipped for historical lookups.
        if timestamp is None:
            from shared.services.data_hub_service import (
                fetch_token_prices as _hub_prices,
                get_token_decimals as _hub_decimals,
            )

            chains: Dict[int, List[TokenIdentifier]] = {}
            for token in tokens:
                chains.setdefault(token.chain_id, []).append(token)

            resolved_keys: set[str] = set()
            for chain_id, chain_tokens in chains.items():
                try:
                    hub_map = _hub_prices(chain_id)
                except Exception as exc:
                    logger.warning(
                        f"Data hub fetch failed for chain {chain_id}: {exc}"
                    )
                    continue
                for token in chain_tokens:
                    price = hub_map.get(token.address.lower())
                    if price is None or price <= 0:
                        continue
                    decimals = _hub_decimals(chain_id, token.address)
                    if decimals is None:
                        continue
                    results[token.key] = {
                        "price": float(price),
                        "decimals": int(decimals),
                    }
                    resolved_keys.add(token.key)

            remaining = [t for t in tokens if t.key not in resolved_keys]

        # 2. DefiLlama fallback (also handles historical lookups)
        if remaining:
            token_keys = [t.defillama_key for t in remaining]
            batch_size = 50
            for i in range(0, len(token_keys), batch_size):
                batch_keys = token_keys[i : i + batch_size]
                batch_tokens = remaining[i : i + batch_size]
                batch_results = self._fetch_defillama_batch_with_decimals(
                    batch_keys, batch_tokens, timestamp
                )
                results.update(batch_results)

        return results

    def _fetch_defillama_batch_with_decimals(
        self,
        token_keys: List[str],
        tokens: List[TokenIdentifier],
        timestamp: Optional[int] = None,
    ) -> Dict[str, Dict[str, float]]:
        """Fetch prices with decimals from DefiLlama."""
        coins_param = ",".join(token_keys)
        if timestamp:
            url = f"{self.DEFILLAMA_BASE_URL}/historical/{timestamp}/{coins_param}"
        else:
            url = f"{self.DEFILLAMA_BASE_URL}/current/{coins_param}"

        self._defillama_limiter.wait()

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()

            results: Dict[str, Dict[str, float]] = {}
            coins = data.get("coins", {})

            for token in tokens:
                coin_data = coins.get(token.defillama_key)
                if not coin_data or "price" not in coin_data:
                    continue
                if float(coin_data.get("confidence", 1)) < self.DEFILLAMA_MIN_CONFIDENCE:
                    logger.warning(
                        f"DefiLlama low-confidence price for {token.defillama_key} "
                        f"(confidence={coin_data.get('confidence')}); skipping"
                    )
                    continue
                results[token.key] = {
                    "price": float(coin_data["price"]),
                    "decimals": int(coin_data.get("decimals", 18)),
                }

            return results

        except requests.RequestException as e:
            logger.warning(f"DefiLlama API error: {e}")
            return {}
        except (KeyError, ValueError) as e:
            logger.warning(f"DefiLlama response parsing error: {e}")
            return {}

    # ==================== DefiLlama ====================

    def _fetch_defillama_prices(
        self, tokens: List[TokenIdentifier]
    ) -> Dict[str, float]:
        """Fetch current prices from DefiLlama."""
        if not tokens:
            return {}

        # Build comma-separated list of tokens
        token_keys = [t.defillama_key for t in tokens]

        # DefiLlama has URL length limits, batch if needed
        prices = {}
        batch_size = 50  # ~50 tokens per request to stay under URL limits

        for i in range(0, len(token_keys), batch_size):
            batch_keys = token_keys[i : i + batch_size]
            batch_tokens = tokens[i : i + batch_size]
            batch_prices = self._fetch_defillama_batch(batch_keys, batch_tokens)
            prices.update(batch_prices)

        return prices

    def _fetch_defillama_batch(
        self,
        token_keys: List[str],
        tokens: List[TokenIdentifier],
    ) -> Dict[str, float]:
        """Fetch a batch of prices from DefiLlama."""
        coins_param = ",".join(token_keys)
        url = f"{self.DEFILLAMA_BASE_URL}/current/{coins_param}"

        self._defillama_limiter.wait()

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()

            prices = {}
            coins = data.get("coins", {})

            for token in tokens:
                coin_data = coins.get(token.defillama_key)
                if not coin_data or "price" not in coin_data:
                    continue
                if float(coin_data.get("confidence", 1)) < self.DEFILLAMA_MIN_CONFIDENCE:
                    logger.warning(
                        f"DefiLlama low-confidence price for {token.defillama_key} "
                        f"(confidence={coin_data.get('confidence')}); skipping"
                    )
                    continue
                prices[token.key] = float(coin_data["price"])

            return prices

        except requests.RequestException as e:
            logger.warning(f"DefiLlama API error: {e}")
            return {}
        except (KeyError, ValueError) as e:
            logger.warning(f"DefiLlama response parsing error: {e}")
            return {}

    def _fetch_defillama_historical_prices(
        self,
        tokens: List[TokenIdentifier],
        timestamp: int,
    ) -> Dict[str, float]:
        """Fetch historical prices from DefiLlama."""
        if not tokens:
            return {}

        token_keys = [t.defillama_key for t in tokens]
        prices = {}
        batch_size = 50

        for i in range(0, len(token_keys), batch_size):
            batch_keys = token_keys[i : i + batch_size]
            batch_tokens = tokens[i : i + batch_size]
            batch_prices = self._fetch_defillama_historical_batch(
                batch_keys, batch_tokens, timestamp
            )
            prices.update(batch_prices)

        return prices

    def _fetch_defillama_historical_batch(
        self,
        token_keys: List[str],
        tokens: List[TokenIdentifier],
        timestamp: int,
    ) -> Dict[str, float]:
        """Fetch a batch of historical prices from DefiLlama."""
        coins_param = ",".join(token_keys)
        url = f"{self.DEFILLAMA_BASE_URL}/historical/{timestamp}/{coins_param}"

        self._defillama_limiter.wait()

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()

            prices = {}
            coins = data.get("coins", {})

            for token in tokens:
                coin_data = coins.get(token.defillama_key)
                if not coin_data or "price" not in coin_data:
                    continue
                if float(coin_data.get("confidence", 1)) < self.DEFILLAMA_MIN_CONFIDENCE:
                    logger.warning(
                        f"DefiLlama low-confidence historical price for "
                        f"{token.defillama_key} (confidence={coin_data.get('confidence')}); "
                        "skipping"
                    )
                    continue
                prices[token.key] = float(coin_data["price"])

            return prices

        except requests.RequestException as e:
            logger.warning(f"DefiLlama historical API error: {e}")
            return {}
        except (KeyError, ValueError) as e:
            logger.warning(f"DefiLlama historical response parsing error: {e}")
            return {}

    # ==================== GeckoTerminal ====================

    def _fetch_gecko_terminal_prices(
        self, tokens: List[TokenIdentifier]
    ) -> Dict[str, float]:
        """Fetch current prices from GeckoTerminal (fallback)."""
        prices = {}

        # Group tokens by network for efficient API calls
        tokens_by_network: Dict[str, List[TokenIdentifier]] = {}
        for token in tokens:
            network = token.gecko_network
            if network:
                if network not in tokens_by_network:
                    tokens_by_network[network] = []
                tokens_by_network[network].append(token)

        # Fetch prices for each network
        for network, network_tokens in tokens_by_network.items():
            # GeckoTerminal allows batching addresses
            addresses = [t.address for t in network_tokens]

            # Batch in groups of 30 (GeckoTerminal limit)
            batch_size = 30
            for i in range(0, len(addresses), batch_size):
                batch_addresses = addresses[i : i + batch_size]
                batch_tokens = network_tokens[i : i + batch_size]

                url = f"{self.GECKO_TERMINAL_BASE_URL}/simple/networks/{network}/token_price/{','.join(batch_addresses)}"

                self._gecko_limiter.wait()

                try:
                    response = requests.get(url, timeout=30)
                    response.raise_for_status()
                    data = response.json()

                    token_prices = (
                        data.get("data", {}).get("attributes", {}).get("token_prices", {})
                    )

                    for token in batch_tokens:
                        price_str = token_prices.get(token.address)
                        if price_str:
                            try:
                                prices[token.key] = float(price_str)
                            except ValueError:
                                pass

                except requests.RequestException as e:
                    logger.warning(f"GeckoTerminal API error for {network}: {e}")
                except (KeyError, ValueError) as e:
                    logger.warning(f"GeckoTerminal response parsing error: {e}")

        return prices

    # ==================== CoinGecko ====================

    def _fetch_coingecko_historical_prices(
        self,
        tokens: List[TokenIdentifier],
        timestamp: int,
    ) -> Dict[str, float]:
        """
        Fetch historical prices from CoinGecko (fallback).

        Note: CoinGecko requires contract address to coin ID mapping,
        which we don't have. This is a best-effort implementation
        that uses the /coins/{platform}/contract/{address} endpoint.
        """
        if not self._coingecko_api_key:
            return {}

        prices = {}
        headers = {"x-cg-pro-api-key": self._coingecko_api_key}

        # Map chain_id to CoinGecko platform
        chain_to_platform = {
            1: "ethereum",
            42161: "arbitrum-one",
            10: "optimistic-ethereum",
            8453: "base",
            56: "binance-smart-chain",
            137: "polygon-pos",
        }

        for token in tokens:
            platform = chain_to_platform.get(token.chain_id)
            if not platform:
                continue

            # First, get the coin ID from contract address
            url = f"{self.COINGECKO_BASE_URL}/coins/{platform}/contract/{token.address}"

            self._coingecko_limiter.wait()

            try:
                response = requests.get(url, headers=headers, timeout=30)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                coin_data = response.json()
                coin_id = coin_data.get("id")

                if not coin_id:
                    continue

                # Now get historical price
                # Convert timestamp to date string (DD-MM-YYYY)
                from datetime import datetime

                date_str = datetime.utcfromtimestamp(timestamp).strftime("%d-%m-%Y")
                history_url = f"{self.COINGECKO_BASE_URL}/coins/{coin_id}/history?date={date_str}"

                self._coingecko_limiter.wait()

                history_response = requests.get(
                    history_url, headers=headers, timeout=30
                )
                history_response.raise_for_status()
                history_data = history_response.json()

                price = (
                    history_data.get("market_data", {})
                    .get("current_price", {})
                    .get("usd")
                )
                if price:
                    prices[token.key] = float(price)

            except requests.RequestException as e:
                logger.warning(f"CoinGecko API error for {token.address}: {e}")
            except (KeyError, ValueError) as e:
                logger.warning(f"CoinGecko response parsing error: {e}")

        return prices


_price_service: Optional[PriceService] = None


def get_price_service() -> PriceService:
    """Get the global price service instance."""
    global _price_service
    if _price_service is None:
        _price_service = PriceService()
    return _price_service


def get_single_token_price(
    chain_id: int,
    address: str,
    timestamp: Optional[int] = None,
) -> Optional[float]:
    """
    Get price for a single token.

    Convenience function using global service instance.
    """
    return get_price_service().get_single_price(chain_id, address, timestamp)
