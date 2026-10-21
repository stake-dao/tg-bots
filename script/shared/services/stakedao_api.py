"""Votemarket Hub client; deprecated markets remain readable for final claims."""

import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv(
    "VOTEMARKET_API_URL", "https://hub.stakedao.org/v1/votemarket"
).rstrip("/")
PROTOCOLS = ["curve", "balancer", "fxn", "pendle", "yb"]
CAPABILITIES = (
    "gauges",
    "campaigns",
    "externalCampaigns",
    "analytics",
    "vlcvxAnalytics",
    "rounds",
)


class StakeDAOError(Exception):
    """A request failed or returned an invalid Hub response."""


class DataNotFoundError(StakeDAOError):
    pass


class InvalidProtocolError(StakeDAOError):
    pass


def parse_protocols(data):
    if not isinstance(data, list) or not data:
        raise DataNotFoundError("Missing protocol discovery")
    ids = set()
    for item in data:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or item["id"] in ids
            or item.get("status") not in ("active", "deprecated")
            or not isinstance(item.get("capabilities"), dict)
            or any(
                type(item["capabilities"].get(key)) is not bool for key in CAPABILITIES
            )
        ):
            raise DataNotFoundError("Invalid protocol discovery")
        ids.add(item["id"])
    return data


def validate_collection(data, key):
    if (
        not isinstance(data, dict)
        or not isinstance(data.get(key), list)
        or any(not isinstance(item, dict) for item in data[key])
    ):
        raise DataNotFoundError(f"Missing or invalid {key} response")
    return data


class StakeDAOApiService:
    def __init__(self, w3=None, *, base_url=None, transport=None):
        self.w3 = w3
        self.base_url = (base_url or API_BASE_URL).rstrip("/")
        self.transport = transport
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "StakeDAO-automation",
        }
        self.max_retries = 3
        self.initial_delay = 5
        self.max_delay = 300
        self.backoff_factor = 2

    async def _retry_with_backoff(self, func, *args, **kwargs):
        for attempt in range(self.max_retries):
            try:
                return await func(*args, **kwargs)
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if isinstance(
                    error, httpx.HTTPStatusError
                ) and error.response.status_code not in (429, 500, 502, 503, 504):
                    raise
                if attempt + 1 == self.max_retries:
                    raise
                await asyncio.sleep(
                    min(
                        self.initial_delay * self.backoff_factor**attempt,
                        self.max_delay,
                    )
                )

    async def _request(self, path, *, params=None):

        async def request():
            async with httpx.AsyncClient(
                timeout=360, transport=self.transport
            ) as client:
                response = await client.get(
                    f"{self.base_url}{path}", headers=self.headers, params=params
                )
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError as error:
                    raise DataNotFoundError("Hub returned invalid JSON") from error

        try:
            return await self._retry_with_backoff(request)
        except httpx.HTTPError as error:
            raise StakeDAOError(f"Hub request failed: GET {path}: {error}") from error

    @staticmethod
    def _params(chain_id=None, block=None):
        params = {}
        for key, value in (("chainId", chain_id), ("block", block)):
            if value is not None:
                if type(value) is not int or value < (1 if key == "chainId" else 0):
                    raise ValueError(f"Invalid {key}")
                params[key] = value
        return params

    @staticmethod
    def _protocol(protocol):
        if protocol not in PROTOCOLS:
            raise InvalidProtocolError(f"Unsupported protocol: {protocol}")
        return protocol

    async def get_protocol_metadata(self):
        return parse_protocols(await self._request("/protocols"))

    async def get_protocols(self, *, active_only=False, capability=None):
        if capability is not None and capability not in CAPABILITIES:
            raise ValueError(f"Unknown capability: {capability}")
        return [
            item["id"]
            for item in await self.get_protocol_metadata()
            if (not active_only or item["status"] == "active")
            and (capability is None or item["capabilities"][capability])
        ]

    async def get_gauges_data(self, protocol: str, *, block=None):
        protocol = self._protocol(protocol)
        data = await self._request(
            f"/{protocol}/gauges", params=self._params(block=block)
        )
        return validate_collection(data, "gauges")
