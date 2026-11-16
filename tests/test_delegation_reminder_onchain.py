"""Convex on-chain watcher of the delegation reminder bot (ENG-2003).

Covers the pure selection logic (DAO proposal filtering, reminder windows)
and the delivery/idempotence semantics of the on-chain alert path. The DAO
platforms allow overlapping proposals and out-of-order scheduling
(startTime can be arbitrarily far in the future), so the filter must be fed
ALL pids and never stop at the first settled one.
"""

import asyncio
import html
from unittest.mock import MagicMock

import pytest

from bots.snapshot import delegation_reminder as dr
from shared.constants import ContractRegistry

NOW = 1_784_400_000
DAY = 86_400

PLATFORM = {
    "key": "convex-curve-dao",
    "kind": "dao",
    "proto": "Curve",
    "name": "Convex Curve DAO (on-chain)",
    "address": "0xE645F3d7b04BFFD67F5Ad4457c08E0C8FE1ddB89",
    "abi": "convex_dao_voting",
    "path": "dao/curve",
}


def test_gauge_platforms_use_replacement_mainnet_deployments():
    gauge_platforms = {
        platform["key"]: platform["address"]
        for platform in dr.CONVEX_ONCHAIN_PLATFORMS
        if platform["kind"] == "gauge"
    }
    assert gauge_platforms == {
        "convex-curve": "0x64D9B5AC386B70af9EDCD20A58cE9262D2EAC278",
        "convex-fxn": "0xDcEa673B021f1f431E7D0Ec70a63bF8DcB6d44E6",
    }


def _raw(pid, start, end, vote_type=0, external_id=1450):
    return (pid, (start, end, 227, vote_type, external_id))


# --------------------------------------------------------------------------
# _select_dao_proposals
# --------------------------------------------------------------------------

def test_select_keeps_overlapping_active_proposals():
    raw = [
        _raw(2, NOW - 2 * DAY, NOW + 1 * DAY, external_id=1453),
        _raw(3, NOW - 1 * DAY, NOW + 2 * DAY, external_id=1454),
    ]
    selected = dr._select_dao_proposals(PLATFORM, raw, NOW)
    assert [p["pid"] for p in selected] == [2, 3]
    assert selected[1]["id"] == "convex-curve-dao-3"
    assert selected[1]["platform_kind"] == "dao"
    assert selected[1]["start"] == NOW - 1 * DAY
    assert selected[1]["end"] == NOW + 2 * DAY
    assert selected[1]["title"] == "[Curve] Ownership DAO Vote ID: 1454 (on-chain #3)"
    assert selected[1]["link"] == "https://www.convexfinance.com/vote/dao/curve/3"


def test_select_skips_force_ended_and_settled():
    raw = [
        _raw(0, 0, 0),  # force-ended: timestamps zeroed
        _raw(1, NOW - 20 * DAY, NOW - 10 * DAY),  # settled long ago
        _raw(2, NOW - 8 * DAY, NOW - dr.DAO_SETTLED_MARGIN + 60),  # inside margin
    ]
    selected = dr._select_dao_proposals(PLATFORM, raw, NOW)
    assert [p["pid"] for p in selected] == [2]


def test_select_non_monotonic_scheduling():
    # An early pid scheduled far in the future must survive even when every
    # later pid is already settled — the Codex P1 on the backward-scan plan.
    raw = [
        _raw(4, NOW + 30 * DAY, NOW + 33 * DAY),  # scheduled months ahead
        _raw(5, NOW - 20 * DAY, NOW - 15 * DAY),  # settled
        _raw(6, NOW - 15 * DAY, NOW - 10 * DAY),  # settled
    ]
    selected = dr._select_dao_proposals(PLATFORM, raw, NOW)
    assert [p["pid"] for p in selected] == [4]


def test_select_title_variants():
    raw = [
        _raw(7, NOW, NOW + DAY, vote_type=1, external_id=99),
        _raw(8, NOW, NOW + DAY, vote_type=0, external_id=0),
    ]
    selected = dr._select_dao_proposals(PLATFORM, raw, NOW)
    assert selected[0]["title"] == "[Curve] Parameter DAO Vote ID: 99 (on-chain #7)"
    # external id 0 means "no external reference" -> no "ID:" segment
    assert selected[1]["title"] == "[Curve] Ownership DAO Vote (on-chain #8)"


# --------------------------------------------------------------------------
# _is_in_reminder_window
# --------------------------------------------------------------------------

def test_reminder_window_bounds():
    last_run, offset = NOW - 900, 24 * 3600
    # Inclusive upper bound: end exactly at now+offset fires on THIS run...
    assert dr._is_in_reminder_window(NOW + offset, last_run, NOW, offset)
    # ...and the next run (last_run == now) must skip it (exclusive lower bound)
    assert not dr._is_in_reminder_window(NOW + offset, NOW, NOW + 900, offset)
    assert not dr._is_in_reminder_window(last_run + offset, last_run, NOW, offset)
    # A proposal that already ended never gets a reminder
    assert not dr._is_in_reminder_window(NOW - 60, last_run, NOW, offset)
    assert dr._is_in_reminder_window(NOW + offset - 60, last_run, NOW, offset)


# --------------------------------------------------------------------------
# process_reminder_proposal — delivery + idempotence (on-chain path)
# --------------------------------------------------------------------------

ONCHAIN_PROPOSAL = {
    "id": "convex-curve-dao-3",
    "pid": 3,
    "platform_address": PLATFORM["address"],
    "platform_kind": "dao",
    "title": "[Curve] Ownership DAO Vote ID: 1454 (on-chain #3)",
    "start": NOW - DAY,
    "end": NOW + 2 * DAY,
    "space": {"id": "convex-curve-dao", "name": "Convex Curve DAO (on-chain)"},
    "link": "https://www.convexfinance.com/vote/dao/curve/3",
}

SAFE_LINK_TO_PROPOSAL_3 = (
    "https://app.safe.global/apps/open"
    "?safe=eth:0xbB06fEFB8f23A7c60C93fe20464DB6687C51955f"
    "&appUrl=https%3A%2F%2Fwww.convexfinance.com%2Fvote%2Fdao%2Fcurve%2F3"
)
# As it appears in the message: parse_mode=html requires the "&" escaped
SAFE_HREF_TO_PROPOSAL_3 = html.escape(SAFE_LINK_TO_PROPOSAL_3, quote=False)

CONFIG_24H = dr.ALERT_CONFIGS[0]


def _run_reminder(monkeypatch, *, voted, delivered, redis_get=None):
    redis_client = MagicMock()
    redis_client.get.return_value = redis_get
    send = MagicMock(return_value=delivered)
    monkeypatch.setattr(dr, "send_telegram_message", send)
    monkeypatch.setattr(dr, "_has_delegate_voted_onchain_sync", lambda p: voted)
    result = asyncio.run(
        dr.process_reminder_proposal(
            None, ONCHAIN_PROPOSAL, asyncio.Semaphore(1), CONFIG_24H,
            redis_client=redis_client,
        )
    )
    return result, redis_client, send


SENT_KEY = "delegation_reminder_onchain_sent:convex-curve-dao-3:24h"


def test_reminder_delivered_sets_idempotence_key(monkeypatch):
    result, redis_client, send = _run_reminder(
        monkeypatch, voted=False, delivered=True
    )
    assert result is True
    send.assert_called_once()
    message = send.call_args.args[2]
    assert "[Curve] Ownership DAO Vote ID: 1454 (on-chain #3)" in message
    # The delegate votes from the Safe: the message links into the Safe App,
    # not to the bare Convex page (ENG-2100)
    assert f"href='{SAFE_HREF_TO_PROPOSAL_3}'" in message
    redis_client.set.assert_any_call(
        f"{SENT_KEY}:inflight", 1, nx=True, ex=300
    )
    redis_client.set.assert_any_call(SENT_KEY, 1, ex=dr.ONCHAIN_SENT_TTL)


def test_reminder_delivery_failure_keeps_retry_state(monkeypatch):
    result, redis_client, send = _run_reminder(
        monkeypatch, voted=False, delivered=False
    )
    assert result is False
    send.assert_called_once()
    # Only the in-flight lock was taken; the sent key must NOT exist so the
    # frozen watermark retries this reminder next run
    sent_calls = [c for c in redis_client.set.call_args_list if c.args[0] == SENT_KEY]
    assert sent_calls == []


def test_reminder_already_sent_is_skipped(monkeypatch):
    result, redis_client, send = _run_reminder(
        monkeypatch, voted=False, delivered=True, redis_get=b"1"
    )
    assert result is True
    send.assert_not_called()


def _run_contender(monkeypatch, sent_key_reads):
    # A concurrent run holds the in-flight lock (SET NX returns falsy)
    redis_client = MagicMock()
    redis_client.get.side_effect = sent_key_reads
    redis_client.set.return_value = None  # NX lock not acquired
    send = MagicMock(return_value=True)
    monkeypatch.setattr(dr, "send_telegram_message", send)
    monkeypatch.setattr(dr, "_has_delegate_voted_onchain_sync", lambda p: False)
    result = asyncio.run(
        dr.process_reminder_proposal(
            None, ONCHAIN_PROPOSAL, asyncio.Semaphore(1), CONFIG_24H,
            redis_client=redis_client,
        )
    )
    return result, send


def test_reminder_inflight_unconfirmed_reports_failure(monkeypatch):
    # The lock holder has not confirmed delivery: the contender must not
    # send, and must NOT report success either — advancing the watermark
    # here would lose the window if the holder crashes
    result, send = _run_contender(monkeypatch, sent_key_reads=[None, None])
    assert result is False
    send.assert_not_called()


def test_reminder_inflight_confirmed_reports_success(monkeypatch):
    # The lock holder finished delivering between our two reads
    result, send = _run_contender(monkeypatch, sent_key_reads=[None, b"1"])
    assert result is True
    send.assert_not_called()


def test_reminder_voted_sends_nothing(monkeypatch):
    result, redis_client, send = _run_reminder(
        monkeypatch, voted=True, delivered=True
    )
    assert result is True
    send.assert_not_called()
    redis_client.set.assert_not_called()


def test_vote_check_error_still_reminds(monkeypatch):
    # Better a spurious reminder than a silently missed one
    redis_client = MagicMock()
    redis_client.get.return_value = None
    send = MagicMock(return_value=True)
    monkeypatch.setattr(dr, "send_telegram_message", send)

    def _boom(proposal):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(dr, "_has_delegate_voted_onchain_sync", _boom)
    result = asyncio.run(
        dr.process_reminder_proposal(
            None, ONCHAIN_PROPOSAL, asyncio.Semaphore(1), CONFIG_24H,
            redis_client=redis_client,
        )
    )
    assert result is True
    send.assert_called_once()


# --------------------------------------------------------------------------
# _safe_app_link — vote page opened inside the delegate Safe (ENG-2100)
# --------------------------------------------------------------------------

def test_safe_app_link_encodes_the_whole_vote_page():
    assert dr._safe_app_link(ONCHAIN_PROPOSAL["link"]) == SAFE_LINK_TO_PROPOSAL_3
    # Fully encoded, "/" included: any raw "?"/"&" in a wrapped page URL
    # would be read as a query param of app.safe.global instead of reaching
    # the Convex app inside the Safe iframe
    assert "/" not in dr._safe_app_link(ONCHAIN_PROPOSAL["link"]).split("appUrl=")[1]


def test_safe_app_link_targets_the_onchain_delegate():
    link = dr._safe_app_link(dr.CONVEX_VOTE_URL)
    assert f"safe=eth:{ContractRegistry.DELEGATION_ONCHAIN[1]}" in link
    assert link.endswith("appUrl=https%3A%2F%2Fwww.convexfinance.com%2Fvote")


def test_new_proposal_alert_links_into_the_safe(monkeypatch):
    send = MagicMock(return_value=True)
    monkeypatch.setattr(dr, "send_telegram_message", send)
    assert asyncio.run(dr.process_new_proposal_alert(ONCHAIN_PROPOSAL)) is True
    message = send.call_args.args[2]
    assert f"href='{SAFE_HREF_TO_PROPOSAL_3}'" in message
    # The bare Convex page never appears on its own: it only survives
    # percent-encoded inside appUrl
    assert f"'{ONCHAIN_PROPOSAL['link']}'" not in message


# --------------------------------------------------------------------------
# _advance_watermark — monotonic, atomic with fallback
# --------------------------------------------------------------------------

def test_advance_watermark_uses_atomic_cas():
    redis_client = MagicMock()
    dr._advance_watermark(redis_client, "k", 123)
    redis_client.eval.assert_called_once_with(dr._WATERMARK_CAS_LUA, 1, "k", 123)
    redis_client.set.assert_not_called()


def test_advance_watermark_fallback_is_monotonic():
    redis_client = MagicMock()
    redis_client.eval.side_effect = RuntimeError("EVAL disabled")
    redis_client.get.return_value = b"200"
    dr._advance_watermark(redis_client, "k", 123)
    redis_client.set.assert_not_called()  # never roll back

    redis_client.get.return_value = b"100"
    dr._advance_watermark(redis_client, "k", 123)
    redis_client.set.assert_called_once_with("k", 123)


# --------------------------------------------------------------------------
# handle_convex_onchain_flow — watermark contract
# --------------------------------------------------------------------------

def test_flow_fetch_failure_returns_false(monkeypatch):
    async def _boom():
        raise RuntimeError("rpc down")

    monkeypatch.setattr(dr, "fetch_convex_onchain_proposals", _boom)
    result = asyncio.run(
        dr.handle_convex_onchain_flow(None, MagicMock(), NOW - 900, NOW, asyncio.Semaphore(1))
    )
    assert result is False


def test_flow_delivery_failure_returns_false(monkeypatch):
    async def _proposals():
        return [ONCHAIN_PROPOSAL], True

    redis_client = MagicMock()
    redis_client.sismember.return_value = True  # new-proposal alert already sent
    redis_client.get.return_value = None
    monkeypatch.setattr(dr, "fetch_convex_onchain_proposals", _proposals)
    monkeypatch.setattr(dr, "_has_delegate_voted_onchain_sync", lambda p: False)
    monkeypatch.setattr(dr, "send_telegram_message", MagicMock(return_value=False))

    # Make the 24h window match the proposal end
    last_run = ONCHAIN_PROPOSAL["end"] - 24 * 3600 - 60
    now = ONCHAIN_PROPOSAL["end"] - 24 * 3600 + 60
    result = asyncio.run(
        dr.handle_convex_onchain_flow(None, redis_client, last_run, now, asyncio.Semaphore(1))
    )
    assert result is False


def test_flow_partial_fetch_still_alerts_but_freezes_watermark(monkeypatch):
    # One broken platform must not block the healthy ones (Codex P1):
    # the fetched proposals are processed, but the flow reports failure so
    # the watermark stays frozen and the broken platform is retried
    async def _proposals():
        return [ONCHAIN_PROPOSAL], False

    redis_client = MagicMock()
    redis_client.sismember.return_value = False
    redis_client.get.return_value = None
    send = MagicMock(return_value=True)
    monkeypatch.setattr(dr, "fetch_convex_onchain_proposals", _proposals)
    monkeypatch.setattr(dr, "_has_delegate_voted_onchain_sync", lambda p: False)
    monkeypatch.setattr(dr, "send_telegram_message", send)

    now = ONCHAIN_PROPOSAL["start"] + 60  # active -> new-proposal alert fires
    result = asyncio.run(
        dr.handle_convex_onchain_flow(None, redis_client, now - 900, now, asyncio.Semaphore(1))
    )
    assert result is False
    send.assert_called_once()  # the healthy platform's alert went out


def test_fetch_isolates_platform_failures(monkeypatch):
    # convex-curve (gauge) responds, one DAO platform raises, the rest are
    # empty -> the gauge proposal is returned and all_ok is False
    gauge_addr = dr.CONVEX_ONCHAIN_PLATFORMS[0]["address"]
    frax_addr = next(
        p["address"] for p in dr.CONVEX_ONCHAIN_PLATFORMS if p["key"] == "convex-frax-dao"
    )

    def fake_load_contract(w3, addr, abi):
        contract = MagicMock()
        if addr == frax_addr:
            contract.functions.proposalCount.return_value.call.side_effect = (
                RuntimeError("bad contract")
            )
        elif addr == gauge_addr:
            contract.functions.proposalCount.return_value.call.return_value = 1
            contract.functions.proposals.return_value.call.return_value = (
                NOW - DAY, NOW + DAY, 228,
            )
        else:
            contract.functions.proposalCount.return_value.call.return_value = 0
        return contract

    _use_rotation(monkeypatch)
    monkeypatch.setattr(dr, "load_contract_w3", fake_load_contract)

    proposals, all_ok = dr._fetch_convex_onchain_proposals_sync()
    assert all_ok is False
    assert [p["id"] for p in proposals] == ["convex-curve-0"]
    # Gauge pages have a single active proposal -> no pid path segment
    assert proposals[0]["link"] == "https://www.convexfinance.com/vote/weights/curve"


# --------------------------------------------------------------------------
# Platform read retries + endpoint rotation (ENG-2073)
# --------------------------------------------------------------------------

GAUGE_ADDRESS = dr.CONVEX_ONCHAIN_PLATFORMS[0]["address"]
FRAX_ADDRESS = next(
    p["address"] for p in dr.CONVEX_ONCHAIN_PLATFORMS if p["key"] == "convex-frax-dao"
)

# Stand-ins for the real chain-1 list (alchemy -> mevblocker -> drpc -> ...);
# no request is ever made, only the head of the rotated list is asserted on.
FAKE_ENDPOINTS = [
    "https://rpc-a.test/some-key",
    "https://rpc-b.test",
    "https://rpc-c.test",
]


def _use_rotation(monkeypatch, endpoints=None):
    """Swaps the process-wide rotation for one over disposable endpoints."""
    rotation = dr._EndpointRotation(endpoints=endpoints or FAKE_ENDPOINTS)
    monkeypatch.setattr(dr, "_ONCHAIN_RPC", rotation)
    monkeypatch.setattr(dr, "ONCHAIN_READ_BACKOFF_SECONDS", 0)
    # The fake endpoints answer nothing; the empty-platform guard has its own
    # tests below.
    monkeypatch.setattr(dr, "_has_contract_code", lambda w3, address: True)
    return rotation


def _healthy_gauge(contract):
    contract.functions.proposalCount.return_value.call.return_value = 1
    contract.functions.proposals.return_value.call.return_value = (
        NOW - DAY, NOW + DAY, 228,
    )


def test_rotation_promotes_next_endpoint_and_keeps_the_others_behind_it():
    rotation = dr._EndpointRotation(endpoints=FAKE_ENDPOINTS)
    assert rotation.web3().provider.endpoint_uri == FAKE_ENDPOINTS[0]
    assert rotation.host() == "rpc-a.test"  # host only: URLs carry API keys

    rotation.rotate()
    provider = rotation.web3().provider
    assert provider.endpoint_uri == FAKE_ENDPOINTS[1]
    # The demoted endpoint stays in the list: transport-level failover of
    # FallbackHTTPProvider must keep working behind the new head
    assert provider._endpoints == [
        FAKE_ENDPOINTS[1], FAKE_ENDPOINTS[2], FAKE_ENDPOINTS[0],
    ]

    for _ in range(len(FAKE_ENDPOINTS) - 1):
        rotation.rotate()
    assert rotation.web3().provider.endpoint_uri == FAKE_ENDPOINTS[0]  # wraps


def test_read_rotates_away_from_an_endpoint_answering_json_rpc_errors(monkeypatch):
    # The 2026-08-05 failure mode: HTTP 200 carrying {'code': -32603}. The
    # provider sees a successful request and never rotates by itself, so the
    # bot must move the head — retrying the same node would fail identically.
    rotation = _use_rotation(monkeypatch)
    heads = []

    def fake_load_contract(w3, addr, abi):
        contract = MagicMock()
        heads.append(w3.provider.endpoint_uri)
        if w3.provider.endpoint_uri == FAKE_ENDPOINTS[0]:
            contract.functions.proposalCount.return_value.call.side_effect = (
                RuntimeError("{'code': -32603, 'message': 'Internal error'}")
            )
        elif addr == GAUGE_ADDRESS:
            _healthy_gauge(contract)
        else:
            contract.functions.proposalCount.return_value.call.return_value = 0
        return contract

    monkeypatch.setattr(dr, "load_contract_w3", fake_load_contract)

    proposals, all_ok = dr._fetch_convex_onchain_proposals_sync()
    assert all_ok is True
    assert [p["id"] for p in proposals] == ["convex-curve-0"]
    # First read on the broken head, retry on the next one...
    assert heads[:2] == [FAKE_ENDPOINTS[0], FAKE_ENDPOINTS[1]]
    # ...and the rotation is sticky: the remaining platforms never go back
    assert set(heads[1:]) == {FAKE_ENDPOINTS[1]}
    assert rotation.host() == "rpc-b.test"


def test_zero_proposals_from_a_codeless_endpoint_is_a_failure(monkeypatch):
    # A node serving state without this contract answers proposalCount() = 0
    # instead of failing. Believed, that zero advances the watermark and
    # silently burns the run's reminder windows — so it must be a read
    # failure: retried, rotated away from, and watermark frozen.
    _use_rotation(monkeypatch)
    monkeypatch.setattr(dr, "_has_contract_code", lambda w3, address: False)

    def fake_load_contract(w3, addr, abi):
        contract = MagicMock()
        contract.functions.proposalCount.return_value.call.return_value = 0
        return contract

    monkeypatch.setattr(dr, "load_contract_w3", fake_load_contract)

    proposals, all_ok = dr._fetch_convex_onchain_proposals_sync()
    assert (proposals, all_ok) == ([], False)


def test_zero_proposals_with_code_present_is_a_legitimate_empty_platform(monkeypatch):
    # A platform that genuinely has no proposal yet must stay a clean empty
    # read — not a failure that would freeze the watermark forever.
    _use_rotation(monkeypatch)
    code_reads = []

    def fake_has_code(w3, address):
        code_reads.append(address)
        return True

    def fake_load_contract(w3, addr, abi):
        contract = MagicMock()
        contract.functions.proposalCount.return_value.call.return_value = 0
        return contract

    monkeypatch.setattr(dr, "_has_contract_code", fake_has_code)
    monkeypatch.setattr(dr, "load_contract_w3", fake_load_contract)

    proposals, all_ok = dr._fetch_convex_onchain_proposals_sync()
    assert (proposals, all_ok) == ([], True)
    # The extra eth_getCode is only paid on the zero path
    assert len(code_reads) == len(dr.CONVEX_ONCHAIN_PLATFORMS)


def test_vote_check_reuses_the_rotated_endpoint(monkeypatch):
    # The vote check runs right after the reads; it must inherit the endpoint
    # the poll settled on instead of starting again on the broken one.
    rotation = _use_rotation(monkeypatch)
    rotation.rotate()
    seen = {}

    def fake_load_contract(w3, addr, abi):
        seen["head"] = w3.provider.endpoint_uri
        contract = MagicMock()
        contract.functions.getVote.return_value.call.return_value = (True, 0, 0, 0, 0)
        return contract

    monkeypatch.setattr(dr, "load_contract_w3", fake_load_contract)

    assert dr._has_delegate_voted_onchain_sync(ONCHAIN_PROPOSAL) is True
    assert seen["head"] == FAKE_ENDPOINTS[1]


def test_platform_read_retries_transient_failure(monkeypatch):
    # The 2026-08-05 incident: the provider answers -32603 on one poll and is
    # healthy on the next. A retry inside the same run keeps the proposal in
    # the alert pipeline instead of waiting ~40 min for the next GHA run.
    reads = []

    def fake_load_contract(w3, addr, abi):
        contract = MagicMock()
        if addr == GAUGE_ADDRESS:
            reads.append(addr)
            if len(reads) == 1:
                contract.functions.proposalCount.return_value.call.side_effect = (
                    RuntimeError("{'code': -32603, 'message': 'Internal error'}")
                )
            else:
                _healthy_gauge(contract)
        else:
            contract.functions.proposalCount.return_value.call.return_value = 0
        return contract

    _use_rotation(monkeypatch)
    monkeypatch.setattr(dr, "load_contract_w3", fake_load_contract)

    proposals, all_ok = dr._fetch_convex_onchain_proposals_sync()
    assert all_ok is True  # recovered -> the watermark may advance
    assert [p["id"] for p in proposals] == ["convex-curve-0"]
    assert len(reads) == 2


def test_platform_read_gives_up_after_max_attempts(monkeypatch):
    # A permanently broken platform must cost only its own attempts: the
    # healthy platforms still alert, and all_ok=False freezes the watermark.
    attempts = []

    def fake_load_contract(w3, addr, abi):
        contract = MagicMock()
        if addr == FRAX_ADDRESS:
            attempts.append(addr)
            contract.functions.proposalCount.return_value.call.side_effect = (
                RuntimeError("bad contract")
            )
        elif addr == GAUGE_ADDRESS:
            _healthy_gauge(contract)
        else:
            contract.functions.proposalCount.return_value.call.return_value = 0
        return contract

    _use_rotation(monkeypatch)
    monkeypatch.setattr(dr, "load_contract_w3", fake_load_contract)

    proposals, all_ok = dr._fetch_convex_onchain_proposals_sync()
    assert all_ok is False
    assert [p["id"] for p in proposals] == ["convex-curve-0"]
    assert len(attempts) == dr.ONCHAIN_READ_ATTEMPTS


def test_total_outage_stays_bounded_and_cycles_endpoints(monkeypatch):
    # Worst case: every endpoint is down for every platform. The poll must
    # keep sweeping the endpoint list (never re-pinning to a single node) and
    # stay well inside the GHA run budget.
    rotation = _use_rotation(monkeypatch)
    monkeypatch.setattr(dr, "ONCHAIN_READ_BACKOFF_SECONDS", 1.0)
    sleeps = []
    heads = []

    def fake_load_contract(w3, addr, abi):
        heads.append(w3.provider.endpoint_uri)
        contract = MagicMock()
        contract.functions.proposalCount.return_value.call.side_effect = RuntimeError(
            "rpc down"
        )
        return contract

    monkeypatch.setattr(dr.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(dr, "load_contract_w3", fake_load_contract)

    proposals, all_ok = dr._fetch_convex_onchain_proposals_sync()
    platform_count = len(dr.CONVEX_ONCHAIN_PLATFORMS)
    retries = dr.ONCHAIN_READ_ATTEMPTS - 1
    assert (proposals, all_ok) == ([], False)
    # Every attempt moved the head one step down the list, wrapping around
    assert len(heads) == platform_count * dr.ONCHAIN_READ_ATTEMPTS
    assert heads == [
        FAKE_ENDPOINTS[i % len(FAKE_ENDPOINTS)] for i in range(len(heads))
    ]
    assert set(heads) == set(FAKE_ENDPOINTS)  # no endpoint left untried
    assert rotation.web3().provider.endpoint_uri == FAKE_ENDPOINTS[
        len(heads) % len(FAKE_ENDPOINTS)
    ]
    expected_backoff = [
        dr.ONCHAIN_READ_BACKOFF_SECONDS * (2 ** attempt) for attempt in range(retries)
    ]
    assert sleeps == expected_backoff * platform_count
    assert sum(sleeps) <= 30  # keep the poll well inside the GHA run budget
