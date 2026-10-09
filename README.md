# Telegram bots

Stake DAO Telegram bots, scheduled by GitHub Actions. Each cron requires its `BOT_CRON_*` repository variable to equal `true`. Workflows also support manual dispatch.

## Bots

Entrypoints are relative to `script/bots/`. All schedules use UTC.

| Workflow | Entrypoint | State | Cron |
| --- | --- | --- | --- |
| `asdcrv.yml` | `asdcrv/main.py` | Actions logs | `*/5 * * * *` |
| `curve-pools.yml` | `curve/pools/main.py` | Actions logs | `*/5 * * * *` |
| `daily-recap.yml` | `daily/lockers/main.py`, `staking_v2_daily/main.py`, `morpho_daily/main.py` | None | `0 11 * * *` |
| `morpho.yml` | `morpho/main.py` | Actions logs | `*/5 * * * *` |
| `onlyboost_v2.yml` | `onlyboost_v2/main.py` | Redis | `*/5 * * * *` |
| `vlcvx-delegation.yml` | `vlcvx_delegation/main.py` | Actions logs | `*/5 * * * *` |
| `vlsdt.yml` | `vlsdt/main.py` | Actions logs | `*/5 * * * *` |
| `votemarket-incentives.yml` | `votemarket_incentives/main.py` | Redis | `*/5 * * * *` |
| `votemarket-v2.yml` | `votemarket/v2/votemarket.py` | Actions logs | `*/10 * * * *` |

Cron variables follow the workflow name in uppercase with hyphens replaced by underscores, such as `BOT_CRON_CURVE_POOLS` and `BOT_CRON_VOTEMARKET_V2`.

## Configuration

Python 3.10.13; dependencies are pinned in `requirements.txt`. ABI and configuration files live under `json/`.

Configure these GitHub Actions secrets:

| Secret | Use |
| --- | --- |
| `BOT_API_KEY` | Activity notifications |
| `BOT_VOTEMARKET_API_KEY` | VoteMarket notifications |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` | OnlyBoost and incentives state; TLS, database 0 |
| `ETHERSCAN_API_KEY` | Explorer access |
| `EXPLORER_KEY` | Optional preferred explorer key; falls back to `ETHERSCAN_API_KEY` |
| `WEB3_ALCHEMY_API_KEY` | Alchemy RPC access |
| `ROUTEMESH_API_KEY` | Optional preferred RPC provider |

Workflows supply `GITHUB_TOKEN` automatically with `actions: read` and `contents: read` permissions.

## State and execution

Log-backed bots recover block checkpoints from the latest successful executed workflow run. Keep workflow filenames stable and retain checkpoint-bearing Actions logs. Missing logs or checkpoint markers abort recovery.

OnlyBoost stores checkpoints and event deduplication in `tg-bot:onlyboost-v2:*` Redis keys. Incentives stores seen IDs and gauge caches in `tg-bot:votemarket-incentives:*`; IDs are marked seen after confirmed Telegram delivery.

Each workflow has a 20-minute timeout and a shared concurrency group for scheduled and manual runs. Running jobs finish before the next starts. GitHub scheduling may be delayed, and pending runs may be replaced. Check job logs for chain coverage and delivery errors before replaying a run.

## Validation

```sh
uv run --python 3.10 --with-requirements requirements.txt --with pytest==8.2.2 python tools/validate.py
actionlint
git diff --check
```

The validation script clears credentials, disables dotenv loading, blocks network access, imports the bot modules, and runs the test suite. Use it for local checks: running a bot entrypoint directly can send Telegram messages or update Redis state.
