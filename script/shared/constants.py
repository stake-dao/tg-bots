import os


from dotenv import load_dotenv


load_dotenv()


STAKE_ERPC_BASE = "https://erprc.contact-69d.workers.dev"


ALCHEMY_API_KEY = os.environ.get("WEB3_ALCHEMY_API_KEY", "")


ALCHEMY_BASE = "https://{network}.g.alchemy.com/v2"


def stake_rpc(chain_id_or_name: str | int) -> str:
    """Build Stake eRPC URL for a given chain ID or name."""
    return f"{STAKE_ERPC_BASE}/{chain_id_or_name}"


def alchemy_rpc(network: str) -> str:
    """Build Alchemy RPC URL for a given network (e.g., 'eth-mainnet', 'arb-mainnet')."""
    return f"{ALCHEMY_BASE.format(network=network)}/{ALCHEMY_API_KEY}"


ROUTEMESH_API_KEY = os.environ.get("ROUTEMESH_API_KEY", "")


ROUTEMESH_BASE = "https://lb.routeme.sh/rpc"


ROUTEMESH_CHAINS = {1, 10, 137, 8453, 42161}


def routemesh_rpc(chain_id: int) -> str:
    """Build the RouteMesh RPC URL for a chain id.

    Returns "" when unusable (no API key, or chain not covered) so the empty
    entry is dropped from the fallback list instead of producing a broken URL.
    """
    if not ROUTEMESH_API_KEY or chain_id not in ROUTEMESH_CHAINS:
        return ""
    return f"{ROUTEMESH_BASE}/{chain_id}/{ROUTEMESH_API_KEY}"


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class Common:
    TELEGRAM_API_KEY = os.environ.get("TELEGRAM_API_KEY", "")

    TELEGRAM_FEES_API_KEY = os.environ.get("TELEGRAM_FEES_API_KEY", "")

    TEST_TELEGRAM_API_KEY = os.environ.get("TEST_TELEGRAM_API_KEY", "")

    TEST_TELEGRAM_CHAT_ID = os.environ.get("TEST_TELEGRAM_CHAT_ID", "")

    TELEGRAM_ONLY_BOOST_API_KEY = os.environ.get("TELEGRAM_ONLY_BOOST_API_KEY", "")

    chains_ids_to_name = {
        1: "ethereum",
        42161: "arbitrum",
        10: "optimism",
        8453: "base",
        56: "bsc",
        137: "polygon",
        146: "sonic",
        252: "fraxtal",
    }

    chains_ids_to_abbreviation = {
        1: "eth",
        56: "bsc",
        42161: "arb",
        10: "opt",
        8453: "base",
    }

    DEPOSITORS_TRANSFER = {
        1: [
            {
                "depositor": "0x7F5c485D24fB1832A14f122C8722ef15C158Acb5",  # sdPendle V2,
                "token": "0x5Ea630e00D6eE438d3deA1556A110359ACdc10A9",  # sdPENDLE
                "base_token": "0x808507121B80c02388fAd14726482e061B8da827",  # PENDLE
            },
            {
                "depositor": "0xE0ffc03bb4086051090646a05ae4aF43843a5b51",  # Pre launch sdYND
                "token": "0x0a885027D84155387B9Bd47485B0fdec10C6B4EC",  # sdYND
                "base_token": "0x7159cc276D7d17Ab4b3bEb19959E1F39368a45Ba",  # YND
            },
            {
                "depositor": "0xFB9Aa699f1BaDb31A7C4B40F7Fa1f49469595785",  # sdYB
                "token": "0x0c057598dcE1891688829581f890DD2a3685a43f",  # sdYB
                "base_token": "0x01791F726B4103694969820be083196cC7c045fF",  # YB
            },
        ],
        8453: [
            {
                "depositor": "0x9A7B5505c91b1add06188C665B588D4CC5227F27",  # sdSpectra
                "token": "0x8e7801bAC71E92993f6924e7D767D7dbC5fCE0AE",  # SD_SPECTRA
                "base_token": "0x64FCC3A02eeEba05Ef701b7eed066c6ebD5d4E51",  # SPECTRA
            }
        ],
    }

    DEPOSITORS = {
        252: [
            "0x453369Ba22CEb6c3Eccd3029b911F590C3AD92d2",  # SDFXS_DEPOSITOR
        ],
        56: [
            "0x32ee46755AE81ce917392ed1fB21f74a8104515B",  # sdCAKE (CAKE_DEPOSITOR)
            "0xC5CCc20f6A4CD65fda979A2E292DBCF2C450C067",  # sdMAV
        ],
        1: [
            "0x88C88Aa6a9cedc2aff9b4cA6820292F39cc64026",  # sdCRV
            "0xf7F64f63ec693C6a3A79fCe4b222Bca2595cAcEf",  # sdPendle
            "0x7995192bE61EA0B28ce14183DDA51eDF78F1c7AB",  # sdFXN
            "0xf908C0281f4bAfbca67e490edae816B8472608C8",  # sdYFI
            "0x3e0d44542972859de3CAdaF856B1a4FD351B4D2E",  # sdBAL
            "0xFe928ca6a9C0cdf658a26A374b7373B9D6CefBCf",  # sdAPW
            "0xFaF3740167B866b571465B063c6B3A71Ba9b6285",  # sdFXS
            "0x56D27f6BA42Ec4C4E37dae0561e8E872ABb196Ad",  # sdFPIS
            "0x8A97e8B3389D431182aC67c0DF7D46FF8DCE7121",  # sdANGLE
            "0x219f7496fbD30e1F21A20613F9372d608A279993",  # sdBPT
            "0x177Eaa1A7c26da6Dc84c0cC3F9AE6Fd0A470E7Ec",  # sdMAV
        ],
        324: [
            "0x5b3b63E8313e7c68F88E4A12c86dC319ba75fC98",  # sdMAV
        ],
        8453: [
            "0xb30807324354379233FC6F7716C0510BbBD88487",  # sdMAV
        ],
    }

    ADDRESSES_TO_LABEL = {
        "0x12cAAe8Fc4ee86b8F774707eBC5fb7d760189418".lower(): "CONCENTRATOR"
    }


class Protocol:
    CURVE = "0xc715e373"

    BALANCER = "0xb774acb8"


class GlobalConstants:
    CHAIN_ID_TO_RPC = {
        1: alchemy_rpc("eth-mainnet"),                     # Ethereum
        56: stake_rpc(56),                   # BSC
        42161: alchemy_rpc("arb-mainnet"),   # Arbitrum (Alchemy for reliability)
        8453: alchemy_rpc("base-mainnet"),             # Base
        10: alchemy_rpc("opt-mainnet"),                   # Optimism
        137: "https://polygon.drpc.org",     # Polygon - not supported by worker yet
        59144: "https://rpc.linea.build",    # Linea - not supported by worker yet
        252: "https://rpc.frax.com",         # Fraxtal - official RPC with archive for eth_getProof
        324: "https://mainnet.era.zksync.io",  # zkSync - not supported by worker yet
        146: stake_rpc("sonic"),               # Sonic
        43111: "https://rpc.hemi.network/rpc",  # Hemi - not supported by worker yet
        43114: "https://avalanche.drpc.org", # Avalanche - not supported by worker yet
        999: "https://rpc.hyperliquid.xyz/evm",    # Hyperliquid - not supported by worker yet
        747474: "https://katana.drpc.org",   # Katana
        42793: stake_rpc(42793),             # Etherlink
        14: "https://rpc.au.cc/flare",       # Flare
        143: "https://infra.originstake.com/monad/evm",  # Monad
    }

    CHAIN_ID_TO_PUBLIC_RPC = {
        1: "https://rpc.mevblocker.io",
        56: stake_rpc(56),
        42161: stake_rpc(42161),
        8453: stake_rpc(8453),
        10: stake_rpc(10),
        137: "https://polygon-mainnet.public.blastapi.io",
        59144: "https://rpc.linea.build",
        252: "https://fraxtal-rpc.publicnode.com",
        324: "https://mainnet.era.zksync.io",
        146: stake_rpc("sonic"),
        43111: "https://hemi.drpc.org",
        43114: "https://avalanche.drpc.org",
        999: "https://rpc.hyperliquid.xyz/evm",
        747474: "https://katana.drpc.org",
        42793: stake_rpc(42793),  # Etherlink
        14: "https://rpc.au.cc/flare",  # Flare
        143: "https://rpc.monad.xyz",  # Monad - official public RPC
    }

    CHAIN_ID_TO_RPC_FALLBACKS = {
        1: [  # Ethereum
            "https://eth.drpc.org",
            "https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
            "https://rpc.ankr.com/eth",
            "https://1rpc.io/eth",
            "https://cloudflare-eth.com",
        ],
        10: [  # Optimism
            "https://mainnet.optimism.io",
            "https://optimism.drpc.org",
            "https://optimism-rpc.publicnode.com",
            "https://1rpc.io/op",
        ],
        56: [  # BSC
            "https://bsc-rpc.publicnode.com",
            "https://bsc-dataseed.bnbchain.org",
            "https://bsc.drpc.org",
            "https://binance.llamarpc.com",
            "https://1rpc.io/bnb",
        ],
        137: [  # Polygon
            "https://polygon.drpc.org",
            "https://polygon-rpc.com",
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon-mainnet.public.blastapi.io",
            "https://1rpc.io/matic",
        ],
        143: [  # Monad
            "https://rpc.monad.xyz",
            "https://monad-mainnet.drpc.org",
            "https://monad.gateway.tenderly.co",
            "https://143.rpc.thirdweb.com",
            "https://infra.originstake.com/monad/evm",
        ],
        146: [  # Sonic
            "https://rpc.soniclabs.com",
            "https://sonic.drpc.org",
        ],
        252: [  # Fraxtal
            "https://rpc.frax.com",
            "https://fraxtal-rpc.publicnode.com",
            "https://fraxtal.drpc.org",
        ],
        8453: [  # Base
            "https://mainnet.base.org",
            "https://base.drpc.org",
            "https://base-rpc.publicnode.com",
            "https://1rpc.io/base",
        ],
        42161: [  # Arbitrum
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum.drpc.org",
            "https://arbitrum-one-rpc.publicnode.com",
            "https://1rpc.io/arb",
        ],
        43114: [  # Avalanche
            "https://api.avax.network/ext/bc/C/rpc",
            "https://avalanche.drpc.org",
            "https://avalanche-c-chain-rpc.publicnode.com",
        ],
        59144: [  # Linea
            "https://rpc.linea.build",
            "https://linea.drpc.org",
            "https://1rpc.io/linea",
        ],
    }

    @staticmethod
    def rpc_endpoints(chain_id: int) -> list:
        """Ordered RPC preference for a chain, used to build the failover list.

        RouteMesh (primary) -> Alchemy/current primary -> public -> chainlist
        backups. Empty entries (missing key, chain not in a map) are dropped and
        the order is de-duplicated. Single source of truth for provider failover.
        """
        ordered = [
            routemesh_rpc(chain_id),
            GlobalConstants.CHAIN_ID_TO_RPC.get(chain_id, ""),
            GlobalConstants.CHAIN_ID_TO_PUBLIC_RPC.get(chain_id, ""),
            *GlobalConstants.CHAIN_ID_TO_RPC_FALLBACKS.get(chain_id, []),
        ]
        seen: set = set()
        result: list = []
        for url in ordered:
            if url and url not in seen:
                seen.add(url)
                result.append(url)
        return result

    TELEGRAM_PUBLIC_CHINOIS_CHANNEL_ID = "@StakeDaoAndsdCAKE"

    TELEGRAM_ACTIVITY_CHANNEL_ID = "@SDLiquidLockerBot"

    BOT_API_KEY = os.environ.get("BOT_API_KEY", "")

    BOT_VOTEMARKET_API_KEY = os.environ.get("BOT_VOTEMARKET_API_KEY", "")

    BOT_HARVESTS_API_KEY = os.environ.get("BOT_HARVESTS_API_KEY", "")

    VOTEMARKET_CHANNEL_ID = "@votemarket"

    VOTE_REMINDER_BOT_API_KEY = os.environ.get("VOTE_REMINDER_BOT_API_KEY", "")


class Bots:
    CURVE_API = "https://api.curve.finance"


class ContractRegistry:
    EXECUTOR = {
        1: "0x90569D8A1cF801709577B24dA526118f0C83Fc75",  # Ethereum
        56: "0xB0552b6860CE5C0202976Db056b5e3Cc4f9CC765",  # BSC (uses BOSS/Governance)
    }

    HOOK_INCENTIVE_L2 = "0x06Ab7052b00d038F8EeF33B267C23b5154cE8cDc"

    HOOK_INCENTIVE_L2_V2 = "0x68654D460fDF3231B49B25817cBBD72d8d291Fcf"

    HOOK_INCENTIVE_MERKLE = "0xD4898A378eA555595c4E7dbDE722B134a3F346D1"

    CURVE_VOTEMARKET_V2 = {
        1: None,
        10: [
            "0x5e5C922a5Eeab508486eB906ebE7bDFFB05D81e5",
            "0x8c2c5A295450DDFf4CB360cA73FCCC12243D14D9",
        ],
        42161: [
            "0x5e5C922a5Eeab508486eB906ebE7bDFFB05D81e5",
            "0x8c2c5A295450DDFf4CB360cA73FCCC12243D14D9",
        ],
        8453: [
            "0x5e5C922a5Eeab508486eB906ebE7bDFFB05D81e5",
            "0x8c2c5A295450DDFf4CB360cA73FCCC12243D14D9",
        ],
    }

    BALANCER_VOTEMARKET_V2 = {
        1: None,
        42161: ["0xDD2FaD5606cD8ec0c3b93Eb4F9849572b598F4c7"],
        8453: ["0xDD2FaD5606cD8ec0c3b93Eb4F9849572b598F4c7"],
        10: ["0xDD2FaD5606cD8ec0c3b93Eb4F9849572b598F4c7"],
        137: ["0xDD2FaD5606cD8ec0c3b93Eb4F9849572b598F4c7"],
    }

    FXN_VOTEMARKET_V2 = {
        1: None,
        10: ["0x155a7Cf21F8853c135BdeBa27FEA19674C65F2b4"],
        42161: ["0x155a7Cf21F8853c135BdeBa27FEA19674C65F2b4"],
        8453: ["0x155a7Cf21F8853c135BdeBa27FEA19674C65F2b4"],
    }

    PENDLE_VOTEMARKET_V2 = {
        1: None,
        42161: ["0x105694FC5204787eD571842671d1262A54a8135B"],
        8453: ["0x105694FC5204787eD571842671d1262A54a8135B"],
        10: ["0x105694FC5204787eD571842671d1262A54a8135B"],
        137: ["0x105694FC5204787eD571842671d1262A54a8135B"],
    }

    YB_VOTEMARKET_V2 = {
        1: None,
        42161: ["0x9Babb77562AeBDD19930b5bd9396B06636f6dDd6"],
        8453: ["0x9Babb77562AeBDD19930b5bd9396B06636f6dDd6"],
        10: ["0x9Babb77562AeBDD19930b5bd9396B06636f6dDd6"],
        137: ["0x9Babb77562AeBDD19930b5bd9396B06636f6dDd6"],
    }

    SD_SPECTRA_NFT_ID = 1263

    DELEGATION_ONCHAIN = {1: "0xbB06fEFB8f23A7c60C93fe20464DB6687C51955f"}

    PENDLE_LOCKER = {1: "0xD8fa8dC5aDeC503AcC5e026a98F32Ca5C1Fa289A"}

    BALANCER_LOCKER = {1: "0xea79d1A83Da6DB43a85942767C389fE0ACf336A5"}

    TETU_LOCKER = {1: "0x9cC56Fa7734DA21aC88F6a816aF10C5b898596Ce"}

    CRV_LL = {1: "0x52f541764E6e90eeBc5c21Ff570De0e2D63766B6"}

    FXN_LOCKER = {1: "0x75736518075a01034fa72D675D36a47e9B06B2Fb"}

    FXN_CONVEX_LL = {1: "0xd11a4Ee017cA0BECA8FA45fF2abFe9C6267b7881"}

    YB_LOCKER = {1: "0x0070D9adC687a28FBAcC0a0Aab24B90c037AD24e"}

    CVX = {1: "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b"}

    SDT = {1: "0x73968b9a57c6E53d41345FD57a6E6ae27d6CDB2F"}

    sdYND = {1: "0x0a885027D84155387B9Bd47485B0fdec10C6B4EC"}

    sdCRV_POOL = {1: "0xca0253a98d16e9c1e3614cafda19318ee69772d0"}

    sdFXS_POOL = {1: "0x71c91b173984d3955f7756914bbf9a7332538595"}

    sdPENDLE_POOL = {1: "0x26f3f26f46cbee59d1f8860865e13aa39e36a8c0"}

    sdYFI_POOL = {1: "0x852b90239c5034b5bb7a5e54ef1bef3ce3359cc8"}

    sdFXN_POOL = {1: "0x28ca243dc0ac075dd012fcf9375c25d18a844d96"}

    sdBAL_POOL = {1: "0xBA12222222228d8Ba445958a75a0704d566BF2C8"}

    sdFXS_CURVE_POOL = {252: "0x3df1658e3e76d14ef3c4d410829ea7575bb83b9b"}

    SD_SPECTRA_POOL = {8453: "0x02D55aF4813a3a6826Ef185935E4FC1dEfA45FB0"}

    MORPHO_BLUE = {1: "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"}

    STAKEDAO_MORPHO_USDC_V1 = {1: "0x13AA4f80AD5F06cE4f1A3a3cA58C37059F0EE4c5"}

    STAKEDAO_MORPHO_USDC_V2 = {1: "0x8EDCC305E633d29BFB383872e79401c506cE9E6f"}

    STAKEDAO_MORPHO_FRXUSD_V1 = {1: "0xB2376cC88EA47C80fFC3De60dAe8F2F48BC872a3"}

    STAKEDAO_MORPHO_FRXUSD_V2 = {1: "0xCE13e39534082FCF8f13F6D84e6D95414D14271e"}

    veSDT = {1: "0x0C30476f66034E11782938DF8e4384970B6c9e8a"}

    VLSDT = {1: "0x94818A7baa7e9F5dC62ce4da1B52ef9a760b80B8"}

    CONVEX_VOTER = {1: "0x989AEb4d175e16225E39E87d0D97A3360524AD80"}

    CONVEX_CVX_LOCKER = {1: "0x72a19342e8F1838460eBFCCEf09F6585e32db86E"}

    CONVEX_DAO_DELEGATION = {1: "0x22697721EC1C14e42305e91BF02672932E8a7B09"}

    CONVEX_GAUGE_DELEGATION = {1: "0xb8270eef1319173dE9f5033FED442F638ff1607d"}

    YEARN_VOTER = {1: "0xF147b8125d2ef93FB6965Db97D6746952a133934"}

    CAKE_DEPOSITOR = {56: "0x32ee46755AE81ce917392ed1fB21f74a8104515B"}

    sdCAKE_CAKE_V3 = {56: "0x8A876Ca851063e0252654CA6368a5B2280f51c32"}

    sdCAKE_CAKE_STABLE = {56: "0xb8204D31379A9B317CD61C833406C972F58ecCbC"}

    LLAMALEND_ASDCRV = {42161: "0x83B85f3b08B5EE58dE9EF9604e7Eec087FCCf130"}

    @staticmethod
    def get_address(contract_name: str, chain_id: int) -> str:
        # Force convert chain_id to int
        chain_id = int(chain_id)
        """Get contract address for specified chain"""
        addresses = getattr(ContractRegistry, contract_name, None)
        if not addresses:
            raise ValueError(f"Contract {contract_name} not found")
        address = addresses.get(chain_id)
        if not address:
            raise ValueError(
                f"Contract {contract_name} not deployed on chain {chain_id}"
            )
        return address
