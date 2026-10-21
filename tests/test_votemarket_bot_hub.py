"""Exercise real consumer functions with no imports that initialize RPC or Telegram."""

import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from shared.services.stakedao_api import (
    CAPABILITIES,
    parse_protocols,
    validate_collection,
)

ROOT = Path(__file__).parents[1] / "script"


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    assert len(selected) == len(names)
    exec(
        compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"),
        namespace,
    )
    return namespace


def catalog_namespace():
    namespace = {
        "requests": SimpleNamespace(get=Mock()),
        "json": json,
        "logging": logging,
        "VOTEMARKET_API_BASE": "https://hub.test/v1/votemarket",
        "parse_protocols": parse_protocols,
        "validate_collection": validate_collection,
        "ACTIVE_PROTOCOLS": frozenset(("curve", "fxn", "yb")),
        "REDIS_KEY_GAUGES": "current",
        "REDIS_KEY_GAUGES_STALE": "stale",
    }
    return functions(
        Path("bots/votemarket_incentives/main.py"),
        {"fetch_gauge_data", "load_gauge_data", "get_gauge_info"},
        namespace,
    )


def protocol(name, status="active"):
    return {
        "id": name,
        "status": status,
        "capabilities": {key: True for key in CAPABILITIES},
    }


def response(data):
    return Mock(json=Mock(return_value=data))


@pytest.mark.parametrize("retired_status", ["deprecated", "active"])
def test_gauge_map_only_fetches_active_markets_and_never_guesses_unknown_identity(retired_status):
    ns = catalog_namespace()
    ns["requests"].get.side_effect = [
        response([protocol("curve"), protocol("balancer", retired_status), protocol("pendle", retired_status)]),
        response({"gauges": [{"gauge": "0xAAA", "name": "Curve"}]}),
    ]
    data = ns["fetch_gauge_data"]()
    assert ns["get_gauge_info"]("0xAAA", data) == ("Curve", "curve")
    with pytest.raises(ValueError, match="deferred"):
        ns["get_gauge_info"]("0xUNKNOWN", data)
    assert [call.args[0] for call in ns["requests"].get.call_args_list] == [
        "https://hub.test/v1/votemarket/protocols",
        "https://hub.test/v1/votemarket/curve/gauges",
    ]


def test_partial_catalog_is_never_cached_and_stale_complete_catalog_is_retained():
    ns = catalog_namespace()
    stale = {"0xold": {"name": "Curve", "protocol": "curve"}}
    redis = Mock(get=Mock(side_effect=[None, json.dumps(stale)]))
    ns["get_redis_client"] = lambda: redis
    failed = response({"error": "offline"})
    failed.raise_for_status.side_effect = requests.HTTPError("503")
    ns["requests"].get.side_effect = [
        response([protocol("curve"), protocol("fxn")]),
        response({"gauges": [{"gauge": "0xAAA", "name": "Curve"}]}),
        failed,
    ]
    assert ns["load_gauge_data"]() == stale
    redis.set.assert_not_called()
    redis.setex.assert_not_called()


def test_missing_catalog_aborts_before_any_notification():
    ns = catalog_namespace()
    ns["get_redis_client"] = lambda: Mock(get=Mock(return_value=None))
    ns["fetch_gauge_data"] = Mock(side_effect=requests.HTTPError("503"))
    with pytest.raises(requests.HTTPError):
        ns["load_gauge_data"]()


def test_complete_catalog_updates_both_cache_entries():
    ns = catalog_namespace()
    redis = Mock(get=Mock(return_value=None))
    ns["get_redis_client"] = lambda: redis
    complete = {"0xaaa": {"name": "Curve", "protocol": "curve"}}
    ns["fetch_gauge_data"] = Mock(return_value=complete)
    assert ns["load_gauge_data"]() == complete
    redis.set.assert_called_once_with("stale", json.dumps(complete))
    redis.setex.assert_called_once_with("current", 86400, json.dumps(complete))


def test_one_protocol_failure_does_not_block_following_protocol_but_fails_run():
    calls = []

    async def created(*args):
        calls.append(args[2])
        if args[2] == "retired":
            raise RuntimeError("fixture 404")

    logs = Mock()
    logs.get_last_block_and_log.return_value = (1, 2)
    ns = {
        "logging": logging,
        "time": SimpleNamespace(sleep=lambda _: None),
        "get_all_platforms": lambda: ([{1: ["retired", "active"]}], {}, {}),
        "get_last_workflow_run": lambda *args: None,
        "GithubLogService": lambda: logs,
        "OWNER": "fixture",
        "REPO": "fixture",
        "TOKEN": "fixture",
        "WORKFLOW_NAME": "fixture",
        "Common": SimpleNamespace(chains_ids_to_name={1: "ethereum"}),
        "web3_service": SimpleNamespace(w3={1: object()}, get_w3=lambda _: object()),
        "get_block_range": lambda *args: (1, 2),
        "get_campaigns_created": created,
        "get_campaigns_increased": AsyncMock(),
    }
    functions(Path("bots/votemarket/v2/votemarket.py"), {"job"}, ns)
    with pytest.raises(RuntimeError, match="processing failed"):
        asyncio.run(ns["job"]())
    assert calls == ["retired", "active"]
    assert ns["get_campaigns_increased"].await_count == 1
