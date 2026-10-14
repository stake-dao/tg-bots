"""Chunked ``eth_getLogs`` scans that adapt to provider block-range limits."""

import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from web3.exceptions import Web3RPCError

# Largest block window requested per eth_getLogs call, per chain. The Stake
# eRPC worker rejects anything above 30000 blocks and its public upstreams are
# far stricter (drpc 10000, publicnode ~5000 recent blocks, 1rpc 50), so start
# well below those and shrink further when a provider still rejects the range.
LOG_RANGE_LIMITS = {
    1: 2000,
    10: 10000,
    56: 5000,
    252: 5000,
    324: 5000,
    8453: 10000,
    42161: 10000,
}
DEFAULT_LOG_RANGE_LIMIT = 5000
# Below this window a backlog cannot be scanned within the job timeout, so a
# provider that still rejects it is switched out instead of hammered.
MIN_LOG_RANGE = 500
# JSON-RPC codes providers return when a range is too wide: eRPC -32012,
# 1rpc -32602, Alchemy/Infura/eRPC -32005, drpc 35.
RANGE_LIMIT_RPC_CODES = {-32012, -32602, -32005, 35}
RANGE_LIMIT_MESSAGE = re.compile(
    r"block range|range too large|exceeded max|max allowed|limited to|"
    r"too many (?:results|logs|events)|more than \d+ results|query timeout|response size",
    re.IGNORECASE,
)
# FallbackHTTPProvider already cycles through every endpoint on -32005 before
# returning it, so rotating again on it would only repeat that cycle.
PROVIDER_ROTATED_RPC_CODES = {-32005}
# Endpoints refusing the window whatever its size (pruned history, free plan
# limits on old blocks): useless for this scan, switch at once.
ENDPOINT_REFUSAL_MESSAGE = re.compile(
    r"archive requests require|personal token|not supported on free plan|"
    r"pruned|missing trie node",
    re.IGNORECASE,
)
# Endpoint saturated: switch to the next one without shrinking the window.
RATE_LIMIT_RPC_CODES = {15}
RATE_LIMIT_MESSAGE = re.compile(r"rate limit|too many requests", re.IGNORECASE)
# Limits providers spell out in their message: "up to a 10 block range",
# "maximum allowed is 50 blocks", "limited to 0 - 50 blocks range",
# "ranges over 10000 blocks are not supported".
ADVERTISED_LIMIT_MESSAGE = re.compile(
    r"(?:up to a|maximum allowed is|limited to 0 -|maximum of|ranges over)\s*(\d+)\s*blocks?",
    re.IGNORECASE,
)
RETRY_DELAY_SECONDS = 1.0

# Chunk size a chain's provider was seen to accept during this process, so the
# shrinking ladder runs once per chain rather than once per call site.
_learned_limits: Dict[int, int] = {}


def reset_learned_limits() -> None:
    _learned_limits.clear()


def _rpc_error(error: BaseException) -> Dict[str, Any]:
    response = getattr(error, "rpc_response", None)
    if not isinstance(response, dict):
        return {}
    rpc_error = response.get("error")
    return rpc_error if isinstance(rpc_error, dict) else {}


def _message(error: BaseException) -> str:
    return str(_rpc_error(error).get("message") or error)


def is_endpoint_refusal(error: BaseException) -> bool:
    """True when the endpoint cannot serve this window at any size."""
    return isinstance(error, Web3RPCError) and bool(ENDPOINT_REFUSAL_MESSAGE.search(_message(error)))


def is_rate_limit_error(error: BaseException) -> bool:
    """True when the endpoint refused the request because it is saturated."""
    if not isinstance(error, Web3RPCError):
        return False
    if _rpc_error(error).get("code") in RATE_LIMIT_RPC_CODES:
        return True
    return bool(RATE_LIMIT_MESSAGE.search(_message(error)))


def is_range_limit_error(error: BaseException) -> bool:
    """True when the provider rejected the request because of its block window."""
    if not isinstance(error, Web3RPCError):
        return False
    if _rpc_error(error).get("code") in RANGE_LIMIT_RPC_CODES:
        return True
    return bool(RANGE_LIMIT_MESSAGE.search(_message(error)))


def advertised_range_limit(error: BaseException) -> Optional[int]:
    """Block window the provider says it accepts, when the error states one."""
    rpc_error = _rpc_error(error)
    data = rpc_error.get("data")
    details = data.get("details") if isinstance(data, dict) else None
    limit = details.get("maxAllowedRange") if isinstance(details, dict) else None
    if isinstance(limit, int) and limit > 0:
        return limit
    match = ADVERTISED_LIMIT_MESSAGE.search(str(rpc_error.get("message") or ""))
    return int(match.group(1)) if match else None


def _describe(error: BaseException) -> str:
    rpc_error = _rpc_error(error)
    if rpc_error:
        return f"code {rpc_error.get('code')}: {rpc_error.get('message')}"
    return str(error)


def _provider(event: Any) -> Any:
    return getattr(getattr(event, "w3", None), "provider", None)


def _endpoint_count(event: Any) -> int:
    return getattr(_provider(event), "endpoint_count", 1)


def _rotate_provider(event: Any) -> bool:
    """Move a FallbackHTTPProvider to its next endpoint; False when not possible."""
    rotate = getattr(_provider(event), "rotate", None)
    if rotate is None:
        return False
    # Host only: endpoint URLs embed API keys.
    logging.info(f"Switching RPC endpoint to {urlsplit(rotate()).netloc}")
    return True


def _smaller_chunk(window: int, advertised: Optional[int]) -> int:
    """Next chunk to try after ``window`` was rejected; 0 when this endpoint is useless."""
    if advertised is not None and advertised < MIN_LOG_RANGE:
        return 0
    if advertised is not None and advertised < window:
        return advertised
    if window > MIN_LOG_RANGE:
        return max(window // 2, MIN_LOG_RANGE)
    return 0


def fetch_event_logs(
    event: Any,
    from_block: int,
    to_block: int,
    chain_id: int,
    argument_filters: Optional[Dict[str, Any]] = None,
    max_range: Optional[int] = None,
) -> List[Any]:
    """Scan ``event`` logs over an inclusive block window in provider-sized chunks.

    The chunk starts at the chain's configured (or previously learned) limit
    and shrinks on each range rejection: to the limit the provider advertises
    when it states one, otherwise to half the rejected window, never below
    ``MIN_LOG_RANGE``. An endpoint that still rejects the floor, caps below
    it, refuses the window outright or is rate limited is rotated out
    (``FallbackHTTPProvider``) and the window retried; once every endpoint was
    tried the error is raised so the caller keeps the chain's checkpoint. Any
    other error is raised at once.
    """
    if to_block < from_block:
        return []

    configured = max_range or LOG_RANGE_LIMITS.get(chain_id, DEFAULT_LOG_RANGE_LIMIT)
    chunk = min(configured, _learned_limits.get(chain_id, configured))
    rotations_left = _endpoint_count(event) - 1
    logs: List[Any] = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        kwargs: Dict[str, Any] = {"from_block": start, "to_block": end}
        if argument_filters:
            kwargs["argument_filters"] = argument_filters
        try:
            logs.extend(event.get_logs(**kwargs))
        except Web3RPCError as error:
            refused = is_endpoint_refusal(error) or is_rate_limit_error(error)
            if not refused and not is_range_limit_error(error):
                raise
            smaller = 0 if refused else _smaller_chunk(end - start + 1, advertised_range_limit(error))
            can_rotate = rotations_left > 0 and (
                refused or _rpc_error(error).get("code") not in PROVIDER_ROTATED_RPC_CODES
            )
            if smaller:
                chunk = smaller
                if max_range is None:
                    _learned_limits[chain_id] = chunk
                logging.info(
                    f"Chain {chain_id}: eth_getLogs rejected {start}-{end} "
                    f"({_describe(error)}), retrying with {chunk}-block chunks"
                )
            elif can_rotate and _rotate_provider(event):
                rotations_left -= 1
                if not refused:
                    # The new endpoint has its own range cap: start over.
                    chunk = configured
                    _learned_limits.pop(chain_id, None)
                logging.info(
                    f"Chain {chain_id}: eth_getLogs rejected {start}-{end} "
                    f"({_describe(error)}), retrying on the next endpoint"
                )
            else:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        start = end + 1
    return logs
