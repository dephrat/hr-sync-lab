# hr-sync-lab

Priya, a sales rep in Toronto, is promoted to team lead in Workday on Monday. Company policy says every new manager completes "Manager Fundamentals" and the Ontario harassment-prevention course within 30 days. Those courses live in Ethena. Workday does not know about courses and Ethena does not know about the promotion, so today someone in Enablement notices and assigns the courses by hand, or nobody does. This repo is a sync between the two: it reads job changes from the HRIS, updates the learner in the learning platform, checks the right training was enrolled, logs every action, is safe to rerun, and puts anything it is unsure about in a review queue for a person. Both systems are mocks with fake data.

![demo: seed, run, queue, resolve, report](demo/demo.gif)

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

The story takes six clicks:

1. Open Workday (mock), search for Lucas Fischer (W-1014, a Toronto software engineer), and record a Promotion to Engineering Manager.
2. In the Ethena tab, check that his learner shows manager = no and has no Manager Fundamentals.
3. In the Sync tab, press Run sync (it also checks everyone else against Workday and fixes drift with no job change). The sentence for him reads "Set isManager=true in Ethena. Verified: ..., Manager Fundamentals".
4. Read the sentences the page prints for each change: what was patched, what was queued and why.
5. Check Ethena again: his attributes changed and he is enrolled in Manager Fundamentals.
6. Resolve a queue item in the Sync tab, then read the Report tab to see the drift that is left.

You can also hire someone in Workday and mark training complete in Ethena, to see the sync leave each system's own data alone.

Reset demo puts all three databases back to the seed.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```
uv sync
uv run hrsync seed      # 100 workers, 98 learners, 12 job changes, in data/
uv run hrsync serve     # leave running: Workday mock on :8001, Ethena mock on :8002
```

In a second terminal:

```
uv run hrsync report                    # before the first run: one terminated-but-active row
uv run hrsync run                       # changes_seen 100 (12 job changes + 88 other workers),
                                        #   patched 5, created 3, terminated 1, queued 4, ok 87
uv run hrsync queue                     # three UNKNOWN_LOCATION (Vancouver, BC), one AMBIGUOUS_LEARNER (W-1042)
uv run hrsync resolve 1 --skip --note "location not in rules yet"
uv run hrsync report                    # drift report, also written to data/report.md
```

Run `hrsync report` before and after the first `run` to see the terminated-but-active row disappear. The first run's patches include the seed's version of Priya: Chloe Chen (`W-1038`), a Toronto `Sales_Rep` promoted to `Sales_Team_Lead`, whose learner gets `isManager` true and is then enrolled in Manager Fundamentals next to Harassment Prevention (Ontario). Each change lands in exactly one summary bucket, so the seven counts add up to `changes_seen`.

`resolve <id> --map-learner <learner id>` is the other way to close an item: it saves the mapping and reprocesses that change. `run` exits 1 if any step failed; `resolve` exits 2 if the item is missing or closed, the learner is unknown, or a mock is unreachable, and exits 1 if the mapping was saved but the rerun change ended up queued or failed. Both print `cannot reach ...` instead of a traceback when `serve` is not running.

The seed plants problems on purpose: `Vancouver, BC` is not in the rules table (three people are there: two from the start and one transferred in), worker `W-1042`'s email is on two learners and that worker has a seeded change, 3 workers are missing from Ethena (the first run creates them), 2 enrollments are overdue, and one terminated worker is still active in Ethena until the first run. Ethena is seeded from the workers as they were before the 12 job changes, so the first run has real drift to fix.

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
uv run pytest    # 84 tests
```

## Docs

- [docs/design.md](docs/design.md): problem, components, data flow, what is mocked, what is out of scope
- [docs/ownership-map.md](docs/ownership-map.md): which HR team, IT and the automation builder own what
- [docs/runbook.md](docs/runbook.md): running, scheduling, every queue reason code, failure modes, rules changes
- [docs/patterns.md](docs/patterns.md): reusable patterns for teams building agents on business systems
- Decision records: [0001 polling](docs/decisions/0001-polling-not-webhooks.md), [0002 attributes, not assignments](docs/decisions/0002-attributes-not-assignments.md), [0003 review queue](docs/decisions/0003-review-queue-not-guessing.md), [0004 idempotency](docs/decisions/0004-idempotency-key.md), [0005 read-only agent](docs/decisions/0005-agent-read-only-by-default.md)
