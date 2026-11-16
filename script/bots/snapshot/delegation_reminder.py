import asyncio
import html
import logging
import threading
import time
import requests
from typing import List, Dict, Any, Optional
from urllib.parse import quote, urlparse

from gql import Client, gql
from gql.transport.aiohttp import AIOHTTPTransport

from dotenv import load_dotenv
from w3multicall.multicall import W3Multicall
from web3 import Web3

from shared.communication.telegram import send_telegram_message
from shared.constants import ContractRegistry, GlobalConstants
from shared.services.web3_service import FallbackHTTPProvider
from datetime import datetime, timezone
from shared.utils.globals import (
    get_redis_client,
    load_contract_w3,
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.ERROR)

# Constants
SNAPSHOT_GRAPHQL_URL = "https://hub.snapshot.org/graphql"
CONCURRENCY_LIMIT = 5 

# Configuration for different alert windows (Reminders)
ALERT_CONFIGS = [
    {
        "hours": 24,
        "emoji": "🟠",
        "label": "24h reminder",
        "offset_seconds": 24 * 3600
    },
    {
        "hours": 2,
        "emoji": "🔴",
        "label": "2h reminder",
        "offset_seconds": 2 * 3600
    }
]

def format_utc_timestamp(timestamp: float) -> str:
    """
    Converts a Unix timestamp to a formatted UTC string.
    Format: YYYY-MM-DD HH:MM:SS UTC
    """
    dt_object = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt_object.strftime("%Y-%m-%d %H:%M:%S UTC")

# Convex on-chain voting platforms (https://www.convexfinance.com/vote) — the
# vlCVX votes that used to live only on Snapshot cvx.eth. The Stake DAO
# delegate votes on-chain (manually for now), so this bot is the safety net
# against a missed deadline. Snapshot cvx.eth stays watched in parallel:
# Convex still mirrors proposals there during the shadow-run, and once they
# stop the space simply returns nothing while the on-chain watcher keeps
# covering.
#
# kind="gauge": weight-allocation votes, at most one active proposal (the
#   contract requires the previous one to be over), getVote -> voted index 2.
# kind="dao": yes/no governance votes, proposals CAN overlap and startTime
#   may be scheduled far in the future, getVote -> voted index 0.
#
# "path" is the CONVEX_VOTE_URL route of the platform's voting page.
# DAO proposal pages live at <path>/<pid> (the frontend ignores ?id=<pid>,
# which lands on the proposal LIST — verified 2026-08-19); gauge pages have
# no per-proposal page (a single active proposal, shown by default). An
# unknown path 308-redirects to /vote, so a frontend rename degrades to the
# generic page instead of a dead link.
CONVEX_ONCHAIN_PLATFORMS = [
    {
        "key": "convex-curve",
        "kind": "gauge",
        "name": "Convex Curve (on-chain)",
        "address": ContractRegistry.CONVEX_CURVE_GAUGE_VOTING[1],
        "abi": "convex_gauge_voting",
        "path": "weights/curve",
    },
    {
        "key": "convex-fxn",
        "kind": "gauge",
        "name": "Convex FXN (on-chain)",
        "address": ContractRegistry.CONVEX_FX_GAUGE_VOTING[1],
        "abi": "convex_gauge_voting",
        "path": "weights/fx",
    },
    {
        "key": "convex-curve-dao",
        "kind": "dao",
        "proto": "Curve",
        "name": "Convex Curve DAO (on-chain)",
        "address": ContractRegistry.CONVEX_CURVE_DAO_VOTING[1],
        "abi": "convex_dao_voting",
        "path": "dao/curve",
    },
    {
        "key": "convex-fxn-dao",
        "kind": "dao",
        "proto": "f(x)",
        "name": "Convex f(x) DAO (on-chain)",
        "address": ContractRegistry.CONVEX_FX_DAO_VOTING[1],
        "abi": "convex_dao_voting",
        "path": "dao/fx",
    },
    {
        "key": "convex-frax-dao",
        "kind": "dao",
        "proto": "Frax",
        "name": "Convex Frax DAO (on-chain)",
        "address": ContractRegistry.CONVEX_FRAX_DAO_VOTING[1],
        "abi": "convex_dao_voting",
        "path": "dao/frax",
    },
    {
        "key": "convex-resupply-dao",
        "kind": "dao",
        "proto": "Resupply",
        "name": "Convex Resupply DAO (on-chain)",
        "address": ContractRegistry.CONVEX_RESUPPLY_DAO_VOTING[1],
        "abi": "convex_dao_voting",
        "path": "dao/resupply",
    },
    {
        "key": "convex-dao",
        "kind": "dao",
        "proto": "Convex",
        "name": "Convex DAO (on-chain)",
        "address": ContractRegistry.CONVEX_GOVERNANCE_DAO_VOTING[1],
        "abi": "convex_dao_voting",
        "path": "dao/convex",
    },
]

# DaoVotePlatform.VoteType
DAO_VOTE_TYPES = {0: "Ownership", 1: "Parameter"}

# Convex voting frontend, root of every on-chain proposal link
CONVEX_VOTE_URL = "https://www.convexfinance.com/vote"

# The on-chain delegate is a Safe, so the vote can only be cast from inside
# it. app.safe.global/apps/open loads a Safe App in the Safe's context: the
# reminder lands on the proposal page already connected as the delegate,
# instead of on a Convex page the signer then has to reach by hand.
SAFE_APP_OPEN_URL = "https://app.safe.global/apps/open"
SAFE_CHAIN_PREFIX = "eth"

def _safe_app_link(app_url: str) -> str:
    """Wraps a Convex vote page into the delegate Safe's Safe App opener."""
    # appUrl is fully percent-encoded: any raw "?"/"&" in the wrapped page
    # URL would be parsed as query params of app.safe.global instead of
    # reaching the Convex app inside the Safe iframe.
    return (
        f"{SAFE_APP_OPEN_URL}"
        f"?safe={SAFE_CHAIN_PREFIX}:{ContractRegistry.DELEGATION_ONCHAIN[1]}"
        f"&appUrl={quote(app_url, safe='')}"
    )

# A DAO proposal lasts at most 6 days (MAX_PROPOSAL_DURATION); anything that
# ended earlier than this margin can never re-enter an alert window.
DAO_SETTLED_MARGIN = 7 * 86400

# proposals(pid) reads batched per multicall3 aggregate
DAO_MULTICALL_CHUNK = 200

# Per-reminder idempotence keys: only set after Telegram confirms delivery,
# so a partially-failed run can be retried without re-sending what went out.
ONCHAIN_SENT_PREFIX = "delegation_reminder_onchain_sent"
ONCHAIN_SENT_TTL = 30 * 86400

# Bounded retry on the platform reads. Provider hiccups (-32603, timeouts)
# come in bursts: on 2026-08-05 two consecutive polls read 0/7 platforms and
# the 2h reminder of a DAO proposal ending at 17:58 UTC only went out at
# 16:49 UTC — 1h10 of margin instead of ~2h. Retrying is per platform, so a
# permanently broken one costs the healthy ones a few seconds, never their
# reminder. Worst case with every platform down: 7 * (1s + 2s) of backoff.
ONCHAIN_READ_ATTEMPTS = 3
ONCHAIN_READ_BACKOFF_SECONDS = 1.0

class _EndpointRotation:
    """
    Sticky RPC endpoint pointer shared by every on-chain read of this bot.

    ``FallbackHTTPProvider`` only rotates on TRANSPORT failures (HTTP 429/5xx,
    connection drop, timeout). A node that answers HTTP 200 carrying
    ``{'code': -32603, 'message': 'Internal error'}`` is a successful request
    as far as the provider is concerned, so the connection stays pinned to it
    for the whole run — that is how 2026-08-05 produced two consecutive polls
    with 0/7 platforms read. Rotating here rather than in
    ``web3_service.FallbackHTTPProvider`` keeps the behaviour change scoped to
    this bot; the executor jobs keep their current provider semantics.

    The pointer is sticky across platforms and vote checks of the same run:
    once one read proves an endpoint answers, the rest of the poll reuses it
    instead of rediscovering the broken one. Each Web3 is built on the full
    endpoint list, rotated, so transport-level failover still applies behind
    the current head.
    """

    def __init__(self, chain_id: int = 1, endpoints: Optional[List[str]] = None):
        self._endpoints = [
            url for url in (endpoints or GlobalConstants.rpc_endpoints(chain_id)) if url
        ]
        self._lock = threading.Lock()
        self._index = 0
        # One Web3 per rotation offset: a poll that fails everywhere must not
        # build (and cache a session for) the same endpoint list 20 times.
        self._by_offset: Dict[int, Web3] = {}

    def _offset(self) -> int:
        return self._index % len(self._endpoints)

    def web3(self) -> Web3:
        with self._lock:
            offset = self._offset()
            if offset not in self._by_offset:
                rotated = self._endpoints[offset:] + self._endpoints[:offset]
                self._by_offset[offset] = Web3(FallbackHTTPProvider(rotated))
            return self._by_offset[offset]

    def rotate(self) -> None:
        """Promotes the next endpoint to head after a failed read."""
        with self._lock:
            self._index += 1

    def host(self) -> str:
        """
        Host actually serving the requests — never the full URL, which
        carries API keys. Read from the live provider rather than from our
        own index: FallbackHTTPProvider advances on its own after a
        transport failure (e.g. the keyed Alchemy URL 401ing when the job
        runs without RPC secrets), so the head we picked is not necessarily
        the node that answered.
        """
        # No lock here: web3() takes it, and threading.Lock is not reentrant.
        return urlparse(self.web3().provider.endpoint_uri).netloc

# Process-wide: the script is a one-shot poll, so the rotation state lives
# exactly as long as one run.
_ONCHAIN_RPC = _EndpointRotation()

def _select_dao_proposals(platform: Dict[str, Any], raw_proposals, now: int) -> List[Dict[str, Any]]:
    """
    Filters raw (pid, (startTime, endTime, epoch, voteType, externalId))
    entries of one DaoVotePlatform into Snapshot-shaped proposal dicts.

    The caller must pass ALL pids of the platform: proposal creation is not
    chronologically ordered (startTime can be scheduled far in the future),
    so any "stop at the first old pid" heuristic could skip a still-active
    early proposal.
    """
    selected = []
    for pid, (start_time, end_time, _epoch, vote_type, external_id) in raw_proposals:
        if end_time == 0:
            # Force-ended: timestamps are zeroed, nothing to alert on
            continue
        if end_time < now - DAO_SETTLED_MARGIN:
            continue
        vote_type_label = DAO_VOTE_TYPES.get(int(vote_type), f"Type {vote_type}")
        # Mirror the Snapshot naming ("[Curve] Ownership DAO Vote ID: 1454")
        # so the Telegram channel stays consistent across both sources
        title = f"[{platform['proto']}] {vote_type_label} DAO Vote"
        if external_id:
            title += f" ID: {external_id}"
        title += f" (on-chain #{pid})"
        selected.append(
            {
                "id": f"{platform['key']}-{pid}",
                "pid": pid,
                "platform_address": platform["address"],
                "platform_kind": "dao",
                "title": title,
                "start": int(start_time),
                "end": int(end_time),
                "space": {"id": platform["key"], "name": platform["name"]},
                "link": f"{CONVEX_VOTE_URL}/{platform['path']}/{pid}",
            }
        )
    return selected

def _is_in_reminder_window(end: int, last_run: int, now: int, offset: int) -> bool:
    """
    Half-open lower bound: a proposal ending exactly at now+offset is
    selected by THIS run; the next run (last_run == now) must skip it.
    """
    return end > now and (last_run + offset) < end <= (now + offset)

def _fetch_dao_platform_proposals_sync(w3, platform: Dict[str, Any], count: int, now: int) -> List[Dict[str, Any]]:
    """Reads ALL proposals of one DaoVotePlatform, batched via multicall3."""
    raw = []
    for chunk_start in range(0, count, DAO_MULTICALL_CHUNK):
        pids = range(chunk_start, min(chunk_start + DAO_MULTICALL_CHUNK, count))
        multicall = W3Multicall(w3)
        for pid in pids:
            multicall.add(
                W3Multicall.Call(
                    platform["address"],
                    "proposals(uint256)(uint48,uint48,uint48,uint8,uint104)",
                    [pid],
                )
            )
        raw.extend(zip(pids, multicall.call()))
    return _select_dao_proposals(platform, raw, now)

def _has_contract_code(w3, address: str) -> bool:
    """Whether the endpoint actually knows the contract at ``address``."""
    return bool(w3.eth.get_code(address))

def _read_platform_proposals_sync(w3, platform: Dict[str, Any], now: int) -> List[Dict[str, Any]]:
    """
    Single read of one platform. Gauge platforms only need the latest
    proposal (the contract requires the previous one to be over before
    creating a new one); DAO platforms are read in full because proposals
    can overlap and be scheduled out of order.
    """
    contract = load_contract_w3(w3, platform["address"], platform["abi"])
    count = contract.functions.proposalCount().call()
    if count == 0 and not _has_contract_code(w3, platform["address"]):
        # An endpoint serving state where this contract does not exist answers
        # 0 instead of failing (verified against a node on another chain: the
        # eth_call returns 0, no exception). Trusted as a legitimate empty
        # platform, that zero advances the watermark and burns the reminder
        # windows of the run in complete silence — a definitive miss, worse
        # than the -32603 burst this rotation handles. So a zero is only
        # believed when the code is actually there.
        raise ValueError(
            f"no contract code at {platform['address']} — stale or wrong-chain endpoint"
        )
    if count == 0:
        return []
    if platform["kind"] != "gauge":
        return _fetch_dao_platform_proposals_sync(w3, platform, count, now)
    pid = count - 1
    start_time, end_time, _epoch = contract.functions.proposals(pid).call()
    return [
        {
            "id": f"{platform['key']}-{pid}",
            "pid": pid,
            "platform_address": platform["address"],
            "platform_kind": "gauge",
            "title": f"{platform['name']} gauge weights vote #{pid}",
            "start": int(start_time),
            "end": int(end_time),
            "space": {"id": platform["key"], "name": platform["name"]},
            "link": f"{CONVEX_VOTE_URL}/{platform['path']}",
        }
    ]

def _read_platform_proposals_with_retry(platform: Dict[str, Any], now: int) -> List[Dict[str, Any]]:
    """
    Reads one platform, retrying transient RPC failures on the NEXT endpoint
    with an exponential backoff. Re-raises the last error once the attempts
    are exhausted, so a permanently broken platform still reports its failure
    to the caller.
    """
    last_error = None
    for attempt in range(ONCHAIN_READ_ATTEMPTS):
        try:
            return _read_platform_proposals_sync(_ONCHAIN_RPC.web3(), platform, now)
        except Exception as error:
            last_error = error
            logging.error(
                f"Convex platform {platform['key']} read failed on "
                f"{_ONCHAIN_RPC.host()} "
                f"(attempt {attempt + 1}/{ONCHAIN_READ_ATTEMPTS}): {error}"
            )
            # Rotate on the last attempt too: the next platform must not
            # start on the endpoint that just failed three times.
            _ONCHAIN_RPC.rotate()
            if attempt + 1 < ONCHAIN_READ_ATTEMPTS:
                time.sleep(ONCHAIN_READ_BACKOFF_SECONDS * (2 ** attempt))
    raise last_error

def _fetch_convex_onchain_proposals_sync() -> tuple:
    """
    Reads the proposals of every Convex on-chain platform and returns
    (proposals, all_ok): Snapshot-shaped proposal dicts flowing through the
    same alert pipeline, plus a completeness flag.

    Failures are isolated PER PLATFORM: one broken contract/read must not
    block the reminders of the healthy platforms. all_ok=False tells the
    caller to keep the on-chain watermark frozen so the failed platform's
    windows are retried next run (the healthy platforms' alerts are
    protected from re-sending by Redis dedup/idempotence keys).
    """
    proposals = []
    all_ok = True
    now = int(time.time())

    for platform in CONVEX_ONCHAIN_PLATFORMS:
        try:
            proposals.extend(_read_platform_proposals_with_retry(platform, now))
        except Exception as e:
            logging.error(f"Error reading Convex platform {platform['key']}: {e}")
            all_ok = False
    return proposals, all_ok

async def fetch_convex_onchain_proposals() -> tuple:
    return await asyncio.to_thread(_fetch_convex_onchain_proposals_sync)

def _has_delegate_voted_onchain_sync(proposal: Dict[str, Any]) -> bool:
    """
    Gauge getVote returns (gauges, weights, voted, baseWeight,
    adjustedWeight) -> voted at index 2; DAO getVote returns (voted,
    yesWeight, noWeight, baseWeight, adjustedWeight) -> voted at index 0.
    Both count votes cast via surrogate (voteStatus > 0), and a re-vote
    overwrites the previous one, so `voted` is the final state.
    """
    is_dao = proposal["platform_kind"] == "dao"
    abi = "convex_dao_voting" if is_dao else "convex_gauge_voting"
    voted_index = 0 if is_dao else 2
    # Same rotation as the platform reads: this check runs right after them,
    # so it inherits whichever endpoint the poll found to be answering.
    w3 = _ONCHAIN_RPC.web3()
    contract = load_contract_w3(w3, proposal["platform_address"], abi)
    vote = contract.functions.getVote(
        proposal["pid"], ContractRegistry.DELEGATION_ONCHAIN[1]
    ).call()
    return bool(vote[voted_index])

async def get_snapshot_spaces() -> List[str]:
    """
    Fetches the list of spaces from the Stake DAO API.
    """
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, lambda: requests.get(
            "https://raw.githubusercontent.com/stake-dao/api/refs/heads/main/api/lockers/index.json"
        ))
        
        if response.status_code != 200:
            logging.error(f"Failed to fetch spaces: {response.status_code}")
            return []

        lockers = response.json()
        # cvx.eth stays on purpose during the on-chain shadow-run: Convex
        # still mirrors its proposals on Snapshot, and once they stop the
        # space simply returns nothing while CONVEX_ONCHAIN_PLATFORMS keeps
        # covering — no cutover action needed for this bot.
        spaces = ["cvx.eth", "aurafinance.eth", "gauges.aurafinance.eth"]

        for locker in lockers.get("parsed", []):
            if "delegation" in locker:
                spaces.extend(locker["delegation"])

        return list(set(spaces))
    except Exception as e:
        logging.error(f"Error fetching spaces: {e}")
        return []

async def fetch_ending_proposals(session, space: str, start_threshold: int, end_timestamp: int) -> List[Dict[str, Any]]:
    """
    Fetches proposals that are ending within a specific time window.
    Used for 24h and 2h reminders.
    """
    query = gql("""
        query Proposals ($space: String!, $start_threshold: Int!, $end: Int!) {
            proposals (
                first: 100,
                where: {
                    space: $space,
                    end_gte: $start_threshold,
                    end_lte: $end,
                    state: "active"
                },
                orderBy: "created",
                orderDirection: desc
            ) {
                id
                title
                start
                end
                space {
                    id
                    name
                }
            }
        }
    """)
    
    params = {"space": space, "start_threshold": start_threshold, "end": end_timestamp}
    
    try:
        data = await session.execute(query, variable_values=params)
        return data.get("proposals", [])
    except Exception as e:
        logging.error(f"Error fetching ending proposals for {space}: {e}")
        return []

async def fetch_newly_active_proposals(session, space: str, start_after: int, start_before: int) -> List[Dict[str, Any]]:
    """
    Fetches proposals whose voting started within the scanning window.
    Catches proposals that transition from pending to active.
    """
    query = gql("""
        query NewProposals ($space: String!, $start_after: Int!, $start_before: Int!) {
            proposals (
                first: 100,
                where: {
                    space: $space,
                    start_gte: $start_after,
                    start_lte: $start_before,
                    state: "active"
                },
                orderBy: "start",
                orderDirection: desc
            ) {
                id
                title
                start
                end
                created
                space {
                    id
                    name
                }
            }
        }
    """)

    params = {"space": space, "start_after": start_after, "start_before": start_before}

    try:
        data = await session.execute(query, variable_values=params)
        return data.get("proposals", [])
    except Exception as e:
        logging.error(f"Error fetching new proposals for {space}: {e}")
        return []

async def fetch_newly_created_proposals(session, space: str, created_after: int, created_before: int) -> List[Dict[str, Any]]:
    """
    Fetches proposals created within the scanning window that are already active.
    Catches replicated proposals with backdated start timestamps.
    """
    query = gql("""
        query NewCreatedProposals ($space: String!, $created_after: Int!, $created_before: Int!) {
            proposals (
                first: 100,
                where: {
                    space: $space,
                    created_gte: $created_after,
                    created_lte: $created_before,
                    state: "active"
                },
                orderBy: "created",
                orderDirection: desc
            ) {
                id
                title
                start
                end
                created
                space {
                    id
                    name
                }
            }
        }
    """)

    params = {"space": space, "created_after": created_after, "created_before": created_before}

    try:
        data = await session.execute(query, variable_values=params)
        return data.get("proposals", [])
    except Exception as e:
        logging.error(f"Error fetching newly created proposals for {space}: {e}")
        return []

async def fetch_all_active_proposals(session, space: str) -> List[Dict[str, Any]]:
    """
    Fetches ALL currently active proposals for a space (catch-all).
    No time window filter — catches any proposal missed by windowed queries.
    """
    query = gql("""
        query AllActiveProposals ($space: String!) {
            proposals (
                first: 100,
                where: {
                    space: $space,
                    state: "active"
                },
                orderBy: "start",
                orderDirection: desc
            ) {
                id
                title
                start
                end
                space {
                    id
                    name
                }
            }
        }
    """)

    params = {"space": space}

    try:
        data = await session.execute(query, variable_values=params)
        return data.get("proposals", [])
    except Exception as e:
        logging.error(f"Error fetching all active proposals for {space}: {e}")
        return []

async def has_delegation_voted(session, proposal_id: str, voter_address: str) -> bool:
    """
    Checks if a specific address has voted on a proposal.
    """
    query = gql("""
        query CheckVote($proposalId: String!, $voter: String!) {
            votes(
                first: 1
                where: {
                    proposal: $proposalId
                    voter: $voter
                }
            ) {
                id
            }
        }
    """)
    
    params = {"proposalId": proposal_id, "voter": voter_address}
    
    try:
        data = await session.execute(query, variable_values=params)
        votes = data.get("votes", [])
        return len(votes) > 0
    except Exception as e:
        logging.error(f"Error checking vote for {proposal_id}: {e}")
        return False

async def process_reminder_proposal(session, proposal: Dict[str, Any], semaphore: asyncio.Semaphore, alert_config: Dict[str, Any], redis_client=None) -> bool:
    """
    Processes a proposal that is about to end (Reminder logic).
    Checks if we voted, and alerts if not.

    Returns False only when a due reminder could not be delivered, so the
    on-chain flow can keep its watermark and retry next run. Snapshot
    callers don't pass redis_client and keep the historical fire-and-forget
    behavior (no idempotence key, result ignored).
    """
    proposal_id = proposal["id"]
    proposal_title = proposal['title']
    proposal_end = proposal['end']
    space_id = proposal["space"]["id"]
    space_name = proposal["space"]["name"]

    is_onchain = "platform_address" in proposal

    # Idempotence (on-chain only): if this exact reminder already went out
    # during a partially-failed run, don't send it again on the retry.
    sent_key = None
    if is_onchain and redis_client is not None:
        sent_key = f"{ONCHAIN_SENT_PREFIX}:{proposal_id}:{alert_config['hours']}h"
        if redis_client.get(sent_key):
            return True

    # Check if we already voted (Convex on-chain platforms use getVote,
    # Snapshot spaces use the GraphQL votes query)
    async with semaphore:
        if is_onchain:
            try:
                voted = await asyncio.to_thread(
                    _has_delegate_voted_onchain_sync, proposal
                )
            except Exception as e:
                # Better a spurious reminder than a silently missed one
                logging.error(f"Error checking on-chain vote for {proposal_id}: {e}")
                voted = False
        else:
            voted = await has_delegation_voted(session, proposal_id, ContractRegistry.DELEGATION[1])

    if voted:
        return True

    emoji = alert_config["emoji"]
    label = alert_config["label"]

    message = f"{emoji} {space_name} {label} : {proposal_title}\n"
    message += f"End Date: {format_utc_timestamp(proposal_end)}"

    link = ""
    if is_onchain:
        # The platform page where the delegate actually casts the vote,
        # opened inside the delegate Safe
        link = _safe_app_link(proposal.get("link", CONVEX_VOTE_URL))
    elif space_id == "cvx.eth":
        link = f"https://snapshot.org/#/s:cvx.eth/proposal/{proposal_id}"
    elif space_id == "aurafinance.eth":
        link = f"https://snapshot.org/#/aurafinance.eth/proposal/{proposal_id}"
    elif space_id == "gauges.aurafinance.eth":
        link = f"https://snapshot.org/#/gauges.aurafinance.eth/proposal/{proposal_id}"
    else:
        link = f"https://www.stakedao.org/governance/protocols?voteId={proposal_id}"
    # parse_mode=html: the "&" separating the Safe App query params must be
    # escaped, or Telegram rejects the message and falls back to plain text
    message += f" (<a href='{html.escape(link, quote=False)}'>link</a>)"
    message += "\n@chago0x"

    # In-flight lock: two overlapping job runs must not both pass the GET
    # above and double-send. Telegram calls are bounded to 30s (well below
    # the 300s lease), so the lock cannot expire mid-delivery. When another
    # run holds the lock, only report success if its delivery is already
    # confirmed — otherwise keep OUR watermark frozen so the window survives
    # a crash of the lock holder.
    if sent_key and not redis_client.set(f"{sent_key}:inflight", 1, nx=True, ex=300):
        return bool(redis_client.get(sent_key))

    try:
        delivered = send_telegram_message(
            GlobalConstants.VOTE_REMINDER_BOT_API_KEY,
            GlobalConstants.VOTE_REMINDER_CHANNEL,
            message,
            "html"
        )
    except Exception as e:
        logging.error(f"Failed to send telegram message: {e}")
        delivered = False

    if not delivered:
        return False
    print(f"Reminder alert sent for {proposal_id} ({label})")
    if sent_key:
        redis_client.set(sent_key, 1, ex=ONCHAIN_SENT_TTL)
    return True

async def process_new_proposal_alert(proposal: Dict[str, Any], redis_client=None) -> bool:
    """
    Sends an alert for a newly created proposal.
    No vote check is needed here.
    If redis_client is provided, records the proposal ID on successful
    delivery so the alert is not repeated; an undelivered alert stays out of
    Redis and is retried by the catch-all (Snapshot) or the next on-chain
    poll (Convex platforms).
    """
    proposal_id = proposal["id"]
    proposal_title = proposal['title']
    proposal_end = proposal['end']
    space_id = proposal["space"]["id"]
    space_name = proposal["space"]["name"]

    message = f"🟢 New {space_name} proposal : {proposal_title}\n"
    message += f"End Date: {format_utc_timestamp(proposal_end)}"

    link = ""
    if "platform_address" in proposal:
        # The platform page where the delegate actually casts the vote,
        # opened inside the delegate Safe
        link = _safe_app_link(proposal.get("link", CONVEX_VOTE_URL))
    elif space_id == "cvx.eth":
        link = f"https://snapshot.org/#/s:cvx.eth/proposal/{proposal_id}"
    elif space_id == "aurafinance.eth":
        link = f"https://snapshot.org/#/aurafinance.eth/proposal/{proposal_id}"
    elif space_id == "gauges.aurafinance.eth":
        link = f"https://snapshot.org/#/gauges.aurafinance.eth/proposal/{proposal_id}"
    else:
        link = f"https://www.stakedao.org/governance/protocols?voteId={proposal_id}"
    # parse_mode=html: the "&" separating the Safe App query params must be
    # escaped, or Telegram rejects the message and falls back to plain text
    message += f" (<a href='{html.escape(link, quote=False)}'>link</a>)"
    message += "\n@chago0x"

    try:
        delivered = send_telegram_message(
            GlobalConstants.VOTE_REMINDER_BOT_API_KEY,
            GlobalConstants.VOTE_REMINDER_CHANNEL,
            message,
            "html"
        )
    except Exception as e:
        logging.error(f"Failed to send new proposal telegram message: {e}")
        return False

    if not delivered:
        return False
    print(f"New Proposal Alert sent for {proposal_id}")
    if redis_client:
        redis_client.sadd("delegation_reminder_alerted_proposals", proposal_id)
    return True

async def job():
    # --- 1. Initialization and Timestamp Retrieval ---
    redis_client = get_redis_client()
    redis_key = "delegation_reminder_timestamp"
    onchain_redis_key = "delegation_reminder_onchain_timestamp"

    now = int(time.time())

    # Retrieve the timestamp of the last successful run
    last_run_str = redis_client.get(redis_key)

    if last_run_str:
        last_run = int(last_run_str)
        print(f"⏱️  Last run found: {format_utc_timestamp(last_run)}")
    else:
        # Fallback: if first run, look back 15 minutes by default
        last_run = now - (15 * 60)
        print(f"⚠️ No last run found. Defaulting to: {format_utc_timestamp(last_run)}")

    # The on-chain flow keeps its OWN watermark: freezing the shared one on
    # an RPC/delivery failure would replay every Snapshot reminder already
    # sent (that path has no delivery dedup), and vice versa.
    onchain_last_run_str = redis_client.get(onchain_redis_key)
    if onchain_last_run_str:
        onchain_last_run = int(onchain_last_run_str)
    else:
        # First run: persist the baseline immediately (SETNX). If this run
        # fails, the retry must resume from HERE — a recomputed fallback
        # would slide forward past the missed windows. Seeding from the
        # shared watermark makes the on-chain flow inherit the Snapshot
        # cadence at migration.
        redis_client.setnx(onchain_redis_key, last_run)
        onchain_last_run = int(redis_client.get(onchain_redis_key))

    # Calculate elapsed time for info
    time_diff = now - last_run
    print(f"🔎 Scanning window: {time_diff} seconds ({format_utc_timestamp(last_run)} -> {format_utc_timestamp(now)})")
    print(f"⛓️  On-chain scanning window: {format_utc_timestamp(onchain_last_run)} -> {format_utc_timestamp(now)}")

    # --- 2. Retrieve Spaces ---
    spaces = await get_snapshot_spaces()
    print(f"Checking {len(spaces)} spaces...")

    transport = AIOHTTPTransport(url=SNAPSHOT_GRAPHQL_URL)
    
    # Use a try/finally block to ensure connections are closed cleanly
    try:
        async with Client(transport=transport, fetch_schema_from_transport=False) as session:
            semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

            # --- 3. New Proposals (phase 1) ---
            # Search for proposals by start AND by created to catch replicated proposals
            # Must run BEFORE catch-all so Redis is populated before catch-all checks it
            new_proposal_tasks = []
            print("--- Phase 1: New Proposals Checks ---")
            for space in spaces:
                new_proposal_tasks.append(
                    fetch_and_alert_new_proposals(session, space, last_run, now, redis_client)
                )

            # --- 4. Reminders 24h / 2h (can run in parallel with new proposals) ---
            reminder_tasks = []
            for config in ALERT_CONFIGS:
                label = config["label"]
                offset = config["offset_seconds"]

                # Target window: [Last Run + offset, Now + offset]
                # Ex for 24h: Search for proposals ending between (LastRun + 24h) and (Now + 24h)
                start_window = last_run + offset
                end_window = now + offset

                print(f"--- Queueing: {label} (Ends between {format_utc_timestamp(start_window)} and {format_utc_timestamp(end_window)}) ---")

                for space in spaces:
                    reminder_tasks.append(
                        handle_reminder_flow(session, space, start_window, end_window, config, semaphore)
                    )

            # --- 4bis. Convex on-chain platforms (cvx.eth counterpart) ---
            # One coroutine gathered WITH the Snapshot tasks so a slow RPC
            # never delays the Snapshot spaces processing.
            convex_flow = handle_convex_onchain_flow(
                session, redis_client, onchain_last_run, now, semaphore
            )

            # --- 5a. Run new proposals + reminders + Convex flow in parallel ---
            print(f"🚀 Executing {len(new_proposal_tasks) + len(reminder_tasks) + 1} tasks (phase 1)...")
            results = await asyncio.gather(
                *new_proposal_tasks, *reminder_tasks, convex_flow
            )
            convex_ok = results[-1]

            # --- 5b. Catch-all AFTER new proposals are recorded in Redis ---
            catchall_tasks = []
            print("--- Phase 2: Catch-all Active Proposals ---")
            for space in spaces:
                catchall_tasks.append(
                    catchall_new_proposals(session, space, redis_client)
                )
            print(f"🚀 Executing {len(catchall_tasks)} catch-all tasks (phase 2)...")
            await asyncio.gather(*catchall_tasks)

        # --- 6. Update Redis (Success Only) ---
        # Only update if everything went well to avoid missing events in case of a crash
        _advance_watermark(redis_client, redis_key, now)
        if convex_ok:
            _advance_watermark(redis_client, onchain_redis_key, now)
            print(f"✅ Job finished successfully. Updated Redis to: {now}")
        else:
            # Do NOT advance the on-chain watermark: a 24h/2h threshold
            # crossed during the failed poll/delivery would otherwise never
            # be retried. Reminders that DID go out in this run are protected
            # from re-sending by their idempotence keys.
            logging.error("❌ Convex on-chain watcher failed — keeping previous on-chain timestamp for retry")

    except Exception as e:
        logging.error(f"❌ Job failed: {e}")
        # In case of error, do NOT update Redis. 
        # The next run will resume from the old timestamp (automatic retry).

# --- Helper Functions to simplify job() ---

# Atomic compare-and-set: only advance the stored watermark
_WATERMARK_CAS_LUA = """
local cur = redis.call('GET', KEYS[1])
if (not cur) or tonumber(cur) < tonumber(ARGV[1]) then
    redis.call('SET', KEYS[1], ARGV[1])
    return 1
end
return 0
"""

def _advance_watermark(redis_client, key: str, now: int):
    """Monotonic watermark write: an overlapping run that finishes late must
    not roll back the window already covered by a newer one. Atomic via Lua
    CAS; falls back to a plain GET/SET where EVAL is unavailable (still
    monotonic without concurrency, and a rare lost update only re-scans an
    already-covered window)."""
    try:
        redis_client.eval(_WATERMARK_CAS_LUA, 1, key, now)
    except Exception as e:
        logging.error(f"Watermark CAS failed for {key}, falling back to GET/SET: {e}")
        current = redis_client.get(key)
        if current is None or int(current) < now:
            redis_client.set(key, now)

async def process_task_safe(fetch_coro, callback, seen_ids: set = None, **callback_kwargs):
    """Executes a fetch and, if results exist, triggers the callback (alert)."""
    try:
        results = await fetch_coro
        if results:
            if seen_ids is not None:
                results = [r for r in results if r["id"] not in seen_ids]
                seen_ids.update(r["id"] for r in results)
            if results:
                await asyncio.gather(*[callback(item, **callback_kwargs) for item in results])
    except Exception as e:
        logging.error(f"Error in task processing: {e}")

async def fetch_and_alert_new_proposals(session, space, last_run, now, redis_client):
    """Fetch new proposals by start AND by created, deduplicate, then alert."""
    seen_ids = set()
    # 1. Proposals whose voting started in the window
    await process_task_safe(
        fetch_newly_active_proposals(session, space, last_run, now),
        callback=process_new_proposal_alert,
        seen_ids=seen_ids,
        redis_client=redis_client
    )
    # 2. Proposals created in the window (catches replicated proposals with backdated start)
    await process_task_safe(
        fetch_newly_created_proposals(session, space, last_run, now),
        callback=process_new_proposal_alert,
        seen_ids=seen_ids,
        redis_client=redis_client
    )

async def catchall_new_proposals(session, space, redis_client):
    """Catch-all: alert for any active proposal we haven't alerted yet."""
    redis_key = "delegation_reminder_alerted_proposals"
    try:
        proposals = await fetch_all_active_proposals(session, space)
        for proposal in proposals:
            if not redis_client.sismember(redis_key, proposal["id"]):
                await process_new_proposal_alert(proposal, redis_client=redis_client)
    except Exception as e:
        logging.error(f"Error in catch-all for {space}: {e}")

async def handle_convex_onchain_flow(session, redis_client, last_run, now, semaphore) -> bool:
    """
    Full alert flow for the Convex on-chain platforms, gathered concurrently
    with the Snapshot tasks. Same semantics as the Snapshot spaces:
    new-proposal alert (Redis-deduped) + 24h/2h unvoted reminders.

    Returns False on any read or delivery failure so the caller keeps the
    on-chain watermark and the missed windows are retried on the next run
    (already-delivered reminders are skipped via their idempotence keys).
    Platform read failures are isolated: the healthy platforms' proposals
    are still processed in the same run.
    """
    try:
        proposals, fetch_ok = await fetch_convex_onchain_proposals()
    except Exception as e:
        logging.error(f"Error fetching Convex on-chain proposals: {e}")
        return False

    tasks = []
    for p in proposals:
        is_active = p["start"] <= now < p["end"]
        if is_active and not redis_client.sismember(
            "delegation_reminder_alerted_proposals", p["id"]
        ):
            tasks.append(process_new_proposal_alert(p, redis_client=redis_client))
        for config in ALERT_CONFIGS:
            if _is_in_reminder_window(p["end"], last_run, now, config["offset_seconds"]):
                tasks.append(
                    process_reminder_proposal(
                        session, p, semaphore, config, redis_client=redis_client
                    )
                )

    if not tasks:
        return fetch_ok
    results = await asyncio.gather(*tasks)
    return fetch_ok and all(results)

async def handle_reminder_flow(session, space, start_window, end_window, config, semaphore):
    """Manages the full flow for a reminder: Fetch -> Filter -> Check Vote -> Alert"""
    try:
        # 1. Fetch proposals ending in the specific window
        proposals = await fetch_ending_proposals(session, space, start_window, end_window)
        
        if proposals:
            # 2. Check votes and alert
            await asyncio.gather(*[
                process_reminder_proposal(session, p, semaphore, config) 
                for p in proposals
            ])
    except Exception as e:
        logging.error(f"Error in reminder flow for {space}: {e}")

def main():
    try:
        asyncio.run(job())
    except Exception as e:
        print(f"Fatal error: {e}")

if __name__ == "__main__":
    main()