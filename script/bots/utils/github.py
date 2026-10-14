import logging
import os
import re
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from requests.exceptions import HTTPError

from shared.external.github import LogsUnavailable, download_logs, extract_logs

logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

# Config
OUTPUT_DIR = "./logs"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
OWNER = "stake-dao"
REPO = "tg-bots"

# Checkpoint marker read back from a previous run's logs: the block the next
# run must start from for that chain. The reader keeps the first match per
# chain, so a run must log it exactly once per chain.
CHECKPOINT_PATTERN = r"Chain (\d+) / last block (\d+)"
# Sentinel logged by runs that emit a chain's marker only once that chain was
# fully processed (or re-emit the previous checkpoint when it failed). Legacy
# runs log the marker before scanning, so only sentinel-bearing runs may be
# trusted as checkpoints when they did not succeed.
DEFERRED_MODE_MARKER = "Checkpoint mode: deferred"

# Safety margin subtracted from `latest` block when computing the run's toBlock.
# Reason: stake_rpc / public RPCs are load-balanced — `eth_getBlockByNumber("latest")`
# can hit a node ahead of the one that later serves `eth_getLogs`, producing
# "block not found, requested toBlock X is not available (latestBlock: X-N)".
# Margins are sized to absorb typical upstream drift on each chain.
RPC_BLOCK_SAFETY_MARGIN = {
    1: 2,         # ethereum (~12s blocks)
    10: 60,       # optimism (~2s blocks)
    56: 10,       # bsc (~0.45s blocks)
    137: 30,      # polygon (~2s blocks)
    8453: 60,     # base (~2s blocks)
    42161: 300,   # arbitrum (~0.25s blocks)
    146: 30,      # sonic
    42793: 30,    # etherlink
}
DEFAULT_RPC_BLOCK_SAFETY_MARGIN = 5
# A deferred checkpoint ahead of the chain head by more than this is not node
# lag but a bad checkpoint (wrong override, head read from the wrong chain).
MAX_HEAD_LAG_BLOCKS = 1000


class GithubLogService:

    def __init__(self):
        self.chain_logged = {}
        # Checkpoint recovered for each chain, kept before the head fetch so a
        # chain whose window could not be computed can still retain it.
        self.chain_checkpoint = {}
        # Chains whose marker was already logged this run.
        self.chain_committed = set()
        self.deferred_announced = False

    def parse_logs(self, logs_dir, pattern=CHECKPOINT_PATTERN):
        extracted_data = []

        for root, _, files in os.walk(logs_dir):
            for file in files:
                if file.endswith(".txt"):
                    file_path = os.path.join(root, file)
                    with open(file_path, "r") as f:
                        content = f.read()

                        matches = re.findall(pattern, content)
                        for match in matches:
                            extracted_data.append(
                                {
                                    "chain_id": int(match[0]),
                                    "last_block": int(match[1]),
                                }
                            )

        return extracted_data

    def logs_contain(self, logs_dir, text) -> bool:
        for root, _, files in os.walk(logs_dir):
            for file in files:
                if file.endswith(".txt"):
                    with open(os.path.join(root, file), "r") as f:
                        if text in f.read():
                            return True
        return False

    def extract_run_markers(self, owner, repo, run_id, token) -> Tuple[List[Dict], bool]:
        """Download one run's logs; return its markers and whether it ran deferred."""
        download_logs(owner, repo, run_id, token)
        extract_logs("logs.zip", OUTPUT_DIR)
        return (
            self.parse_logs(OUTPUT_DIR),
            self.logs_contain(OUTPUT_DIR, DEFERRED_MODE_MARKER),
        )

    def extract_chain_id_last_block(self, owner, repo, run_id, token, pattern):
        try:
            checkpoints, _ = self.extract_run_markers(owner, repo, run_id, token)
            if not checkpoints:
                raise RuntimeError("Checkpoint logs contain no block markers")
            return checkpoints
        except Exception as e:
            raise RuntimeError("Unable to recover checkpoint logs") from e

    def load_checkpoints(
        self,
        runs: Iterable[Dict],
        fetch_markers: Callable[[Dict], Tuple[List[Dict], bool]],
        expected_chain_ids: Iterable[int],
        reset_chain_ids: Iterable[int] = (),
        overrides: Optional[Dict[int, int]] = None,
    ) -> List[Dict]:
        """Merge checkpoint markers from executed runs, newest first (deferred mode).

        ``fetch_markers(run)`` returns ``(markers, deferred)`` for one run. A
        run that did not succeed is trusted only when it ran in deferred mode,
        because legacy runs logged their markers before scanning. Per chain the
        newest trusted marker wins. The walk stops at the first successful run,
        which holds a marker for every chain it knew, or earlier once every
        expected chain is covered. Chains in ``reset_chain_ids``, and chains no
        inspected run mentions, restart from the current head; ``overrides``
        pin a chain to an explicit block.
        """
        found: Dict[int, int] = {}
        expected = set(expected_chain_ids)
        reset_chain_ids = set(reset_chain_ids)
        overrides = overrides or {}
        if expected and expected <= reset_chain_ids | set(overrides):
            # Every chain is given explicitly: the history is not needed, which
            # also rescues a recovery wedged on unreadable logs.
            runs = []
        for run in runs:
            success = run.get("conclusion") == "success"
            try:
                markers, deferred = fetch_markers(run)
            except LogsUnavailable as e:
                if success:
                    raise RuntimeError(
                        f"Unable to recover checkpoint logs of run {run.get('id')}"
                    ) from e
                logging.warning(f"Skipping run {run.get('id')}: {e}")
                continue
            except Exception as e:
                raise RuntimeError(
                    f"Unable to recover checkpoint logs of run {run.get('id')}"
                ) from e
            if success and not markers:
                raise RuntimeError(
                    f"Checkpoint logs of successful run {run.get('id')} contain no block markers"
                )
            if not success and not deferred:
                logging.info(
                    f"Ignoring markers of run {run.get('id')} "
                    f"({run.get('conclusion')}): logged before processing"
                )
                continue
            for marker in markers:
                found.setdefault(marker["chain_id"], marker["last_block"])
            logging.info(
                f"Checkpoint run {run.get('id')} ({run.get('conclusion')}) "
                f"covers chains {sorted({m['chain_id'] for m in markers})}"
            )
            if success or expected <= set(found):
                break

        for chain_id in reset_chain_ids:
            if found.pop(chain_id, None) is not None:
                logging.info(f"Chain {chain_id}: checkpoint discarded on request, restarting from head")
        for chain_id, block in overrides.items():
            logging.info(f"Chain {chain_id}: checkpoint pinned to {block} (recovered {found.get(chain_id)})")
            found[chain_id] = block
        missing = expected - set(found) - set(reset_chain_ids)
        if missing:
            logging.warning(
                f"No checkpoint found for chains {sorted(missing)}: they restart from the current head"
            )
        return [
            {"chain_id": chain_id, "last_block": last_block}
            for chain_id, last_block in found.items()
        ]


    def _fetch_latest_block_with_retry(self, web3, chain_id, max_attempts=5, base_delay=0.5):
        last_exc = None
        for attempt in range(max_attempts):
            try:
                return web3.eth.get_block("latest").number
            except HTTPError as e:
                last_exc = e
                status = getattr(e.response, "status_code", None)
                if status != 429:
                    raise
                logging.info(
                    f"RPC 429 on chain {chain_id} (attempt {attempt + 1}/{max_attempts}), "
                    f"retrying in {base_delay:.1f}s"
                )
                time.sleep(base_delay)
        raise last_exc


    def _log_marker(self, chain_id, block):
        if chain_id in self.chain_committed:
            logging.warning(f"Chain {chain_id}: checkpoint marker already logged, ignoring")
            return
        self.chain_committed.add(chain_id)
        # Log the chain id & block for the next run
        logging.info(f"Chain {chain_id} / last block {block}")

    def _announce_deferred_mode(self):
        if not self.deferred_announced:
            self.deferred_announced = True
            logging.info(DEFERRED_MODE_MARKER)

    def get_last_block_and_log(
        self, chain_ids_last_blocks, chain_id, web3, defer_marker=False, max_blocks=None
    ):
        """Return the ``(from_block, to_block)`` window to scan for a chain.

        By default the marker for the next run is logged right away. With
        ``defer_marker`` the caller must end with :meth:`commit_checkpoint` or
        :meth:`retain_checkpoint`; a chain without checkpoint still logs its
        bootstrap marker immediately since there is nothing to process.
        ``max_blocks`` caps the window so a backlog is caught up over several
        runs instead of one request burst.
        """
        # Get the last block fetched
        min_block = 0
        for chain_id_last_block in chain_ids_last_blocks:
            if chain_id_last_block["chain_id"] == chain_id:
                min_block = chain_id_last_block["last_block"]
                break

        if chain_id not in self.chain_logged:
            self.chain_checkpoint[chain_id] = min_block
            if defer_marker:
                self._announce_deferred_mode()

            current_block = self._fetch_latest_block_with_retry(web3, chain_id)

            margin = RPC_BLOCK_SAFETY_MARGIN.get(
                chain_id, DEFAULT_RPC_BLOCK_SAFETY_MARGIN
            )
            head = current_block - margin
            if defer_marker and min_block and min_block - head > MAX_HEAD_LAG_BLOCKS:
                raise RuntimeError(
                    f"Chain {chain_id}: checkpoint {min_block} is {min_block - head} blocks "
                    f"ahead of the head {head}"
                )
            if defer_marker and min_block and head < min_block:
                # The serving node lags behind the previous run's: scan nothing
                # and keep the checkpoint instead of stepping past unseen blocks.
                logging.info(f"Chain {chain_id}: head {head} behind checkpoint {min_block}, nothing to scan")
                current_block = min_block - 1
            else:
                current_block = max(min_block, head)

            if max_blocks and min_block and current_block - min_block + 1 > max_blocks:
                logging.info(
                    f"Chain {chain_id}: backlog of {current_block - min_block + 1} blocks "
                    f"capped to {max_blocks}, the rest is left to the next run"
                )
                current_block = min_block + max_blocks - 1

            logging.info(f"Last block found for chain id {chain_id} : {min_block}")

            self.chain_logged[chain_id] = [min_block, current_block]

            if not defer_marker or min_block == 0:
                self._log_marker(chain_id, current_block + 1)

        return self.chain_logged[chain_id][0], self.chain_logged[chain_id][1]

    def commit_checkpoint(self, chain_id):
        """Log the marker advancing a deferred chain past its scanned window."""
        self._log_marker(chain_id, self.chain_logged[chain_id][1] + 1)

    def retain_checkpoint(self, chain_id):
        """Re-log the previous checkpoint of a deferred chain whose run failed."""
        block = self.chain_checkpoint.get(chain_id)
        if not block:
            logging.info(f"Chain {chain_id}: no checkpoint recovered, nothing to retain")
            return
        self._log_marker(chain_id, block)
