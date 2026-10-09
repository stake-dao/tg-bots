import logging
import os
import re
import time

from requests.exceptions import HTTPError

from shared.communication.logger import logger
from shared.external.github import download_logs, extract_logs

logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

# Config
OUTPUT_DIR = "./logs"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
OWNER = "stake-dao"
REPO = "tg-bots"

# Safety margin subtracted from `latest` block when computing the run's toBlock.
# Reason: stake_rpc / public RPCs are load-balanced — `eth_getBlockByNumber("latest")`
# can hit a node ahead of the one that later serves `eth_getLogs`, producing
# "block not found, requested toBlock X is not available (latestBlock: X-N)".
# Margins are sized to absorb typical upstream drift on each chain.
RPC_BLOCK_SAFETY_MARGIN = {
    1: 2,         # ethereum (~12s blocks)
    10: 60,       # optimism (~2s blocks)
    56: 10,       # bsc (~3s blocks)
    137: 30,      # polygon (~2s blocks)
    8453: 60,     # base (~2s blocks)
    42161: 300,   # arbitrum (~0.25s blocks)
    146: 30,      # sonic
    42793: 30,    # etherlink
}
DEFAULT_RPC_BLOCK_SAFETY_MARGIN = 5

class GithubLogService:

    def __init__(self):
        self.chain_logged = {}

    def parse_logs(self, logs_dir, pattern=r"Chain (\d+) / last block (\d+)"):
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


    def extract_chain_id_last_block(self, owner, repo, run_id, token, pattern):
        try:
            # Step 1 : Download logs
            download_logs(owner, repo, run_id, token)

            # Step 2 : extract logs into a zip file
            extract_logs("logs.zip", OUTPUT_DIR)

            # Step 3 : parse logs
            checkpoints = self.parse_logs(OUTPUT_DIR)
            if not checkpoints:
                raise RuntimeError("Checkpoint logs contain no block markers")
            return checkpoints
        except Exception as e:
            raise RuntimeError("Unable to recover checkpoint logs") from e


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
                logger.log(
                    f"RPC 429 on chain {chain_id} (attempt {attempt + 1}/{max_attempts}), "
                    f"retrying in {base_delay:.1f}s"
                )
                time.sleep(base_delay)
        raise last_exc


    def get_last_block_and_log(self, chain_ids_last_blocks, chain_id, web3):
        # Get the last block fetched
        min_block = 0
        for chain_id_last_block in chain_ids_last_blocks:
            if chain_id_last_block["chain_id"] == chain_id:
                min_block = chain_id_last_block["last_block"]
                break


        if chain_id not in self.chain_logged:

            current_block = self._fetch_latest_block_with_retry(web3, chain_id)

            margin = RPC_BLOCK_SAFETY_MARGIN.get(
                chain_id, DEFAULT_RPC_BLOCK_SAFETY_MARGIN
            )
            current_block = max(min_block, current_block - margin)

            logger.log(f"Last block found for chain id {chain_id} : {min_block}")

            # Log the chain id & block for the next run
            logger.log(f"Chain {chain_id} / last block {current_block + 1}")

            self.chain_logged[chain_id] = [min_block, current_block]

        return self.chain_logged[chain_id][0], self.chain_logged[chain_id][1]
