"""Exercise notification/voting entrypoints with no live RPC, Redis or Telegram."""

import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).parents[1]
INCENTIVES = Path("script/bots/votemarket_incentives/main.py")
EVENTS = Path("script/bots/votemarket/v2/votemarket.py")
VOTES = Path("script/bots/votemarket/v2/votes.py")


def load(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names for target in node.targets
        ):
            selected.append(node)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def test_campaign_scanner_only_polls_active_platforms():
    registry = SimpleNamespace(**{
        f"{protocol.upper()}_VOTEMARKET_V2": {42161: [protocol]}
        for protocol in ("curve", "balancer", "fxn", "pendle", "yb")
    })
    logs = Mock()
    logs.get_last_block_and_log.return_value = (100, 110)
    ns = {
        "ContractRegistry": registry,
        "to_checksum_address": lambda value: value,
        "logging": logging,
        "time": SimpleNamespace(sleep=lambda _: None),
        "get_last_workflow_run": lambda *args: None,
        "GithubLogService": lambda: logs,
        "OWNER": "fixture", "REPO": "fixture", "TOKEN": "fixture", "WORKFLOW_NAME": "fixture",
        "Common": SimpleNamespace(chains_ids_to_name={42161: "arbitrum"}),
        "web3_service": SimpleNamespace(w3={42161: object()}, get_w3=lambda _: object()),
        "get_block_range": lambda *args: (100, 110),
        "get_campaigns_created": AsyncMock(),
        "get_campaigns_increased": AsyncMock(),
    }
    load(EVENTS, {"get_all_platforms", "job"}, ns)
    platforms, names, endpoints = ns["get_all_platforms"]()
    assert list(endpoints.values()) == ["fxn", "curve", "yb"]
    assert set(names) == {"curve", "fxn", "yb"}
    asyncio.run(ns["job"]())
    assert [call.args[2] for call in ns["get_campaigns_created"].await_args_list] == ["curve", "fxn", "yb"]
    assert [call.args[2] for call in ns["get_campaigns_increased"].await_args_list] == ["curve", "fxn", "yb"]


@pytest.mark.parametrize("protocol", ["balancer", "pendle"])
def test_retired_incentive_never_fetches_prices_or_sends_even_with_old_cached_identity(protocol):
    ns = {
        "get_token_price": Mock(side_effect=AssertionError("unexpected price lookup")),
        "send_telegram_message": Mock(side_effect=AssertionError("unexpected notification")),
    }
    load(INCENTIVES, {"ACTIVE_PROTOCOLS", "get_gauge_info", "send_incentive_notification"}, ns)
    assert ns["send_incentive_notification"](
        {"gauge": "0xold", "reward": "0xtoken"},
        {"0xold": {"protocol": protocol, "name": "Retired"}}, {},
    ) is False
    ns["get_token_price"].assert_not_called()
    ns["send_telegram_message"].assert_not_called()


def test_incentive_job_preserves_active_external_rewards_and_retries_unknown_or_failed_delivery():
    gauges = {
        f"0x{protocol}": {"protocol": protocol, "name": protocol}
        for protocol in ("curve", "balancer", "fxn", "pendle", "yb")
    }
    incentives = [
        {"id": i, "gauge": f"0x{protocol}", "fromChainId": 1}
        for i, protocol in enumerate(("balancer", "pendle", "unknown", "curve", "fxn", "yb"), 1)
    ]
    sent = Mock(side_effect=[True, False, True])
    saved = Mock()
    ns = {
        "logging": logging,
        "time": SimpleNamespace(sleep=lambda _: None),
        "load_seen_ids": lambda: {0},
        "load_gauge_data": lambda: gauges,
        "load_gauge_tvls": lambda: {},
        "fetch_incentives": lambda: incentives,
        "send_incentive_notification": sent,
        "save_seen_ids": saved,
    }
    load(INCENTIVES, {"ACTIVE_PROTOCOLS", "get_gauge_info", "job"}, ns)
    ns["job"]()
    assert [call.args[0]["gauge"] for call in sent.call_args_list] == ["0xcurve", "0xfxn", "0xyb"]
    saved.assert_called_once_with({0, 4, 6})


def test_active_gauge_cache_does_not_reuse_previous_retired_catalog():
    keys = {}
    load(INCENTIVES, {"REDIS_KEY_GAUGES", "REDIS_KEY_GAUGES_STALE"}, keys)
    old_key = "tg-bot:votemarket-incentives:gauge_names:hub-v1"
    old = {"0xretired": {"protocol": "balancer", "name": "Retired"}}
    values = {old_key: json.dumps(old), old_key + ":last-complete": json.dumps(old)}
    redis = Mock(get=Mock(side_effect=values.get))
    fresh = {"0xcurve": {"protocol": "curve", "name": "Curve"}}
    ns = {**keys, "json": json, "logging": logging, "get_redis_client": lambda: redis, "fetch_gauge_data": Mock(return_value=fresh)}
    load(INCENTIVES, {"load_gauge_data"}, ns)
    assert ns["load_gauge_data"]() == fresh
    ns["fetch_gauge_data"].assert_called_once()
    redis.delete.assert_not_called()
    assert values[old_key] == json.dumps(old)


@pytest.mark.skipif(not (ROOT / VOTES).exists(), reason="Voting execution belongs to automation-jobs")
def test_vote_job_only_schedules_active_markets_and_rejects_retired_direct_calls():
    registry = SimpleNamespace(**{
        name: {1: "0x" + protocol}
        for name, protocol in (("CRV_GAUGE_CONTROLLER", "curve"), ("FXN_GAUGE_CONTROLLER", "fxn"), ("YB_GAUGE_CONTROLLER", "yb"), ("BALANCER_GAUGE_CONTROLLER", "balancer"))
    })
    ns = {"ContractRegistry": registry, "log": Mock(), "log_successful_tx": Mock(), "logging": logging,
          "process_protocol": AsyncMock()}
    load(VOTES, {"PROTOCOLS", "main"}, ns)
    asyncio.run(ns["main"]())
    assert {call.args[0] for call in ns["process_protocol"].await_args_list} == {"curve", "fxn", "yb"}
    ns["web3_service"] = Mock()
    load(VOTES, {"process_protocol"}, ns)
    for protocol in ("balancer", "pendle"):
        with pytest.raises(ValueError, match="Inactive voting protocol"):
            asyncio.run(ns["process_protocol"](protocol, "0xretired", 10000, "abi"))
    ns["web3_service"].get_w3.assert_not_called()


def test_active_direct_reward_keeps_notification_content_and_delivery_result():
    telegram = Mock(return_value=True)
    ns = {
        "get_token_price": Mock(return_value=(0, 100.0)),
        "send_telegram_message": telegram,
        "format_amount": lambda *args: "100",
        "SECONDS_PER_DAY": 86400,
        "format_date_short": lambda _: "01 Jan",
        "get_source_type": lambda *args: ("Direct Rewards", "🎯", "Ethereum"),
        "PROTOCOL_LABELS": {"curve": "Curve", "fxn": "F(x)", "yb": "YieldBasis"},
        "calculate_apr": lambda *args: 0.2,
        "format_tvl": lambda _: "$1K",
        "GlobalConstants": SimpleNamespace(BOT_VOTEMARKET_API_KEY="fixture", VOTEMARKET_CHANNEL_ID="fixture"),
        "replace_double_quotes_with_single": lambda value: value,
        "logging": logging,
    }
    load(INCENTIVES, {"ACTIVE_PROTOCOLS", "get_gauge_info", "send_incentive_notification"}, ns)
    incentive = {"id": 7, "gauge": "0xcurve", "vault": "0xvault", "reward": "0xtoken", "rewardSymbol": "USDC", "amount": "100000000", "rewardDecimals": 6, "duration": 86400, "fromChainId": 1}
    gauges = {"0xcurve": {"protocol": "curve", "name": "Curve pool"}}
    assert ns["send_incentive_notification"](incentive, gauges, {"0xcurve": 1000}) is True
    content = telegram.call_args.args[2]
    assert "Direct Rewards | Ethereum" in content
    assert "Gauge: Curve pool" in content
    assert "Reward: 100 USDC (~$100)" in content
    assert "protocol=curve&vault=1-0xvault" in content
    telegram.return_value = False
    assert ns["send_incentive_notification"](incentive, gauges, {}) is False
