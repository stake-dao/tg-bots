import asyncio


import httpx


from dotenv import load_dotenv


load_dotenv()


class StakeDAOError(Exception):
    """Base exception for StakeDAO API errors"""


class DataNotFoundError(StakeDAOError):
    """Raised when expected data is not found"""


class InvalidProtocolError(StakeDAOError):
    """Raised when an invalid protocol is specified"""


PROTOCOLS = ["curve", "balancer", "fxn", "pendle", "yb"]


class StakeDAOApiService:
    def __init__(self, w3):
        self.w3 = w3
        self.base_url = "https://votemarket-api.contact-69d.workers.dev"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://stakedao.org",
        }

        # Retry configuration
        self.max_retries = 3
        self.initial_delay = 5  # seconds
        self.max_delay = 300  # 5 minutes
        self.backoff_factor = 2


    async def _retry_with_backoff(self, func, *args, **kwargs):
        """Execute a function with exponential backoff retry logic"""
        last_exception = None

        for attempt in range(self.max_retries):
            try:
                return await func(*args, **kwargs)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_exception = e

                # Check if it's a 504 Gateway Timeout or other retryable error
                if isinstance(e, httpx.HTTPStatusError):
                    if e.response.status_code not in [502, 503, 504]:
                        # Don't retry for non-gateway errors
                        raise

                if attempt < self.max_retries - 1:
                    # Calculate delay with exponential backoff
                    delay = min(
                        self.initial_delay * (self.backoff_factor**attempt),
                        self.max_delay,
                    )
                    print(
                        f"Request failed (attempt {attempt + 1}/{self.max_retries}), "
                        f"retrying in {delay} seconds... Error: {str(e)}"
                    )
                    await asyncio.sleep(delay)
                else:
                    # Last attempt failed
                    raise

        # This should never be reached, but just in case
        if last_exception:
            raise last_exception



    async def get_gauges_data(self, protocol: str):
        if protocol not in PROTOCOLS:
            raise InvalidProtocolError(f"Unsupported protocol: {protocol}")

        async def _make_request():
            url = f"{self.base_url}/{protocol}/gauges"

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(360.0)
            ) as client:
                response = await client.get(
                    url, headers=self.headers
                )
                response.raise_for_status()
                data = response.json()
                if not data:
                    raise DataNotFoundError(
                        f"No data found for protocol {protocol}"
                    )
                return data

        try:
            return await self._retry_with_backoff(_make_request)
        except httpx.HTTPError as e:
            raise StakeDAOError(
                f"Failed to get votemarket data for {protocol}: {str(e)}"
            )
