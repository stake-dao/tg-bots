from dataclasses import dataclass


from typing import Dict, Optional


CHAIN_ID_TO_NETWORK: Dict[int, str] = {
    1: "ethereum",
    42161: "arbitrum",
    10: "optimism",
    8453: "base",
    56: "bsc",
    137: "polygon",
    146: "sonic",
    252: "fraxtal",
    43114: "avax",
    250: "fantom",
}


CHAIN_ID_TO_GECKO_NETWORK: Dict[int, str] = {
    1: "eth",
    42161: "arbitrum",
    10: "optimism",
    8453: "base",
    56: "bsc",
    137: "polygon_pos",
    43114: "avax",
    250: "ftm",
}


@dataclass
class TokenIdentifier:
    """Identifies a token by chain and address."""

    chain_id: int
    address: str

    def __post_init__(self):
        self.address = self.address.lower()

    @property
    def network(self) -> str:
        """Get DefiLlama network name."""
        return CHAIN_ID_TO_NETWORK.get(self.chain_id, "ethereum")

    @property
    def gecko_network(self) -> Optional[str]:
        """Get GeckoTerminal network name."""
        return CHAIN_ID_TO_GECKO_NETWORK.get(self.chain_id)

    @property
    def key(self) -> str:
        """Returns 'network:address' format used as dict key."""
        return f"{self.network}:{self.address}"

    @property
    def defillama_key(self) -> str:
        """Returns the key format used by DefiLlama API."""
        return f"{self.network}:{self.address}"
