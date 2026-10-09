# Cutover and rollback

Extraction was merged in tg-bots #25. Telegram-only scheduler retirement was merged in orchestrator #129; Guard promotion #128 remains separate. That retirement removed the nine schedule blocks. The follow-up removes the nine manual pipeline definitions from Maestro's catalog; manual dispatch remains available in tg-bots. Task queues are unchanged.

Crons live in the nine workflow files. All nine repository variables were verified enabled after retirement deployment on October 9.

Each scheduled job requires its own repository variable to equal the exact string `true`. Missing/false variables skip scheduled jobs. Manual dispatch remains available. Do not enable these variables while Maestro owns the bots.

| Maestro pipeline / schedule suffix | Workflow | GitHub variable |
| --- | --- | --- |
| `tg-bot-asdcrv` | `asdcrv.yml` | `BOT_CRON_ASDCRV` |
| `tg-bot-curve-pools` | `curve-pools.yml` | `BOT_CRON_CURVE_POOLS` |
| `tg-bot-daily-recap` | `daily-recap.yml` | `BOT_CRON_DAILY_RECAP` |
| `tg-bot-morpho` | `morpho.yml` | `BOT_CRON_MORPHO` |
| `tg-bot-onlyboost-v2` | `onlyboost_v2.yml` | `BOT_CRON_ONLYBOOST_V2` |
| `tg-bot-vlcvx-delegation` | `vlcvx-delegation.yml` | `BOT_CRON_VLCVX_DELEGATION` |
| `tg-bot-vlsdt` | `vlsdt.yml` | `BOT_CRON_VLSDT` |
| `tg-bot-votemarket-incentives` | `votemarket-incentives.yml` | `BOT_CRON_VOTEMARKET_INCENTIVES` |
| `tg-bot-votemarket-v2` | `votemarket-v2.yml` | `BOT_CRON_VOTEMARKET_V2` |

## Cutover status — October 9

Orchestrator #129 deployed successfully at commit `f33c80b5d55807783e5c9116931782c8e1adb446`; the live Temporal UI lists 41 schedules and zero `schedule:pipeline:tg-bot-*` schedules. All nine old schedules were paused and bot pipelines drained before extraction/canaries. Guard promotion #128 remains open with only four non-bot pipeline files.

Nine production canaries ran on extraction commit `c5bb2a38a03409c29efeea6da6fc5a9e3a8de55b`. Lockers run `37958292558` failed on obsolete native event-filter keywords. VoteMarket run `37958322626` completed despite 18 identical scan errors and emitted unusable advanced checkpoints. OnlyBoost run `37958305666` skipped Ethereum, Arbitrum and Sonic on unprefixed event topics; existing Redis checkpoints for failed chains remain the recovery source. Daily recap run `37958296090` completed all three scripts but reported `NOTOK` while fetching vlSDT delegatees.

PR #26 fixes those Web3 compatibility errors, makes VoteMarket scan failures fail the job, and adds inert crons. Local validation passes 83 isolated tests. Main protection is an active organization ruleset with required review and no bypass actors; the current account has repository admin access but cannot bypass that ruleset as configured.

Activation is held: merge the fixes through the approved protection process; prevent invalid VoteMarket run `37958322626` from being selected as a checkpoint; rerun Lockers, VoteMarket and OnlyBoost with valid checkpoint continuity; resolve the recap delegatee warning. Do not enable cron variables or replay from the invalid VoteMarket markers. Deleting that invalid Actions run is destructive and requires explicit authorization; preserve sanitized evidence first. The final valid VoteMarket run is `37956890176`: Arbitrum `513247130`, Base `52386346`, Optimism `157981640` (next blocks).

## Timing changes

Seven interval bots retain five-minute UTC wall-clock slots. Daily recap retains 11:00 UTC. VoteMarket V2 changes from a continuous 11-minute interval to `*/10` (six wall-clock slots/hour, about 10% more runs). `*/11` would produce minute slots 0, 11, 22, 33, 44, 55 and a five-minute gap at the hour boundary; it does not preserve an 11-minute interval.

All intervals become wall-clock crons rather than Temporal elapsed intervals. GitHub scheduling can be delayed or dropped, runs only on the default branch, and public repositories may have schedules disabled after 60 days of inactivity. There is no replacement for Temporal's 30-minute/one-day catchup window or automatic step retries; checkpoint backfill remains subject to existing bot caps. See [GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) and [concurrency behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

The 20-minute execution cap replaces unbounded workflow execution. Concurrency queues rather than terminates a running predecessor. Observe actual runtimes and API limits during the authorized canary; do not equate cron presence with a delivery guarantee.

## Activation procedure

1. Provision the secrets listed in the repository README, using the existing Telegram bot identities and exact existing Redis TLS endpoint/port/database. Confirm repository Actions policy allows the public dependencies and `actions: read` token permissions. Preserve the last successful Actions logs. Resolve partial-chain coverage and inspect recent delivery failures before replaying anything.
2. Obtain authenticated live Maestro/Temporal descriptions for all nine `schedule:pipeline:<pipeline-id>` schedules and their outstanding pipeline runs. Record paused state, interval/calendar, next run, active/buffered executions and current Actions run IDs. Recheck dispatch actors/IDs: the saved October 9 evidence is a point-in-time attribution, not a lock against later ownership changes.
3. Pause all nine live Temporal schedules through the authenticated Maestro UI/API. The API route is `POST /schedules/<url-encoded-schedule-id>/pause` with admin authorization. Verify each description reports paused. Block operational manual triggers for the window. Wait for already-started Maestro runs, retries and queued/running Actions jobs to drain. Pause alone does not stop those executions. Do not cancel partially delivered jobs as a shortcut.
4. With the old scheduler paused/drained and GitHub cron variables unset/false, deploy the extraction change to `tg-bots/main`. Keep cron activation separate. Run one authorized manual canary per bot; this sends real messages and writes existing Redis state, so it is outside local validation. Compare the recovered block markers, Redis key identity and notification channels against the final old executions. Never run old and new canaries concurrently.
5. Deploy the orchestrator retirement diff, preserving non-bot pipelines. Reconcile schedules (`POST /schedules/sync`). The current compose configuration has `SCHEDULE_DELETE_ORPHANS: "true"`; verify it on the deployed server before relying on deletion. Verify all nine old schedules are absent from Temporal. If deletion is disabled or sync reports errors, explicitly delete only these nine schedule IDs through the authorized UI/API after drain. Do not merely leave YAML paused. Restart/reconcile once and confirm none are recreated. Keep remaining `maestro-bots` workers if other pipelines use them.
6. Publish the reviewed cron definitions on `tg-bots/main`, with all nine variables still unset/false. Confirm no scheduled job executes bot code. Verify newest successful executed-run lookup can still reach checkpoint-bearing logs despite any skipped runs.
7. After the old schedules are absent and both systems have no old queued executions, set each `BOT_CRON_*` variable to `true`. Verify the first scheduled runs use event `schedule`, self-checkout and the new script paths, and preserve state/notifications. Confirm no new Maestro dispatch IDs appear. Inspect all configured chains and daily recap's three script outcomes, not just Actions conclusions. Keep this as the sole scheduler.
8. Remove `GIT_ACCESS_TOKEN` from tg-bots only after checking remaining repository consumers; extraction no longer uses it. Rotate the credentials exposed by the private shared source through the authorized secret-management process, coordinating any remaining consumers. Do not publish private source history or secret-bearing evidence.

Global schedule reconciliation can affect other pipelines; review its response and deployment scope. If any prerequisite fails, keep both cron variables false and the old schedules paused until the failure is understood. Reactivation of the old owner follows rollback below.

## Rollback — requires separate authorization

1. Set every affected `BOT_CRON_*` variable to `false`. Prevent manual dispatch. Wait for running and pending Actions jobs and any manual canaries to finish; a variable change does not cancel jobs already started.
2. Preserve current Actions logs, next-block markers and Redis state. Reconcile partial deliveries before any replay. Never restore an older Redis snapshot or replay from an old block blindly.
3. If extraction code is the problem, revert it through the normal reviewed GitHub change while schedules are off. Restore the private checkout only if explicitly approved and its source remains compatible with the now-current shared state and credentials. The old positional checkpoint lookup cannot safely follow skipped cron runs; retain the new lookup or restore a reviewed checkpoint-compatible workflow before enabling it.
4. Restore the nine orchestrator pipeline definitions and their schedule blocks through a reviewed change. Reconcile while cron variables remain false. Deleted schedules are recreated paused because the original YAML says `paused: true`; verify actual descriptions rather than assuming this occurred.
5. Confirm GitHub has no running/pending bot jobs. Unpause only the affected live Maestro schedules. Verify dispatch attribution/tracking IDs and unchanged checkpoint/key continuity. Keep GitHub cron variables false or remove the cron definitions. Never enable both owners.

No automatic rollback resets state. A timeout, missing marker, partial delivery or expired log requires an explicit checkpoint/delivery reconciliation before restart.
