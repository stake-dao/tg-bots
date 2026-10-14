from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from web3.exceptions import Web3RPCError

from shared.utils import event_logs
from shared.utils.event_logs import MIN_LOG_RANGE, fetch_event_logs, reset_learned_limits


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(event_logs.time, "sleep", lambda _seconds: None)
    reset_learned_limits()
    yield
    reset_learned_limits()


def rpc_error(code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return Web3RPCError(message, rpc_response={"jsonrpc": "2.0", "id": 1, "error": error})


def range_error():
    return rpc_error(-32012, "getLogs request exceeded max allowed range")


class FakeProvider:
    """Mimics FallbackHTTPProvider: each endpoint has its own range cap."""

    def __init__(self, limits):
        self.limits = list(limits)
        self.index = 0
        self.rotations = 0

    @property
    def endpoint_count(self):
        return len(self.limits)

    @property
    def limit(self):
        return self.limits[self.index]

    def rotate(self):
        self.index = (self.index + 1) % len(self.limits)
        self.rotations += 1
        return f"https://endpoint-{self.index}.example/v2/secret-api-key"


class FakeEvent:
    """Stands in for ``contract.events.X()``; rejects windows wider than the cap."""

    def __init__(self, limit=None, error_factory=None, provider=None):
        self.limit = limit
        self.provider = provider
        self.w3 = SimpleNamespace(provider=provider) if provider else None
        self.error_factory = error_factory or range_error
        self.ranges = []
        self.filters = []

    def get_logs(self, from_block, to_block, argument_filters=None):
        self.ranges.append((from_block, to_block))
        self.filters.append(argument_filters)
        cap = self.provider.limit if self.provider else self.limit
        if to_block - from_block + 1 > cap:
            raise self.error_factory()
        return [f"log-{from_block}"]


def sizes(event):
    return [high - low + 1 for low, high in event.ranges]


def test_scan_covers_window_with_contiguous_chunks_under_limit():
    event = FakeEvent(limit=5000)
    start, end = 127_436_555, 127_436_555 + 12_345 - 1

    logs = fetch_event_logs(event, start, end, 56)

    assert sizes(event) == [5000, 5000, 2345]
    assert event.ranges[0][0] == start and event.ranges[-1][1] == end
    assert all(nxt[0] == prev[1] + 1 for prev, nxt in zip(event.ranges, event.ranges[1:]))
    assert len(logs) == 3


def test_scan_shrinks_to_advertised_limit_then_halves():
    def erpc_error():
        return rpc_error(
            -32012,
            "getLogs request exceeded max allowed range",
            data={"details": {"requestRange": 10000, "maxAllowedRange": 2000}},
        )

    event = FakeEvent(limit=1200, error_factory=erpc_error)

    fetch_event_logs(event, 0, 9_999, 42161, max_range=10_000)

    # 10000 rejected -> advertised 2000 rejected -> 1000 accepted onwards.
    assert sizes(event)[:3] == [10_000, 2_000, 1_000]
    assert all(size <= 1_000 for size in sizes(event)[2:])
    assert event.ranges[-1][1] == 9_999


def test_single_endpoint_gives_up_at_floor_and_raises():
    event = FakeEvent(limit=50, error_factory=lambda: rpc_error(-32012, "range too large"))

    with pytest.raises(Web3RPCError, match="range too large"):
        fetch_event_logs(event, 0, 49_999, 56, max_range=4_000)

    assert sizes(event) == [4_000, 2_000, 1_000, MIN_LOG_RANGE]


def test_endpoint_capped_below_floor_is_rotated_out_immediately():
    provider = FakeProvider(limits=[50, 10_000])
    event = FakeEvent(
        provider=provider,
        error_factory=lambda: rpc_error(-32602, "eth_getLogs is limited to 0 - 50 blocks range"),
    )

    logs = fetch_event_logs(event, 0, 9_999, 56)

    assert sizes(event) == [5_000, 5_000, 5_000]
    assert provider.rotations == 1
    assert len(logs) == 2


def test_floor_rejection_rotates_then_restarts_from_configured_chunk():
    provider = FakeProvider(limits=[300, 10_000])
    event = FakeEvent(provider=provider)

    fetch_event_logs(event, 0, 9_999, 56)

    assert sizes(event) == [5_000, 2_500, 1_250, 625, MIN_LOG_RANGE, 5_000, 5_000]
    assert provider.rotations == 1


def test_rotation_resets_the_learned_limit():
    provider = FakeProvider(limits=[300, 10_000])
    fetch_event_logs(FakeEvent(provider=provider), 0, 9_999, 56)
    assert event_logs._learned_limits == {}

    second = FakeEvent(provider=provider)
    fetch_event_logs(second, 0, 9_999, 56)
    assert sizes(second) == [5_000, 5_000]


def test_explicit_max_range_is_not_learned():
    fetch_event_logs(FakeEvent(limit=1_200), 0, 9_999, 42161, max_range=10_000)
    assert event_logs._learned_limits == {}


@pytest.mark.parametrize(
    "error",
    [
        lambda: rpc_error(-32602, "Archive requests require a personal token"),
        lambda: rpc_error(35, "ranges over 10000 blocks are not supported on free plan"),
        lambda: rpc_error(15, "You reached Public endpoint rate limit, please upgrade to paid plan"),
    ],
)
def test_refusing_or_saturated_endpoint_is_rotated_without_shrinking(error):
    provider = FakeProvider(limits=[0, 10_000])
    event = FakeEvent(provider=provider, error_factory=error)

    fetch_event_logs(event, 0, 9_999, 56)

    assert sizes(event) == [5_000, 5_000, 5_000]
    assert provider.rotations == 1
    assert event_logs._learned_limits == {}


def test_provider_rotated_error_shrinks_but_never_rotates_again():
    provider = FakeProvider(limits=[300, 10_000])
    event = FakeEvent(provider=provider, error_factory=lambda: rpc_error(-32005, "limit exceeded"))

    with pytest.raises(Web3RPCError, match="limit exceeded"):
        fetch_event_logs(event, 0, 9_999, 56)

    assert sizes(event) == [5_000, 2_500, 1_250, 625, MIN_LOG_RANGE]
    assert provider.rotations == 0


def test_rotation_log_hides_the_endpoint_path(caplog):
    import logging

    caplog.set_level(logging.INFO)
    fetch_event_logs(FakeEvent(provider=FakeProvider(limits=[300, 10_000])), 0, 9_999, 56)

    assert "Switching RPC endpoint to endpoint-1.example" in caplog.text
    assert "secret-api-key" not in caplog.text


def test_rotation_exhausted_raises():
    provider = FakeProvider(limits=[50, 50])
    event = FakeEvent(
        provider=provider,
        error_factory=lambda: rpc_error(-32012, "You can make eth_getLogs requests with up to a 50 block range"),
    )

    with pytest.raises(Web3RPCError, match="up to a 50 block range"):
        fetch_event_logs(event, 0, 9_999, 8453)

    assert sizes(event) == [10_000, 10_000]
    assert provider.rotations == 1


def test_unrelated_rpc_error_is_raised_immediately():
    event = FakeEvent(limit=1, error_factory=lambda: rpc_error(-32000, "execution reverted"))

    with pytest.raises(Web3RPCError, match="execution reverted"):
        fetch_event_logs(event, 0, 9_999, 1)

    assert len(event.ranges) == 1


def test_http_error_is_raised_immediately():
    def http_error():
        response = MagicMock()
        response.status_code = 403
        return requests.exceptions.HTTPError("403 Client Error", response=response)

    event = FakeEvent(limit=1, error_factory=http_error)

    with pytest.raises(requests.exceptions.HTTPError):
        fetch_event_logs(event, 0, 9_999, 56)
    assert len(event.ranges) == 1


def test_generic_too_many_is_not_a_range_error():
    event = FakeEvent(limit=1, error_factory=lambda: rpc_error(-32000, "too many open connections"))

    with pytest.raises(Web3RPCError):
        fetch_event_logs(event, 0, 9_999, 1)
    assert len(event.ranges) == 1


def test_message_only_rejection_shrinks_chunk():
    event = FakeEvent(limit=2_500, error_factory=lambda: rpc_error(-32000, "query returned more than 10000 results"))

    fetch_event_logs(event, 0, 4_999, 56)

    assert sizes(event) == [5_000, 2_500, 2_500]


def test_shrink_is_computed_from_the_rejected_window():
    event = FakeEvent(limit=500)

    fetch_event_logs(event, 0, 599, 56)

    assert sizes(event) == [600, 500, 100]


def test_learned_limit_is_reused_by_later_scans_of_the_chain():
    first = FakeEvent(limit=1_000)
    fetch_event_logs(first, 0, 9_999, 56)
    assert sizes(first)[:4] == [5_000, 2_500, 1_250, 625]

    second = FakeEvent(limit=1_000)
    fetch_event_logs(second, 0, 9_999, 56)
    assert sizes(second)[0] == 625 and all(size <= 625 for size in sizes(second))

    other_chain = FakeEvent(limit=10_000)
    fetch_event_logs(other_chain, 0, 9_999, 8453)
    assert sizes(other_chain) == [10_000]


def test_argument_filters_are_forwarded_and_empty_window_skipped():
    event = FakeEvent(limit=10_000)
    filters = {"from": "0x0000000000000000000000000000000000000000"}

    fetch_event_logs(event, 10, 20, 1, argument_filters=filters)
    assert event.filters == [filters]

    assert fetch_event_logs(event, 21, 20, 1) == []
    assert len(event.ranges) == 1


@pytest.mark.parametrize(
    "message, limit",
    [
        ("You can make eth_getLogs requests with up to a 10 block range", 10),
        ("Block range too large: maximum allowed is 50 blocks on your current plan", 50),
        ("eth_getLogs is limited to 0 - 50 blocks range", 50),
        ("ranges over 10000 blocks are not supported on free plan", 10000),
        ("getLogs request exceeded max allowed range", None),
    ],
)
def test_advertised_limit_parsing(message, limit):
    assert event_logs.advertised_range_limit(rpc_error(-32012, message)) == limit
