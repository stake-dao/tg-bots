import logging


import time


from dataclasses import dataclass


from typing import Dict, Optional


import requests


MORPHO_GRAPHQL_URL = "https://api.morpho.org/graphql"


CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass
class VaultDailyData:
    tvl_usd: float
    net_apy: float
    tvl_usd_yesterday: Optional[float]


class MorphoApiService:
    """GraphQL client for Morpho API with caching."""

    def __init__(self):
        self._cache: Dict[str, tuple[float, any]] = {}

    def _get_cached(self, key: str) -> Optional[any]:
        """Get cached value if not expired."""
        if key in self._cache:
            timestamp, value = self._cache[key]
            if time.time() - timestamp < CACHE_TTL_SECONDS:
                return value
            del self._cache[key]
        return None

    def _set_cached(self, key: str, value: any) -> None:
        """Cache a value with current timestamp."""
        self._cache[key] = (time.time(), value)

    def _query(
        self, query: str, variables: Optional[Dict] = None, retries: int = 3
    ) -> Optional[Dict]:
        """Execute GraphQL query with retries."""
        for attempt in range(1, retries + 1):
            try:
                response = requests.post(
                    MORPHO_GRAPHQL_URL,
                    json={"query": query, "variables": variables or {}},
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()

                if "errors" in data:
                    logging.warning(f"Morpho API GraphQL errors: {data['errors']}")
                    return None

                return data.get("data")
            except Exception as e:
                logging.warning(
                    f"Morpho API query failed (attempt {attempt}/{retries}): {e}"
                )
                if attempt < retries:
                    time.sleep(2 * attempt)
        return None

    def get_vault_apy(self, vault_address: str, chain_id: int = 1) -> Optional[float]:
        """
        Fetch vault APY from Morpho API.

        Args:
            vault_address: The vault contract address (0x...)
            chain_id: Chain ID (default 1 for Ethereum)

        Returns:
            avgNetApy as a float (0.05 = 5%), or None on failure
        """
        cache_key = f"vault_apy:{chain_id}:{vault_address.lower()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        query = """
        query VaultApy($address: String!, $chainId: Int!) {
            vaultV2ByAddress(address: $address, chainId: $chainId) {
                avgNetApy
            }
        }
        """

        data = self._query(query, {"address": vault_address, "chainId": chain_id})
        if not data:
            return None

        vault = data.get("vaultV2ByAddress")
        if not vault:
            return None

        apy = vault.get("avgNetApy")
        if apy is not None:
            self._set_cached(cache_key, apy)

        return apy

    def get_market_borrow_apy(
        self, market_id: str, chain_id: int = 1
    ) -> Optional[float]:
        """
        Fetch market borrow APY from Morpho API.

        Args:
            market_id: The market unique key (0x... hex string)
            chain_id: Chain ID (default 1 for Ethereum)

        Returns:
            borrowApy as a float (0.03 = 3%), or None on failure
        """
        cache_key = f"market_borrow_apy:{chain_id}:{market_id.lower()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        query = """
        query MarketBorrowApy($marketId: String!, $chainId: Int!) {
            marketById(marketId: $marketId, chainId: $chainId) {
                state {
                    borrowApy
                }
            }
        }
        """

        data = self._query(query, {"marketId": market_id, "chainId": chain_id})
        if not data:
            return None

        market = data.get("marketById")
        if not market:
            return None

        state = market.get("state")
        if not state:
            return None

        apy = state.get("borrowApy")
        if apy is not None:
            self._set_cached(cache_key, apy)

        return apy

    def get_markets_borrow_apy_batch(
        self, market_ids: list[str], chain_id: int = 1
    ) -> Dict[str, Optional[float]]:
        """
        Fetch borrow APY for multiple markets in a single query.

        Args:
            market_ids: List of market unique keys
            chain_id: Chain ID (default 1 for Ethereum)

        Returns:
            Dictionary mapping market_id to borrowApy (or None)
        """
        result: Dict[str, Optional[float]] = {}

        # Check cache first
        uncached_ids = []
        for market_id in market_ids:
            cache_key = f"market_borrow_apy:{chain_id}:{market_id.lower()}"
            cached = self._get_cached(cache_key)
            if cached is not None:
                result[market_id] = cached
            else:
                uncached_ids.append(market_id)

        if not uncached_ids:
            return result

        # Build batch query with aliases
        query_parts = []
        for i, market_id in enumerate(uncached_ids):
            query_parts.append(
                f'market{i}: marketById(marketId: "{market_id}", chainId: {chain_id}) {{'
                f"  state {{ borrowApy }}"
                f"}}"
            )

        query = "query {" + " ".join(query_parts) + "}"
        data = self._query(query)

        if not data:
            # Return None for all uncached markets
            for market_id in uncached_ids:
                result[market_id] = None
            return result

        # Parse results
        for i, market_id in enumerate(uncached_ids):
            market = data.get(f"market{i}")
            apy = None
            if market and market.get("state"):
                apy = market["state"].get("borrowApy")

            result[market_id] = apy

            # Cache the result
            if apy is not None:
                cache_key = f"market_borrow_apy:{chain_id}:{market_id.lower()}"
                self._set_cached(cache_key, apy)

        return result

    def get_vault_full_data(
        self, vault_address: str, chain_id: int = 1
    ) -> Optional[VaultDailyData]:
        """
        Fetch current TVL, current APY, and yesterday's TVL in one query.

        Args:
            vault_address: The vault contract address (0x...)
            chain_id: Chain ID (default 1 for Ethereum)

        Returns:
            VaultDailyData or None on failure
        """
        cache_key = f"vault_full_data:{chain_id}:{vault_address.lower()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        now = int(time.time())
        start_ts = now - 2 * 86400
        end_ts = now - 86400

        query = """
        query VaultDailyData($address: String!, $chainId: Int!, $startTs: Int!, $endTs: Int!) {
            vaultV2ByAddress(address: $address, chainId: $chainId) {
                totalAssetsUsd
                netApy
                historicalState {
                    totalAssetsUsd(options: { startTimestamp: $startTs, endTimestamp: $endTs, interval: DAY }) {
                        x
                        y
                    }
                }
            }
        }
        """

        data = self._query(
            query,
            {
                "address": vault_address,
                "chainId": chain_id,
                "startTs": start_ts,
                "endTs": end_ts,
            },
        )
        if not data:
            return None

        vault = data.get("vaultV2ByAddress")
        if not vault:
            return None

        tvl_usd = vault.get("totalAssetsUsd")
        net_apy = vault.get("netApy")

        if tvl_usd is None or net_apy is None:
            return None

        historical = vault.get("historicalState", {}).get("totalAssetsUsd", [])
        tvl_usd_yesterday = historical[-1]["y"] if historical else None

        result = VaultDailyData(
            tvl_usd=tvl_usd,
            net_apy=net_apy,
            tvl_usd_yesterday=tvl_usd_yesterday,
        )
        self._set_cached(cache_key, result)
        return result


_service: Optional[MorphoApiService] = None


def get_morpho_api_service() -> MorphoApiService:
    """Get or create the singleton MorphoApiService instance."""
    global _service
    if _service is None:
        _service = MorphoApiService()
    return _service
