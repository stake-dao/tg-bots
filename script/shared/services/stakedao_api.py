"""Shared read-only client for Stake DAO Hub Votemarket gauge catalogs."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

DEFAULT_BASE_URL = "https://hub.stakedao.org/v1/votemarket"
REQUEST_TIMEOUT = 30
PROTOCOLS = ["curve", "balancer", "fxn", "pendle", "yb"]

_session = requests.Session()
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        ),
    ),
)


def fetch_gauges_data(protocol: str, base_url: str = DEFAULT_BASE_URL) -> dict:
    response = _session.get(f"{base_url}/{protocol}/gauges", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("gauges"), list)
        or any(
            not isinstance(gauge, dict)
            or not isinstance(gauge.get("gauge"), str)
            or not gauge["gauge"]
            for gauge in data["gauges"]
        )
    ):
        raise ValueError(f"Hub response has missing or invalid gauges for {protocol}")
    return data
