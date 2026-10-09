from functools import wraps


import os


import time


from typing import Any, Callable, Dict, List, Optional, TypeVar


import requests


from dotenv import load_dotenv


from rich.console import Console


load_dotenv()


EXPLORER_KEY = os.getenv("EXPLORER_KEY", "") or os.getenv("ETHERSCAN_API_KEY", "")


console = Console()


T = TypeVar("T")


def retry_on_network_error(
    max_retries: int = 5,
    initial_backoff: float = 2.0,
    max_backoff: float = 32.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator that retries on network errors (timeout, connection) with exponential backoff.

    This handles transport-level failures. API-level errors (rate limits, etc.) should
    still be handled inside the decorated function.

    Args:
        max_retries: Maximum number of retry attempts
        initial_backoff: Initial delay in seconds before first retry
        max_backoff: Maximum delay between retries
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            backoff = initial_backoff
            last_error: Optional[Exception] = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                ) as e:
                    last_error = e
                    if attempt < max_retries:
                        console.print(
                            f"\n [yellow]Network error (attempt {attempt + 1}/{max_retries + 1}): "
                            f"{type(e).__name__}, retrying in {backoff:.1f}s...[/yellow]"
                        )
                        time.sleep(backoff)
                        backoff = min(backoff * 2, max_backoff)
                        continue
                    console.print(
                        f"\n [red]Network error after {max_retries + 1} attempts: {e}[/red]"
                    )
                    raise

            # Should not reach here, but safety net
            raise last_error or Exception("Max retries reached")

        return wrapper

    return decorator


@retry_on_network_error(max_retries=5)
def get_logs_by_address_and_topics(
    address: str,
    from_block: int,
    to_block: int,
    topics: Dict[str, str],
    chain_id: int = 1,
) -> List[Dict[str, Any]]:
    """
    Fetch logs for an address within a block range, filtered by topics.

    Args:
        address: The contract address to query
        from_block: Starting block number
        to_block: Ending block number
        topics: Dictionary of topic filters (key: topic position, value: topic hash)
        chain_id: The blockchain network ID (default: 1 for Ethereum mainnet)

    Returns:
        List of log entries matching the criteria
    """
    url = (
        f"https://api.etherscan.io/v2/api?chainid={chain_id}&module=logs"
        f"&action=getLogs&fromBlock={from_block}&toBlock={to_block}"
        f"&address={address}&apikey={EXPLORER_KEY}"
    )

    topic_items = sorted(topics.items(), key=lambda kv: int(kv[0]))
    for key, value in topic_items:
        url += f"&topic{key}={value}"

    # Add required topic operators for all topic pairs
    keys = sorted(int(k) for k in topics.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            url += f"&topic{keys[i]}_{keys[j]}_opr=and"

    max_retries = 5
    backoff = 2.0

    for attempt in range(max_retries + 1):
        response = requests.get(url, timeout=30)
        data = response.json()
        status = str(data.get("status", ""))
        message = str(data.get("message", ""))
        result = data.get("result", "")
        result_str = str(result).lower()

        if status == "1":
            return result

        if status == "0" and "no records found" in message.lower():
            return []

        if status == "0" and message == "NOTOK" and not result_str.strip():
            return []

        message_lower = message.lower()
        is_rate_limited = (
            response.status_code == 429
            or "rate limit" in result_str
            or "temporarily unavailable" in result_str
            or "unusually high network activity" in result_str
            or "timeout" in message_lower
            or "server too busy" in message_lower
        )

        if is_rate_limited:
            if attempt < max_retries:
                console.print(
                    f"\n [yellow]API rate limit/unavailable (attempt {attempt + 1}/{max_retries + 1}), "
                    f"waiting {backoff:.1f}s...[/yellow]"
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 16.0)
                continue

        console.print(f"\n [red]Unexpected response: {data}[/red]")
        raise Exception(message or "Unknown error")

    raise Exception("Max retries reached")
