import os


import shutil


import time


import zipfile


import requests


GITHUB_API_URL = "https://api.github.com"


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
    else:
        raise RuntimeError(f"Checkpoint log download failed: HTTP {response.status_code}")


def extract_logs(zip_file, output_dir):
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir)

    with zipfile.ZipFile(zip_file, "r") as zip_ref:
        zip_ref.extractall(output_dir)
    print(f"Logs extracted here : {output_dir}")


def get_last_workflow_run(owner, repo, workflow_name):
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/actions/workflows/{workflow_name}.yml/runs"
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}
    response = requests.get(
        url,
        headers=headers,
        params={"status": "success", "per_page": 100},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Checkpoint run lookup failed: HTTP {response.status_code}")
    for run in response.json()["workflow_runs"]:
        if str(run["id"]) == os.environ.get("GITHUB_RUN_ID"):
            continue
        jobs = requests.get(run["jobs_url"], headers=headers, timeout=30)
        if jobs.status_code != 200:
            raise RuntimeError(f"Checkpoint job lookup failed: HTTP {jobs.status_code}")
        if any(job["conclusion"] == "success" for job in jobs.json()["jobs"]):
            return run
    raise RuntimeError(f"No successful checkpoint run for {workflow_name}")
