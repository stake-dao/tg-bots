import logging
import re

import pytest

from bots.utils.github import DEFERRED_MODE_MARKER, GithubLogService
from shared import bot_runner

MARKER = re.compile(r"Chain (\d+) / last block (\d+)")


def markers_in(caplog):
    return [(int(c), int(b)) for c, b in MARKER.findall(caplog.text)]


class FakeWeb3:
    def __init__(self, head):
        self.eth = self
        self.head = head

    def get_block(self, _identifier):
        if isinstance(self.head, Exception):
            raise self.head
        return type("Block", (), {"number": self.head})()


def run(run_id, conclusion, markers, deferred=False):
    return {
        "id": run_id,
        "conclusion": conclusion,
        "markers": [{"chain_id": c, "last_block": b} for c, b in markers],
        "deferred": deferred,
    }


def fetch(run):
    return run["markers"], run["deferred"]


def counting(seen):
    def _fetch(r):
        seen.append(r["id"])
        return fetch(r)

    return _fetch


def as_dict(blocks):
    return {b["chain_id"]: b["last_block"] for b in blocks}


# --- load_checkpoints (deferred reader) --------------------------------------


def test_successful_last_run_gives_the_checkpoints():
    runs = [
        run(3, "success", [(1, 26184830), (56, 127436555), (8453, 52560174)]),
        run(2, "success", [(1, 1), (56, 1), (8453, 1)]),
    ]
    seen = []

    blocks = GithubLogService().load_checkpoints(runs, counting(seen), [1, 56, 8453])

    assert as_dict(blocks) == {1: 26184830, 56: 127436555, 8453: 52560174}
    assert seen == [3]  # older runs are never read


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_unsuccessful_last_run_restarts_every_chain_from_head(conclusion, caplog):
    caplog.set_level(logging.INFO)
    runs = [
        run(3, conclusion, [(1, 110), (56, 500)], deferred=True),
        run(2, "success", [(1, 100), (56, 400)]),
    ]
    seen = []
    service = GithubLogService()

    blocks = service.load_checkpoints(runs, counting(seen), [1, 56])

    assert blocks == []
    assert seen == []  # not even the last run's logs are downloaded
    assert f"Last run 3 ({conclusion}) did not succeed" in caplog.text
    assert service.get_last_block_and_log(blocks, 1, FakeWeb3(1000), defer_marker=True) == (0, 998)
    assert markers_in(caplog) == [(1, 999)]


def test_inputs_still_apply_after_an_unsuccessful_last_run():
    blocks = GithubLogService().load_checkpoints(
        [run(2, "failure", [(1, 110)], deferred=True)], fetch, [1, 56], overrides={56: 700}
    )

    assert as_dict(blocks) == {56: 700}


def test_reset_and_overrides_apply_after_recovery(caplog):
    caplog.set_level(logging.INFO)
    runs = [run(1, "success", [(1, 100), (56, 200), (10, 300)])]

    blocks = GithubLogService().load_checkpoints(
        runs, fetch, [1, 56, 10], reset_chain_ids={56}, overrides={1: 150}
    )

    assert as_dict(blocks) == {1: 150, 10: 300}
    assert "Chain 56: checkpoint discarded on request" in caplog.text
    assert "Chain 1: checkpoint pinned to 150 (recovered 100)" in caplog.text


def test_inputs_covering_every_chain_skip_the_history():
    def unreadable(_run):
        raise OSError("logs unreadable")

    blocks = GithubLogService().load_checkpoints(
        [run(1, "success", [])], unreadable, [1, 56], reset_chain_ids={56}, overrides={1: 150}
    )

    assert as_dict(blocks) == {1: 150}


def test_expired_logs_of_the_success_run_abort():
    from shared.external.github import LogsUnavailable

    def expired(_run):
        raise LogsUnavailable("Checkpoint log download failed: HTTP 404")

    with pytest.raises(RuntimeError, match="Unable to recover checkpoint logs of run 1"):
        GithubLogService().load_checkpoints([run(1, "success", [])], expired, [1])


def test_success_run_without_markers_aborts():
    with pytest.raises(RuntimeError, match="successful run 1 contain no block markers"):
        GithubLogService().load_checkpoints([run(1, "success", [])], fetch, [1])


def test_missing_expected_chain_is_reported_and_bootstraps(caplog):
    caplog.set_level(logging.INFO)
    service = GithubLogService()

    blocks = service.load_checkpoints([run(1, "success", [(1, 100)])], fetch, [1, 56])

    assert as_dict(blocks) == {1: 100}
    assert "No checkpoint found for chains [56]" in caplog.text
    assert service.get_last_block_and_log(blocks, 56, FakeWeb3(1000), defer_marker=True) == (0, 990)
    assert markers_in(caplog) == [(56, 991)]


def test_no_executed_run_bootstraps_every_chain(caplog):
    caplog.set_level(logging.INFO)

    assert GithubLogService().load_checkpoints([], fetch, [1, 56]) == []
    assert "No checkpoint found for chains [1, 56]" in caplog.text


def test_log_download_failure_is_wrapped():
    def failing_fetch(_run):
        raise OSError("boom")

    with pytest.raises(RuntimeError, match="Unable to recover checkpoint logs of run 7"):
        GithubLogService().load_checkpoints([run(7, "success", [])], failing_fetch, [1])


def test_extract_run_markers_detects_the_deferred_sentinel(monkeypatch, tmp_path):
    import io
    import zipfile

    import bots.utils.github as github_utils

    monkeypatch.chdir(tmp_path)
    archives = {
        1: "2026-10-14T10:00:00Z INFO: Checkpoint mode: deferred\n2026-10-14T10:00:01Z INFO: Chain 1 / last block 950\n",
        2: "2026-10-14T10:00:01Z INFO: Chain 1 / last block 940\n",
    }

    def download(owner, repo, run_id, token, output_file="logs.zip"):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("build/8_Run the lockers script.txt", archives[run_id])
        open(output_file, "wb").write(data.getvalue())

    monkeypatch.setattr(github_utils, "download_logs", download)
    service = GithubLogService()

    assert service.extract_run_markers("o", "r", 1, "t") == ([{"chain_id": 1, "last_block": 950}], True)
    assert service.extract_run_markers("o", "r", 2, "t") == ([{"chain_id": 1, "last_block": 940}], False)


# --- marker emission ----------------------------------------------------------


def test_legacy_mode_logs_marker_immediately(caplog):
    caplog.set_level(logging.INFO)
    service = GithubLogService()

    assert service.get_last_block_and_log([{"chain_id": 1, "last_block": 100}], 1, FakeWeb3(200)) == (100, 198)
    assert markers_in(caplog) == [(1, 199)]
    assert DEFERRED_MODE_MARKER not in caplog.text


def test_deferred_mode_logs_marker_on_commit_or_retain_only(caplog):
    caplog.set_level(logging.INFO)
    service = GithubLogService()
    blocks = [{"chain_id": 1, "last_block": 100}, {"chain_id": 56, "last_block": 500}]

    assert service.get_last_block_and_log(blocks, 1, FakeWeb3(200), defer_marker=True) == (100, 198)
    assert service.get_last_block_and_log(blocks, 56, FakeWeb3(1000), defer_marker=True) == (500, 990)
    assert markers_in(caplog) == []
    assert caplog.text.count(DEFERRED_MODE_MARKER) == 1

    service.commit_checkpoint(1)
    service.retain_checkpoint(56)
    service.commit_checkpoint(1)  # a second marker for the same chain is ignored

    assert markers_in(caplog) == [(1, 199), (56, 500)]


def test_deferred_mode_bootstraps_unknown_chain_immediately(caplog):
    caplog.set_level(logging.INFO)
    service = GithubLogService()

    assert service.get_last_block_and_log([], 8453, FakeWeb3(1000), defer_marker=True) == (0, 940)
    assert markers_in(caplog) == [(8453, 941)]
    assert DEFERRED_MODE_MARKER in caplog.text


def test_retain_works_when_the_head_fetch_failed(caplog):
    caplog.set_level(logging.INFO)
    service = GithubLogService()
    blocks = [{"chain_id": 56, "last_block": 500}]

    with pytest.raises(ConnectionError):
        service.get_last_block_and_log(blocks, 56, FakeWeb3(ConnectionError("rpc down")), defer_marker=True)
    service.retain_checkpoint(56)
    service.retain_checkpoint(10)  # never recovered: nothing to retain, no error

    assert markers_in(caplog) == [(56, 500)]
    assert DEFERRED_MODE_MARKER in caplog.text


def test_max_blocks_caps_window_and_commit_resumes_from_cap(caplog):
    caplog.set_level(logging.INFO)
    service = GithubLogService()
    blocks = [{"chain_id": 56, "last_block": 127_436_555}]

    window = service.get_last_block_and_log(blocks, 56, FakeWeb3(127_600_000), defer_marker=True, max_blocks=10_000)

    assert window == (127_436_555, 127_446_554)
    service.commit_checkpoint(56)
    assert markers_in(caplog) == [(56, 127_446_555)]


def test_checkpoint_far_ahead_of_head_fails_the_chain(caplog):
    caplog.set_level(logging.INFO)
    service = GithubLogService()
    service.chain_checkpoint[1] = 99_999_999

    with pytest.raises(RuntimeError, match="ahead of the head"):
        service.get_last_block_and_log([{"chain_id": 1, "last_block": 99_999_999}], 1, FakeWeb3(26_190_000), defer_marker=True)
    service.retain_checkpoint(1)

    assert markers_in(caplog) == [(1, 99_999_999)]


def test_head_behind_checkpoint_scans_nothing_in_deferred_mode(caplog):
    caplog.set_level(logging.INFO)
    blocks = [{"chain_id": 1, "last_block": 1000}]

    deferred = GithubLogService()
    assert deferred.get_last_block_and_log(blocks, 1, FakeWeb3(995), defer_marker=True) == (1000, 999)
    deferred.commit_checkpoint(1)
    assert markers_in(caplog) == [(1, 1000)]

    caplog.clear()
    legacy = GithubLogService()
    assert legacy.get_last_block_and_log(blocks, 1, FakeWeb3(995)) == (1000, 1000)
    assert markers_in(caplog) == [(1, 1001)]


# --- bot_runner glue ----------------------------------------------------------


def test_legacy_get_block_range_reads_the_newest_successful_run_only(monkeypatch, caplog):
    import bots.utils.github as github_utils
    import shared.external.github as github_api

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(github_api, "get_last_workflow_run", lambda *_a: {"id": 42})
    monkeypatch.setattr(
        github_api, "list_executed_workflow_runs", lambda *_a, **_k: pytest.fail("legacy bots must not walk runs")
    )
    extracted = []
    monkeypatch.setattr(
        github_utils.GithubLogService,
        "extract_chain_id_last_block",
        lambda self, owner, repo, run_id, token, pattern: extracted.append(run_id) or [{"chain_id": 1, "last_block": 100}],
    )

    assert bot_runner.get_block_range("wf", 1, FakeWeb3(200)) == (100, 198)
    assert extracted == [42]
    assert markers_in(caplog) == [(1, 199)]


def test_deferred_get_block_range_applies_inputs(monkeypatch, caplog):
    import bots.utils.github as github_utils
    import shared.external.github as github_api

    caplog.set_level(logging.INFO)
    runs = [run(2, "failure", [(1, 110), (56, 500)], deferred=True), run(1, "success", [(1, 100), (56, 400)])]
    monkeypatch.setattr(github_api, "list_executed_workflow_runs", lambda *_a, **_k: runs)
    monkeypatch.setattr(github_api, "get_last_workflow_run", lambda *_a: pytest.fail("deferred bots list executed runs"))
    monkeypatch.setattr(
        github_utils.GithubLogService,
        "extract_run_markers",
        lambda self, owner, repo, run_id, token: fetch(next(r for r in runs if r["id"] == run_id)),
    )
    checkpoints = {}
    kwargs = dict(defer_commit=True, expected_chain_ids=[1, 56], reset_chain_ids={56}, checkpoint_overrides={1: 105})

    assert bot_runner.get_block_range("wf", 1, FakeWeb3(200), checkpoints, **kwargs) == (105, 198)
    assert bot_runner.get_block_range("wf", 56, FakeWeb3(1000), checkpoints, **kwargs) == (0, 990)
    bot_runner.commit_block_range("wf", 1, checkpoints)

    assert markers_in(caplog) == [(56, 991), (1, 199)]


def test_commit_block_range_dispatches_to_service(caplog):
    caplog.set_level(logging.INFO)
    service = GithubLogService()
    checkpoints = {"wf": (service, [{"chain_id": 1, "last_block": 10}, {"chain_id": 56, "last_block": 20}])}

    assert bot_runner.get_block_range("wf", 1, FakeWeb3(100), checkpoints, defer_commit=True) == (10, 98)
    assert bot_runner.get_block_range("wf", 56, FakeWeb3(100), checkpoints, defer_commit=True) == (20, 90)
    bot_runner.commit_block_range("wf", 1, checkpoints)
    bot_runner.commit_block_range("wf", 56, checkpoints, processed=False)

    assert markers_in(caplog) == [(1, 99), (56, 20)]


# --- curve-pools job isolation ------------------------------------------------


@pytest.fixture
def curve_pools():
    from bots.curve.pools import main as module

    return module


HEADS = {1: 2000, 42161: 2000, 8453: ConnectionError("head fetch failed"), 10: 2000, 252: 2000, 324: 2000, 56: 2000}


class FakeService:
    def __init__(self, failing=()):
        self.failing = set(failing)
        self.w3 = {}

    def add_chain(self, chain_id):
        if chain_id in self.failing:
            raise ValueError(f"no endpoint for chain {chain_id}")
        self.w3[chain_id] = FakeWeb3(HEADS[chain_id])

    def get_w3(self, chain_id):
        return self.w3[chain_id]


@pytest.fixture
def real_checkpoints(monkeypatch, caplog):
    """Drive the real get_block_range/commit path from one recovered success run."""
    import bots.utils.github as github_utils
    import shared.external.github as github_api

    caplog.set_level(logging.INFO)
    # Chain 10 has no checkpoint: it bootstraps.
    success = run(1, "success", [(c, 1000) for c in (1, 42161, 8453, 252, 324, 56)])
    monkeypatch.setattr(github_api, "list_executed_workflow_runs", lambda *_a, **_k: [success])
    monkeypatch.setattr(github_utils.GithubLogService, "extract_run_markers", lambda self, *_a: fetch(success))


def test_scan_order_and_caps(curve_pools):
    assert set(curve_pools.CHAIN_SCAN_ORDER) == set(curve_pools.BLOCKCHAIN_IDS)
    assert curve_pools.CHAIN_SCAN_ORDER[-1] == 56
    assert curve_pools.MAX_BLOCKS_PER_RUN == {1: 5000, 56: 10000, 42161: 100000}
    assert curve_pools.DEFAULT_MAX_BLOCKS_PER_RUN == 50000


def test_job_isolates_chain_failures_and_commits_after_processing(curve_pools, real_checkpoints, monkeypatch, caplog):
    processed = []

    def fake_process_chain(web3, chain_id, from_block, to_block, lockers):
        # Nothing of this chain may be committed before its notifications.
        assert chain_id not in [c for c, _ in markers_in(caplog)]
        processed.append((chain_id, from_block, to_block))
        if chain_id == 56:
            raise RuntimeError("getLogs request exceeded max allowed range")

    monkeypatch.setattr(curve_pools, "load_lockers", lambda: [])
    monkeypatch.setattr(curve_pools, "get_web3_service", lambda: FakeService(failing={324}))
    monkeypatch.setattr(curve_pools, "process_chain", fake_process_chain)

    with pytest.raises(RuntimeError, match=r"Chains failed: 8453: head fetch failed, 324: no endpoint for chain 324, 56: getLogs"):
        curve_pools.job()

    assert processed == [(1, 1000, 1998), (42161, 1000, 1700), (252, 1000, 1995), (56, 1000, 1990)]
    assert markers_in(caplog) == [
        (1, 1999), (42161, 1701), (8453, 1000), (10, 1941), (252, 1996), (324, 1000), (56, 1000),
    ]
    assert caplog.text.count(DEFERRED_MODE_MARKER) == 1


def test_interrupted_job_keeps_every_unfinished_checkpoint(curve_pools, real_checkpoints, monkeypatch, caplog):
    def fake_process_chain(web3, chain_id, *_args):
        if chain_id == 42161:
            raise KeyboardInterrupt("cancelled")

    monkeypatch.setattr(curve_pools, "load_lockers", lambda: [])
    monkeypatch.setattr(curve_pools, "get_web3_service", FakeService)
    monkeypatch.setattr(curve_pools, "process_chain", fake_process_chain)

    with pytest.raises(KeyboardInterrupt):
        curve_pools.job()

    assert markers_in(caplog) == [
        (1, 1999), (42161, 1000), (8453, 1000), (252, 1000), (324, 1000), (56, 1000),
    ]


def test_spent_budget_keeps_remaining_checkpoints(curve_pools, real_checkpoints, monkeypatch, caplog):
    processed = []
    monkeypatch.setattr(curve_pools, "RUN_BUDGET_SECONDS", -1)
    monkeypatch.setattr(curve_pools, "load_lockers", lambda: [])
    monkeypatch.setattr(curve_pools, "get_web3_service", FakeService)
    monkeypatch.setattr(curve_pools, "process_chain", lambda web3, chain_id, *_a: processed.append(chain_id))

    curve_pools.job()

    # The first chain always runs so the checkpoints get loaded; the rest wait.
    assert processed == [1]
    assert markers_in(caplog) == [(1, 1999), (42161, 1000), (8453, 1000), (252, 1000), (324, 1000), (56, 1000)]
    assert "Run budget" in caplog.text


def test_failed_sends_are_reported(curve_pools, monkeypatch, capsys):
    monkeypatch.setattr(curve_pools, "send_telegram_message", lambda *_a: False)
    curve_pools.failed_sends.clear()

    curve_pools.send_telegram_message_curve("msg", 56)
    curve_pools._report_failed_sends(56)

    assert curve_pools.failed_sends[56] == 2  # activity and public BSC channels
    assert "::warning title=curve-pools chain 56::2 Telegram message(s)" in capsys.readouterr().out
    curve_pools.failed_sends.clear()


def test_process_chain_resets_the_provider_before_dispatch(curve_pools, monkeypatch):
    calls = []
    web3 = type("W3", (), {"provider": type("P", (), {"reset": lambda self: calls.append("reset")})()})()
    monkeypatch.setattr(curve_pools, "collect_chain_events", lambda *_a: calls.append("scan") or [])
    monkeypatch.setattr(curve_pools, "dispatch_chain_events", lambda *_a: calls.append("dispatch"))

    curve_pools.process_chain(web3, 1, 10, 20, [])

    assert calls == ["scan", "reset", "dispatch"]


def test_bsc_pools_skip_the_generic_curve_scan(curve_pools):
    from web3 import Web3
    from web3.providers import BaseProvider

    requests_by_address = []

    class Provider(BaseProvider):
        def make_request(self, method, params):
            if method == "eth_getLogs":
                address = params[0]["address"]
                requests_by_address.append((address[0] if isinstance(address, list) else address).lower())
                return {"jsonrpc": "2.0", "id": 1, "result": []}
            return {"jsonrpc": "2.0", "id": 1, "result": "0x38"}

    curve_pools.collect_chain_events(Web3(Provider()), 56, 900, 998)

    pancake_pools = {a.lower() for a in curve_pools.POOLS_PER_CHAIN[56]}
    depositors = {a.lower() for a in curve_pools.Common.DEPOSITORS.get(56, [])}
    # V3 Swap + stable TokenExchange/AddLiquidity, then the depositor scans.
    assert sum(a in pancake_pools for a in requests_by_address) == 3
    assert set(requests_by_address) <= pancake_pools | depositors


def test_job_aborts_when_no_checkpoint_can_be_recovered(curve_pools, monkeypatch):
    calls = []

    def failing_get_block_range(*args, **_kwargs):
        calls.append(args)
        raise RuntimeError("Checkpoint run lookup failed: HTTP 500")

    monkeypatch.setattr(curve_pools, "load_lockers", lambda: [])
    monkeypatch.setattr(curve_pools, "get_web3_service", FakeService)
    monkeypatch.setattr(curve_pools, "get_block_range", failing_get_block_range)
    monkeypatch.setattr(curve_pools, "commit_block_range", lambda *a, **k: pytest.fail("no marker expected"))

    with pytest.raises(RuntimeError, match="HTTP 500"):
        curve_pools.job()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "env, value",
    [
        ("BOT_RESET_CHAIN_IDS", "1,abc"),
        ("BOT_RESET_CHAIN_IDS", "8543"),
        ("BOT_CHECKPOINT_OVERRIDES", "1:26189639"),
        ("BOT_CHECKPOINT_OVERRIDES", "1=0"),
        ("BOT_CHECKPOINT_OVERRIDES", "137=5"),
        ("BOT_CHECKPOINT_OVERRIDES", "1=5,1=7"),
    ],
)
def test_job_rejects_invalid_inputs_before_any_lookup(curve_pools, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    monkeypatch.setattr(curve_pools, "load_lockers", lambda: pytest.fail("inputs are validated first"))

    with pytest.raises(ValueError, match="Invalid"):
        curve_pools.job()


def test_job_rejects_a_chain_both_reset_and_overridden(curve_pools, monkeypatch):
    monkeypatch.setenv("BOT_RESET_CHAIN_IDS", "1")
    monkeypatch.setenv("BOT_CHECKPOINT_OVERRIDES", "1=26189639")
    monkeypatch.setattr(curve_pools, "load_lockers", lambda: pytest.fail("inputs are validated first"))

    with pytest.raises(ValueError, match="both reset and overridden"):
        curve_pools.job()


def test_input_parsers(curve_pools):
    assert curve_pools.parse_chain_ids("") == set()
    assert curve_pools.parse_chain_ids(" 1, 56 ,") == {1, 56}
    assert curve_pools.parse_checkpoint_overrides("") == {}
    assert curve_pools.parse_checkpoint_overrides("1=26189639, 56=127565851") == {1: 26189639, 56: 127565851}
