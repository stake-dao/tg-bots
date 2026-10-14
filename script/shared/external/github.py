import os
import shutil
import time
import zipfile

import requests


GITHUB_API_URL = "https://api.github.com"


class LogsUnavailable(RuntimeError):
    """A run's logs are gone (expired or deleted)."""


# Run conclusions proving the bot actually executed: a cron gated off by its
# BOT_CRON_* variable leaves a run (and its job) with conclusion "skipped".
EXECUTED_CONCLUSIONS = ("success", "failure", "cancelled", "timed_out")


def _headers():
    return {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}


def download_logs(owner, repo, run_id, token, output_file="logs.zip"):
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    response = requests.get(url, headers=headers, stream=True, timeout=30)

    if response.status_code == 200:
        with open(output_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Logs downloaded : {output_file}")
    elif response.status_code in (404, 410):
        raise LogsUnavailable(f"Checkpoint log download failed: HTTP {response.status_code}")
    else:
        raise RuntimeError(f"Checkpoint log download failed: HTTP {response.status_code}")


def extract_logs(zip_file, output_dir):
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir)

    with zipfile.ZipFile(zip_file, "r") as zip_ref:
        zip_ref.extractall(output_dir)
    print(f"Logs extracted here : {output_dir}")


def _list_workflow_runs(owner, repo, workflow_name, params):
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/actions/workflows/{workflow_name}.yml/runs"
    response = requests.get(url, headers=_headers(), params=params, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f"Checkpoint run lookup failed: HTTP {response.status_code}")
    return response.json()["workflow_runs"]


def get_last_workflow_run(owner, repo, workflow_name):
    """Newest successful run whose job really ran: the legacy bots' checkpoint."""
    headers = _headers()
    for run in _list_workflow_runs(owner, repo, workflow_name, {"status": "success", "per_page": 100}):
        if str(run["id"]) == os.environ.get("GITHUB_RUN_ID"):
            continue
        jobs = requests.get(run["jobs_url"], headers=headers, timeout=30)
        if jobs.status_code != 200:
            raise RuntimeError(f"Checkpoint job lookup failed: HTTP {jobs.status_code}")
        if any(job["conclusion"] == "success" for job in jobs.json()["jobs"]):
            return run
    raise RuntimeError(f"No successful checkpoint run for {workflow_name}")


def list_executed_workflow_runs(owner, repo, workflow_name, per_page=100):
    """Completed runs that executed the bot, newest first, without the current run.

    One query per executed conclusion, so the runs a gated-off cron leaves as
    "skipped" never dilute the page and the newest successful run is always
    part of the result.
    """
    current_run_id = os.environ.get("GITHUB_RUN_ID")
    runs = {}
    for conclusion in EXECUTED_CONCLUSIONS:
        params = {"status": conclusion, "per_page": per_page}
        for run in _list_workflow_runs(owner, repo, workflow_name, params):
            if str(run["id"]) == current_run_id:
                continue
            if run.get("conclusion") not in EXECUTED_CONCLUSIONS:
                continue
            runs[run["id"]] = run
    return [runs[run_id] for run_id in sorted(runs, reverse=True)]
