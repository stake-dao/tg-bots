CHAIN_NAMES = {
    1: "Ethereum",
    10: "Optimism",
    56: "BSC",
    137: "Polygon",
    146: "Sonic",
    252: "Fraxtal",
    8453: "Base",
    42161: "Arbitrum",
    42793: "Etherlink",
}


L2_CHAIN_IDS = {42161, 137, 8453, 10}


CHAIN_EXPLORERS = {
    1: "https://etherscan.io",                    # Ethereum Mainnet
    3: "https://ropsten.etherscan.io",            # Ropsten (deprecated)
    4: "https://rinkeby.etherscan.io",            # Rinkeby (deprecated)
    5: "https://goerli.etherscan.io",             # Goerli Testnet
    11155111: "https://sepolia.etherscan.io",     # Sepolia Testnet
    10: "https://optimistic.etherscan.io",        # Optimism
    56: "https://bscscan.com",                    # BNB Chain Mainnet
    97: "https://testnet.bscscan.com",            # BNB Testnet
    137: "https://polygonscan.com",               # Polygon Mainnet
    80001: "https://mumbai.polygonscan.com",      # Polygon Testnet (Mumbai)
    42161: "https://arbiscan.io",                 # Arbitrum One
    421613: "https://goerli.arbiscan.io",         # Arbitrum Goerli
    43114: "https://snowtrace.io",                # Avalanche C-Chain
    43113: "https://testnet.snowtrace.io",        # Avalanche Fuji Testnet
    250: "https://ftmscan.com",                   # Fantom Opera
    4002: "https://testnet.ftmscan.com",          # Fantom Testnet
    128: "https://hecoinfo.com",                  # HECO Mainnet
    66: "https://explorer.oasischain.io",         # OKExChain
    25: "https://cronoscan.com",                  # Cronos Mainnet
    338: "https://testnet.cronoscan.com",         # Cronos Testnet
    100: "https://gnosisscan.io",                 # Gnosis (xDai)
    321: "https://explorer.kcc.io/en",            # KuCoin Chain
    8453: "https://basescan.org",                 # Base Mainnet
    84531: "https://goerli.basescan.org",         # Base Goerli
    2222: "https://explorer.velas.com",           # Velas Mainnet
    122: "https://fusescan.com",                  # Fuse
    199: "https://bttcscan.com",                  # BitTorrent Chain
    9001: "https://evm.explorer.celo.org",        # Celo Mainnet
    44787: "https://alfajores-blockscout.celo-testnet.org",  # Celo Testnet
    1284: "https://moonbeam.moonscan.io",         # Moonbeam
    1285: "https://moonriver.moonscan.io",        # Moonriver
    1287: "https://moonbase.moonscan.io",         # Moonbase Alpha
    252: "https://fraxscan.com",
    146: "https://sonicscan.org",
    42220: "https://celoscan.io",
    42793: "https://explorer.etherlink.com",          # Etherlink
}


def get_explorer_link(chainId):
    return CHAIN_EXPLORERS.get(chainId, "")
