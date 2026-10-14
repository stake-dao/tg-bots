import logging


from typing import Dict, Iterable, Optional, Tuple


from dotenv import load_dotenv


def setup_bot():
    load_dotenv()
    logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)


def _load_legacy_checkpoints(workflow_name: str):
    """Markers of the newest successful run: the checkpoint of single-shot bots."""
    from bots.utils.github import OWNER, REPO, TOKEN, GithubLogService
    from shared.external.github import get_last_workflow_run

    last_run = get_last_workflow_run(OWNER, REPO, workflow_name)
    service = GithubLogService()
    blocks = service.extract_chain_id_last_block(
        OWNER, REPO, last_run["id"], TOKEN, None
    )
    return service, blocks


def _load_deferred_checkpoints(
    workflow_name: str,
    expected_chain_ids: Iterable[int],
    reset_chain_ids: Iterable[int],
    checkpoint_overrides: Optional[Dict[int, int]],
):
    from bots.utils.github import OWNER, REPO, TOKEN, GithubLogService
    from shared.external.github import list_executed_workflow_runs

    service = GithubLogService()
    blocks = service.load_checkpoints(
        list_executed_workflow_runs(OWNER, REPO, workflow_name),
        lambda run: service.extract_run_markers(OWNER, REPO, run["id"], TOKEN),
        expected_chain_ids,
        reset_chain_ids=reset_chain_ids,
        overrides=checkpoint_overrides,
    )
    # Known before any head fetch, so a chain failing early can still retain it.
    service.chain_checkpoint.update({b["chain_id"]: b["last_block"] for b in blocks})
    return service, blocks


def get_block_range(
    workflow_name: str,
    chain_id: int,
    web3,
    checkpoints=None,
    *,
    defer_commit: bool = False,
    max_blocks: Optional[int] = None,
    expected_chain_ids: Optional[Iterable[int]] = None,
    reset_chain_ids: Iterable[int] = (),
    checkpoint_overrides: Optional[Dict[int, int]] = None,
) -> Tuple[int, int]:
    """Block window to scan for a chain, recovered from previous run logs.

    Without ``defer_commit`` the checkpoint is the newest successful run's
    markers and the marker for the next run is logged right away. With it,
    markers are merged across the newest executed runs (see
    ``GithubLogService.load_checkpoints``) and the caller must end each chain
    with :func:`commit_block_range`, so a chain whose processing fails keeps
    its previous checkpoint; ``expected_chain_ids``, ``reset_chain_ids`` and
    ``checkpoint_overrides`` are consumed by that first recovery. Pass the same
    mode for every chain of a run. ``max_blocks`` caps the window per run.
    """
    if checkpoints is None:
        checkpoints = {}
    if workflow_name not in checkpoints:
        if defer_commit:
            checkpoints[workflow_name] = _load_deferred_checkpoints(
                workflow_name, expected_chain_ids or (), reset_chain_ids, checkpoint_overrides
            )
        else:
            checkpoints[workflow_name] = _load_legacy_checkpoints(workflow_name)
    service, blocks = checkpoints[workflow_name]
    return service.get_last_block_and_log(
        blocks, chain_id, web3, defer_marker=defer_commit, max_blocks=max_blocks
    )


def commit_block_range(workflow_name: str, chain_id: int, checkpoints, processed: bool = True):
    """Emit the checkpoint marker of a chain scanned with ``defer_commit=True``.

    ``processed=True`` advances the checkpoint past the scanned window;
    ``processed=False`` re-emits the previous checkpoint so the next run
    retries the same window.
    """
    service, _ = checkpoints[workflow_name]
    if processed:
        service.commit_checkpoint(chain_id)
    else:
        service.retain_checkpoint(chain_id)


def retain_uncommitted_ranges(workflow_name: str, chain_ids: Iterable[int], checkpoints):
    """Re-emit the previous checkpoint of every chain without a marker yet."""
    service, _ = checkpoints[workflow_name]
    for chain_id in chain_ids:
        if chain_id not in service.chain_committed:
            service.retain_checkpoint(chain_id)


def run_bot(job_fn, name: str = ""):
    try:
        job_fn()
    except Exception:
        logging.exception(f"Bot {name} failed")
        raise
