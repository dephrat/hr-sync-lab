# hr-sync-lab

Priya, a sales rep in Toronto, is promoted to team lead in Workday on Monday. Company policy says every new manager completes "Manager Fundamentals" and the Ontario harassment-prevention course within 30 days. Those courses live in Ethena. Workday does not know about courses and Ethena does not know about the promotion, so today someone in Enablement notices and assigns the courses by hand, or nobody does. This repo is a sync between the two: it reads job changes from the HRIS, updates the learner in the learning platform, checks the right training was enrolled, logs every action, is safe to rerun, and puts anything it is unsure about in a review queue for a person. Both systems are mocks with fake data.

![demo: seed, run, queue, resolve, report](demo/demo.gif)

The GIF shows the command line flow and predates the approvals inbox; the hosted page is the current demo.

## What it does

One `hrsync run` (add `--changes-only` to skip the final step on a large tenant):

1. Reads the watermark from `data/sync.db` and fetches Workday job changes completed since then
   (`GET /jobChanges?completedFrom=...`).
2. Skips any change that already has a finishing row in the audit log, so reruns do not repeat work;
   a change that failed halfway is processed again.
3. Finds the Ethena learner: saved mapping first, then work email. Two learners with one email, or
   none (with `--no-create`), go to the review queue instead of being guessed.
4. For a termination, sets the learner to `TERMINATED`. Otherwise maps job profile and location through
   `hrsync/rules.default.json` to learner attributes; an unknown profile or location is queued.
5. Patches only the attributes that differ. Ethena has no "assign course" call: it enrolls people by
   campaign audience, so correct attributes are what trigger enrollment.
6. Checks the expected campaigns now appear for the learner and queues `ENROLLMENT_MISSING` if not.
   On any HTTP error the run stops and the watermark stays put.
7. Runs steps 3 to 6 again for every worker the job changes did not touch (terminated ones too), so
   drift with no Workday event, such as a hand-edited learner, is fixed in the same run.

## Architecture

```
 hris/  mock Workday  :8001 ----+                +---- ethena/  mock Ethena  :8002
   data/hris.db                 |                |       data/ethena.db
                                v                v
                     hrsync/  engine (rules table, CLI)
                                  |          |
                               clients     store  data/sync.db
                                  |          |    (watermark, id_map, audit, review_queue)
                                  +----+-----+            |
                                       |                  |
              mcp_server/  7 tools over stdio      reconcile/  SQL drift report
              (Claude Desktop; uses clients+store) (attaches all three .db files)
                                       |
              webapp/  one-page demo, mounts both mocks and the engine
              (hrsync web, :8003; see Demo page below)
```

## Demo page

`webapp/` is a single page that shows the whole story without a terminal. Run it locally with `uv run hrsync seed && uv run hrsync web`, then open http://127.0.0.1:8003/. The page runs behind a secret path on a private host; deploy/README.md has the procedure.

Course audiences are editable in the mock Ethena (Courses sub-tab): each course takes one or more groups (any group matching is enough), and changing them enrolls matching people straight away, as Ethena's own audience engine would.

The page opens clean: Workday and Ethena agree on all 100 people, and the out-of-sync panel on the Sync tab is empty. The main story:

1. In the Sync tab, press **Demo a bad day**. It injects a messy Monday: 12 job changes Ethena has not heard about, a duplicate Ethena account, 3 people missing from Ethena, and 2 overdue enrollments.
2. Read the out-of-sync panel: 13 people, each with one plain sentence (5 attribute changes, 3 people with a location not in the rules table, 3 with no Ethena account, 1 leaver still active, 1 shared email).
3. Press Run sync. It fixes 9 of them and leaves 4 for manual review; the panel drops to those 4.
4. Work the queue (map the duplicate to the right learner, or skip a Vancouver item with a note), then read the Report tab.
5. Or fix the cause: in Translation rules on the Sync tab, set the Vancouver office to Canada and British Columbia, run sync again, and the manual-review items for Vancouver clear on their own.

A second story shows one change end to end:

1. Open Workday (mock), search for Lucas Fischer (W-1014, a Toronto software engineer), and submit a Promotion to Engineering Manager. Like real Workday, the change waits for approval: press Approve in the Approvals box above the search. Only then does the worker record change.
2. In the Ethena tab, check that his learner shows manager = no and has no Manager Fundamentals.
3. In the Sync tab, press Run sync (it also checks everyone else against Workday and fixes drift with no job change). The sentence for him reads "Set isManager=true in Ethena. Verified: ..., Manager Fundamentals".
4. Read the sentences the page prints for each change: what was patched, what was queued and why.
5. Check Ethena again: his attributes changed and he is enrolled in Manager Fundamentals.
6. Resolve a queue item in the Sync tab, then read the Report tab to see the drift that is left.

You can also hire someone in Workday (a hire completes in one step, no approval) and take a course in Ethena: open a learner, press Take course on an unfinished enrollment, read the short lesson and answer the question correctly to complete it. Both show the sync leaving each system's own data alone. Deny in the Approvals box discards a submitted change without touching the worker.

Reset demo puts all three databases back to the clean seed. Demo a bad day only works once per reset; pressing it again says so.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```
uv sync
uv run hrsync seed      # clean: 100 workers, 100 matching learners, in data/
uv run hrsync bad-day   # the messy Monday: 12 job changes, 1 duplicate, 3 missing, 2 overdue
uv run hrsync serve     # leave running: Workday mock on :8001, Ethena mock on :8002
```

In a second terminal:

```
uv run hrsync report                    # after bad-day, before the first run: one terminated-but-active row
uv run hrsync run                       # changes_seen 100 (12 job changes + 88 other workers),
                                        #   patched 5, created 3, terminated 1, queued 4, ok 87
uv run hrsync queue                     # three UNKNOWN_LOCATION (Vancouver, BC), one AMBIGUOUS_LEARNER (W-1042)
uv run hrsync resolve 1 --skip --note "location not in rules yet"
uv run hrsync report                    # drift report, also written to data/report.md
```

Run `hrsync report` before and after the first `run` to see the terminated-but-active row disappear. The first run's patches include the bad day's version of Priya: Chloe Chen (`W-1038`), a Toronto `Sales_Rep` promoted to `Sales_Team_Lead`, whose learner gets `isManager` true and is then enrolled in Manager Fundamentals next to Harassment Prevention (Ontario). Each change lands in exactly one summary bucket, so the seven counts add up to `changes_seen`.

`resolve <id> --map-learner <learner id>` is the other way to close an item: it saves the mapping and reprocesses that change. `run` exits 1 if any step failed; `resolve` exits 2 if the item is missing or closed, the learner is unknown, or a mock is unreachable, and exits 1 if the mapping was saved but the rerun change ended up queued or failed. Both print `cannot reach ...` instead of a traceback when `serve` is not running.

`hrsync seed` leaves the two systems matching. The bad day (`hrsync bad-day` or the Demo a bad day button) plants the problems: three people end up in `Vancouver, BC`, which is not in the rules table (two move there quietly and one by a transfer), worker `W-1042`'s email goes on a second learner and that worker gets a change, 3 workers lose their Ethena learner (the first run creates them), 2 enrollments go past their deadline, and one worker terminated in the HRIS stays active in Ethena until the first run. The 12 job changes land only in the HRIS, so the first run has real drift to fix. The bad day never touches the sync database.

## Use from Claude Desktop

Start the mock systems (`uv run hrsync seed && uv run hrsync serve`), then add this to
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hr-sync-lab": {
      "command": "uv",
      "args": ["run", "--directory", "<repo>", "hrsync-mcp"],
      "env": {
        "HRIS_URL": "http://127.0.0.1:8001",
        "ETHENA_URL": "http://127.0.0.1:8002",
        "ETHENA_API_KEY": "test-key"
      }
    }
  }
}
```

Five tools are read-only (`get_worker`, `get_learner_status`, `get_audit_trail`, `list_queue`, `explain_gap`); `resolve_queue_item` and `rerun_sync` write and do nothing unless called with `confirm=true`. That flag makes the write a separate, explicit call whose arguments show the intent; the person approves it in Claude Desktop's own tool-approval prompt. The model fills in the flag itself, so it is not proof that a person approved. `explain_gap` checks enrollment, the open queue, the rules and the audit log and answers in one sentence, for example why a worker is not in a given campaign.

## Why mocks

Neither Workday nor Ethena offers a public sandbox. The mocks follow the published API shapes (Workday Staffing REST envelopes and paging; the Ethena OpenAPI 1.4.0 spec with HTTP Basic and cursor paging), and the clients in `hrsync/clients.py` are written against those shapes behind two small interfaces. Pointing at a real tenant means changing the base URL and auth. Where the mocks simplify the vendors is listed in [docs/design.md](docs/design.md#what-is-mocked-and-why).

## What's deliberately left out

ADP and payroll, n8n (the queue notifications and review loop would be the n8n piece), a scheduler (the runbook has the cron line), and real auth on the mocks. The page runs behind a secret path on a private host; deploy/README.md has the procedure. The sync itself is not scheduled anywhere. [docs/design.md](docs/design.md#deliberately-left-out) says how each would be added.

## Tests

```
uv run pytest    # 125 tests
```

## Docs

- [docs/design.md](docs/design.md): problem, components, data flow, what is mocked, what is out of scope
- [docs/ownership-map.md](docs/ownership-map.md): which HR team, IT and the automation builder own what
- [docs/runbook.md](docs/runbook.md): running, scheduling, every queue reason code, failure modes, rules changes
- [docs/patterns.md](docs/patterns.md): reusable patterns for teams building agents on business systems
- Decision records: [0001 polling](docs/decisions/0001-polling-not-webhooks.md), [0002 attributes, not assignments](docs/decisions/0002-attributes-not-assignments.md), [0003 review queue](docs/decisions/0003-review-queue-not-guessing.md), [0004 idempotency](docs/decisions/0004-idempotency-key.md), [0005 read-only agent](docs/decisions/0005-agent-read-only-by-default.md)
