# Telegram bots

Standalone extraction for the nine active Stake DAO bots. Workflows remain dispatch-only; production cutover is a separate change. No private checkout, private Git history, Ape, deployment tooling or VoteMarket proof toolkit is required.

| Workflow file / preserved name | Entrypoint under `script/bots/` | State | Prepared UTC cron |
| --- | --- | --- | --- |
| `asdcrv.yml` / Bot Llamalend asdcrv | `asdcrv/main.py` | Actions logs | `*/5 * * * *` |
| `curve-pools.yml` / Bot Lockers | `curve/pools/main.py` | Actions logs | `*/5 * * * *` |
| `daily-recap.yml` / Bot Daily recap lockers & SDT | `daily/lockers/main.py`, `staking_v2_daily/main.py`, `morpho_daily/main.py`, sequentially | None | `0 11 * * *` |
| `morpho.yml` / Bot Morpho Stake DAO | `morpho/main.py` | Actions logs | `*/5 * * * *` |
| `onlyboost_v2.yml` / Bot Only Boost V2 | `onlyboost_v2/main.py` | Redis | `*/5 * * * *` |
| `vlcvx-delegation.yml` / Bot vlCVX delegation | `vlcvx_delegation/main.py` | Actions logs | `*/5 * * * *` |
| `vlsdt.yml` / Bot vlSDT | `vlsdt/main.py` | Actions logs | `*/5 * * * *` |
| `votemarket-incentives.yml` / Bot Votemarket Direct Incentives | `votemarket_incentives/main.py` | Redis | `*/5 * * * *` |
| `votemarket-v2.yml` / Bot Votemarket | `votemarket/v2/votemarket.py` | Actions logs | `*/10 * * * *` |

`vesdt.yml` and `vesdt-migration.yml` were manually disabled, last run on 2026-04-27. Their workflows are removed; their scripts are outside this extraction.

## Runtime ownership evidence

On 2026-10-09, GitHub reported `stake-dao/tg-bots` public, default branch `main`, nine active bot workflows and two manually disabled legacy workflows. All nine sampled successful runs were dispatched by `stake-dao-maestro[bot]`. Their job steps contain `dispatch_id:tg-bot-…:…:step:run-1`, matching the nine orchestrator pipelines. See [runtime-evidence.json](cutover/runtime-evidence.json) for run IDs, times and checkpoint markers; raw logs and credential values are excluded.

Sampled five-minute dispatches occurred at 14:30, 14:35, 14:40 and 14:45 UTC; VoteMarket V2 ran at 14:29 and 14:40; daily recap ran at 11:00 on October 8 and 9. YAML `paused: true` does not describe runtime ownership: Maestro's `packages/core/src/lib/schedule-client.ts` preserves existing schedule state on update and uses YAML pause state only when creating schedules. Direct Temporal schedule descriptions and outstanding Maestro runs were not inspected; those remain cutover prerequisites.

## Dependencies and secrets

Python 3.10.13; seven direct public PyPI dependencies in `requirements.txt`. Required ABI/config files are under `json/`; existing public `data/` remains intact. Bots still read their existing external feeds: Stake DAO Data Hub, public locker metadata, Morpho GraphQL, VoteMarket API, Merkl incentives, IPOR/Beefy metadata, Curve pool APIs, Etherscan, RPCs, and price APIs. Extraction does not replace those services.

Provision Actions secrets without logging their values:

| Name | Use |
| --- | --- |
| `BOT_API_KEY` | Activity notifications: asdcrv, lockers, daily recap, Morpho, OnlyBoost, vlCVX delegation, vlSDT |
| `BOT_VOTEMARKET_API_KEY` | VoteMarket V2 and incentives notifications |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` | Same existing Redis endpoint, port and password for OnlyBoost and incentives; TLS and database 0 unchanged |
| `ETHERSCAN_API_KEY` | Existing explorer credential; asdcrv, lockers, Morpho, vlCVX delegation, vlSDT |
| `EXPLORER_KEY` | Optional preferred explorer key; falls back to `ETHERSCAN_API_KEY` |
| `WEB3_ALCHEMY_API_KEY` | Existing RPC credential; current Alchemy endpoint behavior retained |
| `ROUTEMESH_API_KEY` | Optional preferred RPC provider; existing fallback order retained |

`GITHUB_TOKEN` comes from `${{ github.token }}`, with `actions: read` and `contents: read`; no PAT/private checkout is needed. Missing Telegram or Redis secrets fail before bot execution. Explorer/RPC service availability must be verified at cutover. No Hasura, worker authentication, Fraxscan, BSC RPC or Fraxtal RPC secret is required by the extracted code. Optional nonproduction helper tokens and `TEST_TELEGRAM_CHAT_ID` remain environment-backed; no private test-channel identifier is published. Validation does not use them.

The observed secret-name inventory contained `BSC_RPC`, `ETHERSCAN_API_KEY`, `FRAXSCAN_API_KEY`, `FRAXTAL_RPC`, `GIT_ACCESS_TOKEN`, `HASURA_SECRET`, `WEB3_ALCHEMY_API_KEY`. Telegram and Redis secrets were not configured under the new names. Secrets were inventoried by name only. Provision matching Telegram bot identities and unchanged Redis connection settings before deploying extraction; rotation of exposed credentials requires coordination with the old scripts until they retire.

## Checkpoint continuity

Workflow filenames and notification channels remain unchanged. Log recovery stays in `stake-dao/tg-bots`: `Chain <chain_id> / last block <next_from_block>` remains the checkpoint format. Recovery excludes the current run and successful runs whose jobs were all skipped; it selects the newest completed successful execution among the latest 100 successful runs. Unavailable run/job/log APIs or logs with no checkpoint markers abort. This replaces positional selection, which would pick skipped cron runs. The lockers and VoteMarket outer entrypoints now propagate checkpoint errors instead of exiting successfully.

No logs are seeded into a new repository. Retain Actions logs through cutover and archive the last successful run IDs/markers operationally before their retention expires. Missing individual chain markers keep the existing behavior; compare expected chains against current logs before activation. The sample showed lockers markers for chains 1 and 56, and VoteMarket V2 markers for 10, 8453 and 42161; that sample does not prove every configured chain completed.

OnlyBoost retains:

- `tg-bot:onlyboost-v2:last_blocks`, hash of chain ID to next block.
- `tg-bot:onlyboost-v2:seen_events`, set of transaction/log IDs, seven-day TTL.
- Existing bootstrap, backfill caps and per-chain commit-after-delivery behavior.

Incentives retains:

- `tg-bot:votemarket-incentives:seen_ids`.
- `tg-bot:votemarket-incentives:gauge_names`, one-day cache.
- `tg-bot:votemarket-incentives:gauge_tvls`, one-hour cache.

No state is moved, cleared, recreated or expired by migration tooling. Redis socket connection/read timeouts are bounded at 10/30 seconds. Whole workflows have a 20-minute timeout and one concurrency group per bot, with `cancel-in-progress: false`; manual and scheduled runs share the group. GitHub keeps at most one running and one pending run and may replace pending runs. This differs from Maestro's `TERMINATE_OTHER`: active executions finish instead of being terminated.

Existing delivery limitations remain: most log-backed bots ignore Telegram's false return; incentives can mark an ID seen after delivery failure; several bots catch per-event/per-chain failures and may finish successfully. A successful Actions conclusion alone does not prove complete delivery or all-chain coverage. Partial delivery requires reconciling messages and checkpoints before replay; migration does not provide exactly-once delivery.

## Validation

Failure cases enumerated before isolated execution:

- Missing Telegram, Redis or GitHub credentials: abort before delivery/state access where preflight or checkpoint recovery requires them.
- RPC/API/state unavailable: block outbound traffic in validation; exercise checkpoint/RPC failures and existing Redis failure tests.
- Missing checkpoint: log download or missing markers abort; preserve OnlyBoost's existing new-chain bootstrap.
- Overlapping runs: validate stable concurrency groups shared by manual and scheduled triggers; confirm the old scheduler is drained before enabling crons.
- Partial delivery: preserve existing behavior and inspect existing OnlyBoost false-send/dedup tests; reconcile the inherited limitations above before replay.
- Restart: recover the old Actions log format and advance only the subsequent block window; existing Redis tests cover checkpoint/dedup reuse.

Run locally from this repository:

```sh
python3.10 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest==8.2.2
.venv/bin/python tools/validate.py
actionlint
git apply --check cutover/github-crons.patch
git diff --check
```

`tools/validate.py` clears credential environment variables, disables dotenv loading, blocks socket connection/DNS APIs before imports, then imports every extracted module and runs pytest with plugin autoload disabled. `PROD=False` alone is unsafe: the original helper redirects delivery to a test Telegram channel. OnlyBoost `DRY_RUN` alone is not a global safety boundary. Do not run entrypoints directly as validation.

Verified locally: clean public dependency installation, compatible installed packages, every extracted module imported with credentials absent/network blocked, 67 existing checks plus five isolated full-entrypoint vlCVX cases, workflow lint, cron patch applicability/lint, extraction/state contract comparisons, and publication credential scan. The full-entrypoint cases cover old log recovery, current/skipped run exclusion, Telegram payload/channel, restart without repeat delivery, missing GitHub credentials, API failure, missing markers and RPC failure. Existing checks cover OnlyBoost delivery/dedup/Redis failures, user and Beefy resolution, delegation formatting and daily locker delegation reporting, and Data Hub behavior.

No fresh credentialed RPC/API/Redis validation, real Telegram delivery, runner-provided token verification, actual concurrency execution, full daily recap execution or Temporal drain proof was performed. Existing tests cover selected code paths; the other bot entrypoints were import-validated, not fully replayed. Production activation remains pending. Follow [the cutover procedure](cutover/README.md).
