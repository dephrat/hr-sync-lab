# hr-sync-lab

Priya, a sales rep in Toronto, is promoted to team lead in Workday on Monday. Company policy says every new manager completes "Manager Fundamentals" and the Ontario harassment-prevention course within 30 days. Those courses live in Ethena. Workday does not know about courses and Ethena does not know about the promotion, so today someone in Enablement notices and assigns the courses by hand, or nobody does. This repo is a sync between the two: it reads job changes from the HRIS, updates the learner in the learning platform, checks the right training was enrolled, logs every action, is safe to rerun, and puts anything it is unsure about in a review queue for a person. Both systems are mocks with fake data.

![demo: seed, run, queue, resolve, report](demo/demo.gif)

## What it does

One `hrsync run`:

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

## Architecture

```
 hris/  mock Workday  :8001 ----+                +---- ethena/  mock Ethena  :8002
   data/hris.db                 |                |       data/ethena.db
                                v                v
                     hrsync/  clients -> engine -> store
                              rules table   CLI      data/sync.db
                                                     (watermark, id_map, audit, review_queue)
                                  |                         |
              mcp_server/  7 tools over stdio      reconcile/  SQL drift report
              (Claude Desktop)                     (attaches all three .db files)
```

## Quickstart

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```
uv sync
uv run hrsync seed      # 100 workers, 95 learners, 12 job changes, in data/
uv run hrsync serve     # leave running: Workday mock on :8001, Ethena mock on :8002
```

In a second terminal:

```
uv run hrsync run                       # changes_seen 12, terminated 1, queued 2
uv run hrsync queue                     # two UNKNOWN_LOCATION items for Vancouver, BC
uv run hrsync resolve 1 --skip --note "location not in rules yet"
uv run hrsync report                    # drift report, also written to data/report.md
```

`resolve <id> --map-learner <learner id>` is the other way to close an item: it saves the mapping and reprocesses that change. `run` exits 1 if any step failed; `resolve` exits 2 if the item is missing or closed, or the learner is unknown.

The seed plants problems on purpose: `Vancouver, BC` is not in the rules table, worker `W-1042`'s email is on two learners, 6 workers are missing from Ethena, 3 learners have the wrong country, and one terminated worker is still active in Ethena until the first run.

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

Five tools are read-only (`get_worker`, `get_learner_status`, `get_audit_trail`, `list_queue`, `explain_gap`); `resolve_queue_item` and `rerun_sync` write and need `confirm=true`. `explain_gap` checks enrollment, the open queue, the rules and the audit log and answers in one sentence, for example why a worker is not in a given campaign.

## Why mocks

Neither Workday nor Ethena offers a public sandbox. The mocks follow the published API shapes (Workday Staffing REST envelopes and paging; the Ethena OpenAPI 1.4.0 spec with HTTP Basic and cursor paging), and the clients in `hrsync/clients.py` are written against those shapes behind two small interfaces. Pointing at a real tenant means changing the base URL and auth. Where the mocks simplify the vendors is listed in [docs/design.md](docs/design.md#what-is-mocked-and-why).

## What's deliberately left out

ADP and payroll, n8n (the queue notifications and review loop would be the n8n piece), a web UI for the queue, a scheduler (the runbook has the cron line), real auth on the mocks, and deployment. [docs/design.md](docs/design.md#deliberately-left-out) says how each would be added.

## Tests

```
uv run pytest    # 54 tests
```

## Docs

- [docs/design.md](docs/design.md): problem, components, data flow, what is mocked, what is out of scope
- [docs/ownership-map.md](docs/ownership-map.md): which HR team, IT and the automation builder own what
- [docs/runbook.md](docs/runbook.md): running, scheduling, every queue reason code, failure modes, rules changes
- [docs/patterns.md](docs/patterns.md): reusable patterns for teams building agents on business systems
- Decision records: [0001 polling](docs/decisions/0001-polling-not-webhooks.md), [0002 attributes, not assignments](docs/decisions/0002-attributes-not-assignments.md), [0003 review queue](docs/decisions/0003-review-queue-not-guessing.md), [0004 idempotency](docs/decisions/0004-idempotency-key.md), [0005 read-only agent](docs/decisions/0005-agent-read-only-by-default.md)
