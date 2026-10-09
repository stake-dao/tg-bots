import logging


from typing import Tuple


from dotenv import load_dotenv


def setup_bot():
    load_dotenv()
    logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)


def get_block_range(workflow_name: str, chain_id: int, web3, checkpoints=None) -> Tuple[int, int]:
    from bots.utils.github import OWNER, REPO, TOKEN, GithubLogService
    from shared.external.github import get_last_workflow_run

    if checkpoints is None:
        checkpoints = {}
    if workflow_name not in checkpoints:
        last_run = get_last_workflow_run(OWNER, REPO, workflow_name)
        service = GithubLogService()
        blocks = service.extract_chain_id_last_block(
            OWNER, REPO, last_run["id"], TOKEN, None
        )
        checkpoints[workflow_name] = service, blocks
    service, blocks = checkpoints[workflow_name]
    return service.get_last_block_and_log(blocks, chain_id, web3)


def run_bot(job_fn, name: str = ""):
    try:
        job_fn()
    except Exception:
        logging.exception(f"Bot {name} failed")
        raise
