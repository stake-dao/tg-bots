import logging


from typing import Tuple


from dotenv import load_dotenv


def setup_bot():
    load_dotenv()
    logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)


def get_block_range(workflow_name: str, chain_id: int, web3) -> Tuple[int, int]:
    from bots.utils.github import OWNER, REPO, TOKEN, GithubLogService
    from shared.external.github import get_last_workflow_run

    last_workflow_run = get_last_workflow_run(OWNER, REPO, workflow_name)

    github_log_service = GithubLogService()
    chain_ids_last_blocks = []
    if last_workflow_run is not None:
        last_run_id = last_workflow_run["id"]
        chain_ids_last_blocks = github_log_service.extract_chain_id_last_block(
            OWNER, REPO, last_run_id, TOKEN, None
        )

    return github_log_service.get_last_block_and_log(
        chain_ids_last_blocks, chain_id, web3
    )


def run_bot(job_fn, name: str = ""):
    try:
        job_fn()
    except Exception:
        logging.exception(f"Bot {name} failed")
        raise
