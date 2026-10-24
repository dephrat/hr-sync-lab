# Runbook

For whoever operates the sync day to day. You do not need to read the code to use this page.

## What the sync does, in one paragraph

Each run asks Workday for job changes completed since the last successful run, works out which Ethena learner each change belongs to, writes that person's learner attributes (manager or not, country, state or province category, department) to Ethena, and checks that Ethena enrolled them in the training those attributes call for. Everything it does is written to the `audit` table in `data/sync.db`. Anything it cannot decide goes to the review queue for HR Operations.

## Running it

All commands run from the repository root. Settings come from environment variables; the defaults point at the local mocks.

| Variable | Default | Meaning |
|---|---|---|
| `HRIS_URL` | `http://127.0.0.1:8001` | Workday base URL |
| `ETHENA_URL` | `http://127.0.0.1:8002` | Ethena base URL |
| `ETHENA_API_KEY` | `test-key` | Ethena Basic-auth password |
| `SYNC_DB` | `./data/sync.db` | Watermark, id map, audit log, review queue |
| `HRIS_DB`, `ETHENA_DB` | `./data/hris.db`, `./data/ethena.db` | Mock databases (also read by `report`) |
| `RULES_PATH` | `hrsync/rules.default.json` | Rules table |

```
uv run hrsync run              # one pass; prints a summary table
uv run hrsync run --json       # same summary as one JSON line, for logs
uv run hrsync run --no-create  # queue unknown people instead of creating learners
uv run hrsync queue            # open review items (add --all to include closed ones)
uv run hrsync resolve 7 --map-learner L100037 --note "confirmed with manager"
uv run hrsync resolve 7 --skip --note "contractor, no training needed"
uv run hrsync report           # drift report, printed and written to data/report.md
```

The summary has `run_id`, `changes_seen` and seven buckets: `patched`, `created`, `terminated`, `queued`, `skipped`, `failed`, `ok`. Each change counts in exactly one bucket, its final outcome, so the buckets add up to `changes_seen`. A change that created a learner and was then queued counts as `queued`. `ok` means the learner already had the right attributes and enrollment was verified.

`run` exits 0 when every change was handled (queued counts as handled) and 1 when any step failed or a mock could not be reached (it prints `cannot reach ...`, no traceback). `resolve` exits 2 when the item does not exist or is not open, when the learner id is unknown to Ethena, or when a mock could not be reached.

## Schedule

Every 15 minutes on weekdays, appending the JSON summary to a log:

```
*/15 * * * 1-5  cd /opt/hr-sync-lab && /usr/local/bin/uv run hrsync run --json >> /var/log/hrsync.log 2>&1
```

Alert on a non-zero exit. Running more often is safe: a run with no new changes skips the last change it already handled and does nothing else.

## Review queue: reason codes and what HR Ops does

Each open item names a worker, a job change and a reason. A queued change is not retried by later runs; it waits for a person.

| Reason | What happened | What HR Ops does |
|---|---|---|
| `UNKNOWN_LOCATION` | The worker's Workday location is not in the rules table, so the sync does not know which country or province rules apply. Nothing was written to Ethena. | Ask the automation builder to add the location (see below). Then `hrsync resolve <id> --map-learner <learner id>` reruns the change with the new rule (how to find the learner id is below the table). If the location should never get training, `--skip` with a note. |
| `UNKNOWN_PROFILE` | Same, for the job profile. | Same as above, adding the profile. |
| `AMBIGUOUS_LEARNER` | Two or more Ethena learners share the worker's email (the detail lists their ids). | Decide which learner is the real person, then `hrsync resolve <id> --map-learner <that id>`. Ask Enablement to deactivate the duplicate in Ethena. |
| `NO_LEARNER_MATCH` | No Ethena learner has the worker's email, and the sync was told not to create one (`--no-create`), or the change is a termination for someone never in Ethena. | If the person exists in Ethena under another email, map them with `--map-learner`. If it is a termination with no learner, `--skip`. Otherwise ask Enablement to create the learner, then map them with `--map-learner`. A later run without `--no-create` does not pick the change up again, because it is already queued. |
| `ENROLLMENT_MISSING` | Attributes were written, but Ethena did not enroll the person in every campaign the rules expect (the detail names them). Usually a campaign is inactive or its audience in Ethena differs from the rules table. | Send the detail to Enablement to check the campaign's status and audience. Once fixed, `--map-learner` with the same learner id to recheck. |

`--map-learner` saves the worker-to-learner mapping, closes the item as resolved and reprocesses that one job change. `--skip` closes it without touching Ethena. Both record the note in the `resolution` column. If the reprocessed change ends up `queued` or `failed` (for example a new `UNKNOWN_LOCATION`), `resolve` keeps the mapping, prints `item N resolved (mapping saved) but change X is now: queued (...)` and exits 1, so check `hrsync queue` for the new item.

To find a worker's learner id: `sqlite3 data/sync.db "SELECT learner_id FROM id_map WHERE worker_id='W-1040'"`, or ask the assistant to call `get_worker`. If there is no row, search Ethena for the worker's email.

## Failure modes

**Rate limited (HTTP 429) or any other HTTP error.** The run writes a `failed` audit row naming the step (for example `patch_learner`), stops, prints `failed 1`, exits 1, and does not move the watermark. The next run picks up the same changes. Steps that already succeeded are safe to repeat: patching writes only fields that still differ, and creating is skipped because the learner is now in the id map. No action needed beyond letting the next scheduled run go; if it keeps failing, see "watermark stuck".

**A system is down.** If Workday is unreachable the run fails before processing anything (`changes_seen 0, failed 1`). If Ethena is down the first change that needs it fails. Either way nothing is lost; the next run after recovery catches up. Tell IT if it lasts more than an hour.

**Watermark stuck.** Symptom: every run exits 1 and the same job change keeps failing. Find it:

```
sqlite3 data/sync.db "SELECT created_at, change_id, worker_id, action, detail FROM audit WHERE outcome='failed' ORDER BY id DESC LIMIT 5"
```

A 401 means the Ethena key is wrong (IT). A 404 or 422 on one learner usually means the learner was deleted or the record is malformed in Ethena (Enablement). The current watermark is `SELECT value FROM watermark`. Do not edit it by hand unless you intend to skip changes; if you must, record why.

**Duplicate email.** Shows up as `AMBIGUOUS_LEARNER` in the queue; the sync never picks one of the two learners on its own. Handle as in the table above.

## Credential rotation (IT)

The Ethena key is read from `ETHENA_API_KEY` at the start of each run. To rotate: issue the new key in Ethena, update the secret where the cron job reads its environment, confirm the next run exits 0, then revoke the old key. A run that started with the old key and fails with 401 is harmless; the next run retries. Workday credentials follow the same pattern once a real tenant is wired in (`HRIS_URL` plus the auth set in `hrsync/cli.py:_build_clients`).

## Adding a location or a job profile to the rules table

Edit `hrsync/rules.default.json` (or the file `RULES_PATH` points at). The location key must match the Workday location descriptor exactly, and the profile key must match the Workday job profile id.

```json
"locations": {
  "Vancouver, BC": {"country": "CAN", "customCategory": "BC"}
},
"profiles": {
  "Store_Manager": {"isManager": true}
}
```

A location sets `country` and either `state` (US) or `customCategory` (Canadian province). A profile sets `isManager` and may set `customCategory`, which then wins over the location's (that is how warehouse roles get the warehouse safety campaign). If the new values should enroll people in a campaign, add or check the matching entry in `campaigns` and confirm with Enablement that the campaign audience in Ethena uses the same attributes. Then run `uv run pytest`, deploy the file, and resolve the queued items with `--map-learner`.

## Web page

`hrsync web` serves the demo page (see [design-ui.md](design-ui.md)). HR Operations uses it to work the review queue without a terminal: the Queue tab lists open items with the reason code, and each can be skipped with a note or closed by picking the learner it belongs to, which is the same action as `hrsync resolve`. Run sync starts one sync pass and prints a sentence per change. Report shows the drift report.

Reset puts the workers, learners, sync state and queue back to the seed. It is meant for the demo and wipes everything the page created, so never run it against data anyone cares about.

The deployed page sits behind a secret path prefix: `https://hrsync.ephrat.ai/<key>/`. The key is in `/docker/hrsync/.env` on the VPS and in `~/.hrsync-demo-key` on the maintainer's Mac. It is never stored in the repo. To rotate it, follow the last line of [deploy/README.md](../deploy/README.md).
