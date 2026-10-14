from unittest.mock import MagicMock

import pytest

from shared.external import github as github_api


def fake_get(pages, jobs_by_url=None):
    calls = []

    def get(url, headers=None, params=None, timeout=None, **_kwargs):
        calls.append((url, params))
        response = MagicMock()
        response.status_code = 200
        if url.endswith("/runs"):
            response.json.return_value = {"workflow_runs": pages.get(params["status"], [])}
        else:
            response.json.return_value = {"jobs": (jobs_by_url or {}).get(url, [{"conclusion": "success"}])}
        return response

    return get, calls


@pytest.fixture(autouse=True)
def github_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")


def test_executed_runs_are_queried_per_conclusion_and_sorted_newest_first(monkeypatch):
    pages = {
        "success": [{"id": 3, "conclusion": "success"}, {"id": 1, "conclusion": "success"}],
        "failure": [{"id": 9, "conclusion": "failure"}, {"id": 5, "conclusion": "failure"}],
        "cancelled": [{"id": 7, "conclusion": "cancelled"}, {"id": 4, "conclusion": "skipped"}],
        "timed_out": [],
    }
    get, calls = fake_get(pages)
    monkeypatch.setattr(github_api.requests, "get", get)

    runs = github_api.list_executed_workflow_runs("stake-dao", "tg-bots", "curve-pools")

    assert [r["id"] for r in runs] == [7, 5, 3, 1]  # current run 9 and skipped run 4 left out
    assert [params["status"] for _, params in calls] == ["success", "failure", "cancelled", "timed_out"]
    assert all(params["per_page"] == 100 for _, params in calls)
    assert all(url.endswith("/actions/workflows/curve-pools.yml/runs") for url, _ in calls)


def test_executed_runs_lookup_raises_on_http_error(monkeypatch):
    response = MagicMock()
    response.status_code = 503
    monkeypatch.setattr(github_api.requests, "get", lambda *a, **k: response)

    with pytest.raises(RuntimeError, match="HTTP 503"):
        github_api.list_executed_workflow_runs("stake-dao", "tg-bots", "curve-pools")


def test_last_workflow_run_keeps_the_success_query_and_job_check(monkeypatch):
    pages = {
        "success": [
            {"id": 9, "conclusion": "success", "jobs_url": "https://fixture.invalid/current"},
            {"id": 3, "conclusion": "success", "jobs_url": "https://fixture.invalid/gated"},
            {"id": 2, "conclusion": "success", "jobs_url": "https://fixture.invalid/previous"},
        ]
    }
    get, calls = fake_get(pages, jobs_by_url={"https://fixture.invalid/gated": [{"conclusion": "skipped"}]})
    monkeypatch.setattr(github_api.requests, "get", get)

    run = github_api.get_last_workflow_run("stake-dao", "tg-bots", "vlsdt")

    assert run["id"] == 2
    assert [url for url, _ in calls] == [
        "https://api.github.com/repos/stake-dao/tg-bots/actions/workflows/vlsdt.yml/runs",
        "https://fixture.invalid/gated",
        "https://fixture.invalid/previous",
    ]
    assert calls[0][1] == {"status": "success", "per_page": 100}
