import time


from collections import deque


from threading import RLock


from typing import Dict, Optional


class RateLimiter:
    """
    A simple rate limiter that tracks API calls and enforces rate limits.

    Args:
        calls_per_minute: Maximum number of calls allowed per minute
        calls_per_second: Maximum number of calls allowed per second (optional)
    """

    def __init__(
        self,
        calls_per_minute: int = 30,
        calls_per_second: Optional[int] = None,
    ):
        self.calls_per_minute = calls_per_minute
        self.calls_per_second = calls_per_second or calls_per_minute // 60
        self.call_times = deque()
        self.lock = RLock()  # Use RLock to support recursive calls

    def wait_if_needed(self):
        """Wait if necessary to respect rate limits"""
        with self.lock:
            current_time = time.time()

            # Remove calls older than 1 minute
            while self.call_times and self.call_times[0] < current_time - 60:
                self.call_times.popleft()

            # Check per-minute limit
            if len(self.call_times) >= self.calls_per_minute:
                # Wait until the oldest call is more than 1 minute old
                wait_time = 60 - (current_time - self.call_times[0]) + 0.1
                if wait_time > 0:
                    print(
                        f"Rate limit reached. Waiting {wait_time:.1f} seconds..."
                    )
                    time.sleep(wait_time)
                    return self.wait_if_needed()

            # Check per-second limit
            recent_calls = [t for t in self.call_times if t > current_time - 1]
            if len(recent_calls) >= self.calls_per_second:
                # Wait until next second
                wait_time = 1 - (current_time - recent_calls[0]) + 0.1
                if wait_time > 0:
                    time.sleep(wait_time)
                    return self.wait_if_needed()

            # Record this call
            self.call_times.append(current_time)


class APIRateLimiters:
    """Singleton class to manage rate limiters for different APIs"""

    _limiters: Dict[str, RateLimiter] = {}

    def get_limiter(self, api_name: str) -> RateLimiter:
        """Get or create a rate limiter for a specific API"""
        if api_name not in self._limiters:
            # Configure rate limits for different APIs
            if api_name == "geckoterminal":
                # GeckoTerminal has a limit of 30 calls per minute for free tier
                self._limiters[api_name] = RateLimiter(
                    calls_per_minute=25, calls_per_second=1
                )
            elif api_name == "defillama":
                # Defillama is more generous, but let's be respectful
                self._limiters[api_name] = RateLimiter(
                    calls_per_minute=300, calls_per_second=5
                )
            else:
                # Default rate limit
                self._limiters[api_name] = RateLimiter(
                    calls_per_minute=60, calls_per_second=2
                )

        return self._limiters[api_name]


api_rate_limiters = APIRateLimiters()
