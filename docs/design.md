# Design

The build plan this was implemented from is [docs/superpowers/plans/2026-10-23-hr-sync-lab.md](superpowers/plans/2026-10-23-hr-sync-lab.md).

## The problem

HR teams run on several platforms, and the process that spans two of them often has no owner. Take Priya, a sales rep in Toronto. On Monday she is promoted to team lead in Workday. Company policy says every new manager completes "Manager Fundamentals" and the Ontario harassment-prevention course within 30 days. Those courses live in Ethena. Workday does not know about courses and Ethena does not know about the promotion. Today someone in Enablement notices and assigns the courses by hand, or nobody notices.

This project closes that gap with a sync job that reads job changes from the HRIS and updates the learning platform. It has three properties that matter more than speed: it is safe to rerun, every action it takes is written to an audit log, and when it is not sure what to do it puts the case in a review queue for a person instead of guessing.

## Components

| Package | What it is |
|---|---|
| `hris/` | Mock Workday Staffing REST API (FastAPI on port 8001, SQLite `data/hris.db`). |
| `ethena/` | Mock Ethena API under `/v1` (FastAPI on port 8002, SQLite `data/ethena.db`). Enrolls a learner in every active campaign whose audience matches the learner's attributes. |
| `hrsync/` | The sync: HTTP clients (`clients.py`), rules table (`rules.py`, `rules.default.json`), state store (`store.py`, SQLite `data/sync.db`), engine (`engine.py`), CLI (`cli.py`), settings from env (`config.py`). |
| `reconcile/` | Drift report: named SQL queries in `queries.sql`, run with all three databases attached to one SQLite connection. |
| `mcp_server/` | MCP server over stdio exposing seven tools to an assistant such as Claude Desktop. |

## Data flow

```
 Workday (hris/, :8001)                         Ethena (ethena/, :8002)
 GET /jobChanges?completedFrom=W                GET   /v1/learners?email=...
 GET /workers/{id}                              POST  /v1/learners
        |                                       PATCH /v1/learners/{id}
        |                                       GET   /v1/learner-training-campaigns
        v                                                ^
   WorkdayClient --> engine.process_change --> EthenaClient
                          |      ^
                          v      |
                    data/sync.db (store.py)
                    watermark | id_map | audit | review_queue
                          |
          +---------------+----------------+
          v                                v
   hrsync CLI / mcp_server          reconcile/ (attaches hris.db,
   (queue, resolve, rerun)          ethena.db, sync.db; markdown report)
```

One run of `engine.run`:

1. Read the watermark from `sync.db` (default `1970-01-01T00:00:00Z`) and fetch `GET /jobChanges?completedFrom=<watermark>`, paging with `limit`/`offset`. The filter is inclusive, so the last change of the previous run comes back again and is skipped by step 2.
2. For each change, skip it if `audit` already holds a terminal row for that change id: `verify_enrollment` ok, `terminate_learner` ok, or `queue` queued. Otherwise process it from the top.
3. Load the worker and resolve the Ethena learner: first `id_map`, then search by work email. One match is stored in `id_map`. Two or more matches queue `AMBIGUOUS_LEARNER`. No match creates the learner, or queues `NO_LEARNER_MATCH` when run with `--no-create` or when the change is a termination.
4. For a Termination, `PATCH` the learner to `status: TERMINATED` and stop.
5. Otherwise map job profile and location through the rules table to learner attributes (`isManager`, `country`, `state`, `customCategory`, `department`). An unknown profile or location queues `UNKNOWN_PROFILE` or `UNKNOWN_LOCATION`. Compare with the learner's current attributes and `PATCH` only the fields that differ.
6. Ask Ethena which campaigns the learner is now in and check that every campaign the rules expect is there. Any missing campaign queues `ENROLLMENT_MISSING`; otherwise write the terminal `verify_enrollment` ok row.

If any HTTP call fails (a 429, a 4xx, a 5xx, a connection error), the engine writes a `failed` audit row for that step, stops the run, and leaves the watermark where it was. The watermark moves to the latest `completedDate` only after every change in the batch was handled.

For Priya, step 5 turns `Sales_Team_Lead` + `Toronto, ON` into `isManager: true, country: CAN, customCategory: ON`. Ethena's campaign audiences then enroll her in Manager Fundamentals and Harassment Prevention (Ontario), and step 6 confirms that they did.

## Why Ethena gets attributes, not assignments

Ethena's API has no "assign this course to this person" call. Enrollment follows campaign audiences, which are rules over learner attributes. So the sync writes attributes and then checks that the expected campaigns appeared. See [decision 0002](decisions/0002-attributes-not-assignments.md).

## What is mocked and why

Neither Workday nor Ethena offers a public sandbox, so both systems are mocks. Each mock follows the vendor's published API shape so the clients in `hrsync/clients.py` are written against the real contract:

- Workday: the Staffing REST shapes. Lists come back as `{total, data}` and page with `limit`/`offset`. Objects carry `id`, `descriptor` and `href`, and references are nested `{id, descriptor}` objects. Two simplifications: the mock's `POST /jobChanges` applies a change immediately instead of Workday's initiate/submit/approve flow, and `GET /jobChanges` accepts a `completedFrom` filter so it can act as a change feed.
- Ethena: the OpenAPI 1.4.0 spec. `/v1/learners`, `/v1/training-campaigns`, `/v1/learner-training-campaigns`, `/v1/learner-training-modules`; HTTP Basic auth; lists come back as `{limit, hasMore, data}` and the next page is requested with `cursor` set to the last id. Learners carry `isManager`, `country`, `state`, `department` and `customCategory`.

The engine depends on two small Protocols (`HrisClient`, `LearningClient`), not on the mocks. Pointing at a real tenant means changing the base URL and the auth in `hrsync/cli.py:_build_clients`.

The seed (`hrsync seed`) is deterministic: 100 workers, 95 learners, 12 completed job changes over the last 29 days. It plants mismatches on purpose: `Vancouver, BC` is missing from the rules table (two transfers there land in the queue as `UNKNOWN_LOCATION` on the first run), worker `W-1042`'s email is on two learners (`AMBIGUOUS_LEARNER` as soon as a change for that worker arrives), 6 workers are absent from Ethena, 3 learners have the wrong country, and one worker terminated in the HRIS is still ACTIVE in Ethena until the sync processes the termination.

## Deliberately left out

- **ADP / payroll.** Another client behind its own Protocol, feeding the same engine or a second engine with its own rules and watermark.
- **n8n.** Notifications and the human-review loop are the natural n8n piece: a workflow that polls `review_queue` for new open items, posts them to the HR Ops channel, and calls `hrsync resolve` with the reviewer's answer.
- **Web UI for the queue.** A small page over `review_queue` that calls the same `engine.resolve_with_learner` the CLI and MCP tool use.
- **Scheduler.** A cron line runs `hrsync run`; see the [runbook](runbook.md).
- **Auth on the mocks.** The HRIS mock has none and the Ethena mock checks one fixed Basic password. A real deployment uses Workday OAuth client credentials and an Ethena API key, both issued and rotated by IT.
- **Deployment.** The sync is a CLI plus three SQLite files; it would run as a scheduled container with `sync.db` on a persistent volume, or with the store moved to Postgres.
