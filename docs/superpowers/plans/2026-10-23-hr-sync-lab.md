# hr-sync-lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close one HR process no system owns — a Workday job change must result in the right Ethena compliance training — with a sync that is safe to rerun, logs every action, and queues what it cannot resolve for a human.

**Architecture:** Two mock services shaped after the public Workday Staffing REST API and the Ethena OpenAPI spec (FastAPI + SQLite). A plain-Python sync engine reads completed job changes from the HRIS, maps the worker to an Ethena learner, translates job profile/location into Ethena learner attributes via a rules table, PATCHes the learner, verifies the expected campaign enrollments appeared, and records audit rows; unresolvable cases go to a review queue. A SQL reconciliation report and a read-mostly MCP server sit on top.

**Tech Stack:** Python 3.12, uv, FastAPI, uvicorn, httpx, sqlite3 (stdlib), `mcp>=2,<3` (class `MCPServer`), pytest, typer-free argparse CLI.

**Spec:** The design conversation of 2026-10-23 (summarised in `docs/design.md`, written in Task 8). Key points are copied into Global Constraints below so the plan stands alone.

## Global Constraints

- Python 3.12 via uv; `uv run pytest` must pass from a clean clone.
- Layout: top-level packages `hris/`, `ethena/`, `hrsync/`, `reconcile/`, `mcp_server/`, tests in `tests/`.
- All SQLite access through stdlib `sqlite3`; no ORM. Each mock owns one DB file; the engine owns `sync.db`.
- Mock API shapes follow the researched contracts:
  - Workday: list envelopes `{"total": int, "data": [...]}`, query `limit`/`offset`; objects carry `id`, `descriptor`, `href`; nested refs are `{id, descriptor}`.
  - Ethena: list envelopes `{"limit": int, "hasMore": bool, "data": [...]}`, query `limit`/`cursor` (cursor = last id); HTTP Basic auth; error bodies `{"title": ..., "detail": ...}`.
- Ethena has no "assign course" call. Enrollment is a consequence of learner attributes (`isManager`, `country`, `state`, `department`, `customCategory`). The sync writes attributes and verifies enrollments.
- Every write the engine performs is idempotent: a second `run` with no new changes performs zero writes.
- Every engine action, including skips and failures, produces one `audit` row.
- No PII in any prompt or MCP tool description. Fake seed data only; names generated from fixed lists, emails `@example.com`.
- Commit after every task with the attribution footer the session requires; push to `github.com/dephrat/hr-sync-lab` (public) after Task 1 creates the remote.

## Review Focus

1. The same job change appears in two consecutive `run`s (watermark boundary): the second run must skip it with an audit row `outcome=skipped`, not re-PATCH. Test in Task 4.
2. Two Ethena learners share one work email: the engine must queue `AMBIGUOUS_LEARNER`, never pick the first. Test in Task 4.
3. A worker terminated in Workday but ACTIVE in Ethena: `run` must PATCH status `TERMINATED` and audit it; reconciliation must list it until then. Tests in Tasks 4 and 6.
4. Ethena returns 429 mid-batch: the engine must stop the batch without advancing the watermark; already-written audit rows stay. Test in Task 4.
5. A `resolve` with a learner id that does not exist in Ethena: CLI must refuse with a non-zero exit and leave the queue item open. Test in Task 5.

---

### Task 1: Repo scaffold and mock Workday (`hris/`)

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `README.md` (stub), `hris/__init__.py`, `hris/db.py`, `hris/app.py`, `hris/seed.py`
- Test: `tests/test_hris.py`

**Interfaces:**
- Produces: `hris.db.connect(path) -> sqlite3.Connection` (creates schema); `hris.app.create_app(db_path) -> FastAPI`; `hris.seed.seed(conn, n_workers=100, rng_seed=7) -> None`.
- HTTP:
  - `GET /workers?limit&offset` → `{"total", "data": [Worker]}`
  - `GET /workers/{id}` → `Worker` or 404
  - `GET /jobChanges?completedFrom=<iso>&limit&offset` → `{"total", "data": [JobChange]}` ordered by `completedDate, id`
  - `GET /jobChanges/{id}` → `JobChange`
  - `POST /jobChanges` body `{workerId, effectiveDate, reason, jobProfile?, location?, businessTitle?, supervisoryOrganization?}` → 201 `JobChange`; applies the change to the worker immediately and sets `completedDate` = now (mock simplification of Workday's initiate/submit flow; say so in a docstring).
- `Worker`: `id` (`W-1001`…), `descriptor` (full name), `href`, `primaryWorkEmail`, `businessTitle`, `isManager`, `hireDate`, `jobProfile{id,descriptor}`, `location{id,descriptor}`, `primarySupervisoryOrganization{id,descriptor}`, `primaryPosition{id,descriptor}`, `workerStatus{active,terminated,statusDate}`.
- `JobChange`: `id` (`JC-…`), `descriptor`, `href`, `worker{id,descriptor}`, `effectiveDate`, `completedDate`, `reason{id,descriptor}` (reasons: `Promotion`, `Transfer`, `Hire`, `Termination`, `Data_Change`), `jobProfile{id,descriptor}`, `location{id,descriptor}`, `businessTitle`, `supervisoryOrganization{id,descriptor}`. Nullable fields omitted when unchanged.

**Seed data decisions (fixed, so tests can assert on them):**
- Job profiles: `Sales_Rep`, `Sales_Team_Lead`, `Software_Engineer`, `Engineering_Manager`, `HR_Generalist`, `Finance_Analyst`, `Warehouse_Associate`, `Warehouse_Supervisor`.
- Locations (Workday descriptors): `Toronto, ON`, `Vancouver, BC`, `Minneapolis, MN`, `New York, NY`, `Austin, TX`, `Remote - Canada`, `Remote - US`.
- Departments: `Sales`, `Engineering`, `People`, `Finance`, `Operations`.
- Managers: `isManager` true for `*_Lead`, `*_Manager`, `*_Supervisor` profiles.
- Seeded job changes: 12 completed changes spread over the last 30 days, including at least one `Promotion` to a manager profile, one `Transfer` to `Vancouver, BC`, one `Hire`, one `Termination`.

- [ ] **Step 1: Scaffold** `pyproject.toml` (project `hr-sync-lab`, `requires-python = ">=3.12"`, deps `fastapi`, `uvicorn`, `httpx`, `mcp>=2,<3`; dev deps `pytest`; `[project.scripts] hrsync = "hrsync.cli:main"`), `.gitignore` (`.venv`, `*.db`, `__pycache__`, `.pytest_cache`). Run `uv sync`.
- [ ] **Step 2: Write failing tests** in `tests/test_hris.py` using `fastapi.testclient.TestClient(create_app(tmp_path/"hris.db"))` after `seed`:
  - `test_list_workers_envelope`: `total == 100`, `len(data) == 25` with default limit, `data[0]` has keys `id, descriptor, href, primaryWorkEmail, businessTitle, isManager, jobProfile, location, workerStatus`.
  - `test_get_worker_404`.
  - `test_job_changes_since_filters_and_orders`: `completedFrom` 10 days ago returns fewer than 12 and is sorted by `completedDate`.
  - `test_post_job_change_applies_to_worker`: POST a `Promotion` to `Sales_Team_Lead`; GET worker shows new `jobProfile` and `isManager == True`; change has a `completedDate`.
- [ ] **Step 3: Run** `uv run pytest tests/test_hris.py -v` → fails on import.
- [ ] **Step 4: Implement** `hris/db.py` (tables `workers`, `job_changes`), `hris/seed.py` (deterministic `random.Random(rng_seed)`, names from two fixed lists), `hris/app.py`.
- [ ] **Step 5: Run** tests → PASS.
- [ ] **Step 6: Commit** `feat(hris): mock Workday staffing API with seed data`; `gh repo create dephrat/hr-sync-lab --public --source=. --push`.

### Task 2: Mock Ethena (`ethena/`)

**Files:**
- Create: `ethena/__init__.py`, `ethena/db.py`, `ethena/app.py`, `ethena/seed.py`
- Test: `tests/test_ethena.py`

**Interfaces:**
- Produces: `ethena.db.connect(path)`, `ethena.app.create_app(db_path, api_key="test-key") -> FastAPI`, `ethena.seed.seed(conn, hris_conn, rng_seed=7)` (seeds learners FROM the HRIS workers so the two systems overlap realistically).
- HTTP (all under `/v1`, HTTP Basic `username:api_key`, 401 otherwise):
  - `GET /learners?email&status&limit&cursor`, `POST /learners` → 200 `{"id"}`, `GET/PATCH/DELETE /learners/{id}` (PATCH → 204)
  - `GET /training-campaigns?status&limit&cursor`, `GET /training-campaigns/{id}`
  - `GET /learner-training-campaigns?learnerId&trainingCampaignId&completionStatus&limit&cursor`, `GET /learner-training-campaigns/{id}`
  - `GET /learner-training-modules?learnerId&limit&cursor` (one module per campaign, status mirrors completion)
- Learner fields exactly as the Ethena spec: `id` (alphanumeric, `L` + 6 digits), `name`, `email`, `status` (`ACTIVE|DEACTIVATED|TERMINATED`), `country` (ISO-3), `state` (2-letter, USA only), `isManager`, `managerLearnerId`, `department`, `workerType`, `customCategory`, `language`, `trainingUrl`.
- Campaign fields: `id`, `courseName`, `campaignName`, `status`, `initialTrainingAt`, `nextTrainingAt`, `deadlineInDays`. Plus a mock-only `audience` column (JSON: `{"isManager": bool?, "country": [..]?, "state": [..]?, "customCategory": [..]?}`), NOT exposed in the API response.
- Enrollment rule (mock internal, documented in `ethena/app.py` docstring): after `POST`/`PATCH /learners`, for each ACTIVE campaign whose audience matches the learner, ensure a learner-training-campaign exists (`enrollmentStatus=ACTIVE`, `completionStatus=WITHIN_DEADLINE`, `deadline = now + deadlineInDays`). Campaigns that no longer match are left in place (Ethena does not auto-unenroll). TERMINATED learners get no new enrollments.
- Seeded campaigns (fixed): `Harassment Prevention (Ontario)` audience country CAN + state-less + customCategory `ON`; `Harassment Prevention (US Managers)` USA + isManager; `Harassment Prevention (US Employees)` USA + not manager; `Manager Fundamentals` isManager; `Data Privacy Essentials` everyone; `Workplace Safety (Warehouse)` customCategory `WAREHOUSE`. `deadlineInDays` 30 for all.
- Seed mismatches (fixed): 6 HRIS workers absent from Ethena; 2 learners share the email of worker `W-1042`; 3 learners whose `country` disagrees with their HRIS location; one `Termination` in HRIS whose learner is still `ACTIVE`.

- [ ] **Step 1: Write failing tests** `tests/test_ethena.py`:
  - `test_requires_basic_auth` (401 without header, 200 with).
  - `test_learners_cursor_pagination` walks all pages with `cursor`, total count equals seeded count.
  - `test_patch_learner_triggers_enrollment`: PATCH a non-manager to `isManager=true`, `country=USA`, `state=MN` → `GET /learner-training-campaigns?learnerId=` now includes `Manager Fundamentals` and `Harassment Prevention (US Managers)`.
  - `test_patch_is_idempotent`: same PATCH twice → same number of enrollments.
  - `test_terminated_learner_not_enrolled`.
- [ ] **Step 2: Run** → fail on import.
- [ ] **Step 3: Implement** db, seed (reads `hris.db` to copy workers → learners with `country`/`state` derived from location), app with a `_enroll(conn, learner_id)` helper.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(ethena): mock Ethena learners/campaigns API with attribute-driven enrollment`.

### Task 3: API clients and rules (`hrsync/clients.py`, `hrsync/rules.py`)

**Files:**
- Create: `hrsync/__init__.py`, `hrsync/clients.py`, `hrsync/rules.py`, `hrsync/rules.default.json`
- Test: `tests/test_clients.py`, `tests/test_rules.py`

**Interfaces:**
- `class HrisClient(Protocol)`: `get_worker(worker_id) -> dict`, `job_changes_since(completed_from: str) -> list[dict]` (pages through all).
- `class LearningClient(Protocol)`: `find_learners_by_email(email) -> list[dict]`, `get_learner(id) -> dict`, `create_learner(body) -> str`, `patch_learner(id, body) -> None`, `campaigns_for_learner(id) -> list[dict]` (joined with campaign `courseName`), `list_campaigns() -> list[dict]`.
- `WorkdayClient(http: httpx.Client)` and `EthenaClient(http: httpx.Client)` implement them. Constructors take an `httpx.Client` so tests pass `TestClient(app)` and production passes `httpx.Client(base_url=..., auth=...)`. Raise `RateLimited` (subclass of `Exception`, carries `retry_after`) on 429, `ClientError(status, title)` on other 4xx.
- `rules.load(path) -> Rules`; `Rules.attributes_for(job_profile_id: str, location_descriptor: str, department: str) -> LearnerAttrs | None` where `LearnerAttrs = dict` with keys `isManager, country, state, customCategory, department`; returns `None` when `job_profile_id` or `location` is unknown. `Rules.expected_courses(attrs) -> set[str]` of `courseName`s.
- `rules.default.json`: `locations` map (Workday descriptor → `{country, state?, customCategory?}`) deliberately MISSING `Vancouver, BC`; `profiles` map (profile id → `{isManager, customCategory?}`) with `Warehouse_*` → `WAREHOUSE`; `courses` list of `{courseName, when: {isManager?, country?, customCategory?}}` mirroring the seeded campaigns.

- [ ] **Step 1: Tests**: clients against the two mocks (`job_changes_since` pages past 25; `find_learners_by_email` returns 2 for the duplicated email; 429 raises `RateLimited` — monkeypatch the mock to return 429 once). Rules: `attributes_for("Sales_Team_Lead", "Toronto, ON", "Sales")` → `{isManager: True, country: "CAN", state: None, customCategory: "ON", department: "Sales"}`; `attributes_for(..., "Vancouver, BC", ...)` → `None`; `expected_courses` for that attrs → `{"Harassment Prevention (Ontario)", "Manager Fundamentals", "Data Privacy Essentials"}`.
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(hrsync): Workday/Ethena clients and rules table`.

### Task 4: Store and engine (`hrsync/store.py`, `hrsync/engine.py`)

**Files:**
- Create: `hrsync/store.py`, `hrsync/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- `store.connect(path) -> sqlite3.Connection` with tables:
  - `watermark(key TEXT PRIMARY KEY, value TEXT)`; key `job_changes.completed_from`.
  - `id_map(worker_id TEXT PRIMARY KEY, learner_id TEXT NOT NULL, source TEXT, created_at TEXT)`; source ∈ `email`, `manual`, `created`.
  - `audit(id INTEGER PK, run_id TEXT, change_id TEXT, worker_id TEXT, learner_id TEXT, action TEXT, outcome TEXT, detail TEXT, created_at TEXT, UNIQUE(change_id, action))`; action ∈ `map_learner, create_learner, patch_learner, verify_enrollment, terminate_learner, skip, queue`; outcome ∈ `ok, skipped, queued, failed`.
  - `review_queue(id INTEGER PK, change_id TEXT, worker_id TEXT, reason TEXT, detail TEXT, status TEXT, created_at TEXT, resolved_at TEXT, resolution TEXT)`; reason ∈ `NO_LEARNER_MATCH` (not used when create is allowed; kept for `--no-create`), `AMBIGUOUS_LEARNER`, `UNKNOWN_LOCATION`, `UNKNOWN_PROFILE`, `ENROLLMENT_MISSING`; status ∈ `open, resolved, skipped`.
- `engine.run(hris: HrisClient, learning: LearningClient, rules: Rules, store: sqlite3.Connection, now: datetime | None = None, create_missing: bool = True) -> RunSummary` where `RunSummary` is a dataclass `run_id, changes_seen, patched, created, terminated, queued, skipped, failed`.
- Algorithm (the part the signature does not determine):
  1. `completed_from = watermark or "1970-01-01T00:00:00Z"`; fetch changes; skip any whose `change_id` already has an audit row with action `patch_learner`/`terminate_learner`/`queue`/`skip` (audit `skip`, outcome `skipped`, detail `already processed`).
  2. Per change, in order: resolve learner — `id_map` hit, else email lookup: 0 → create (if `create_missing`) else queue `NO_LEARNER_MATCH`; 1 → map with source `email`; ≥2 → queue `AMBIGUOUS_LEARNER`.
  3. If reason is `Termination`: PATCH status `TERMINATED`, audit `terminate_learner`, continue.
  4. `rules.attributes_for(...)`: `None` → queue `UNKNOWN_LOCATION` or `UNKNOWN_PROFILE` (whichever is missing; detail names the value).
  5. Compare attrs with current learner; if different PATCH and audit `patch_learner` with detail = JSON diff; if same audit `patch_learner`/`skipped`.
  6. `verify_enrollment`: expected courses ⊆ `campaigns_for_learner` course names → `ok`; else queue `ENROLLMENT_MISSING` with the missing names.
  7. Commit per change. On `RateLimited` or any exception from a client: audit `failed`, stop the loop, do NOT advance the watermark, return summary.
  8. After the loop completes without stopping, set watermark = max `completedDate` seen.

- [ ] **Step 1: Tests** (`tests/test_engine.py`, fixtures build both mocks in `tmp_path`, seed, wrap in TestClients, fresh `sync.db`):
  - `test_first_run_processes_seed_changes`: `changes_seen == 12`, `failed == 0`, each change has ≥1 audit row.
  - `test_second_run_is_noop`: second `run` → `patched == created == terminated == 0`.
  - `test_promotion_sets_manager_and_enrolls`: after run, the promoted worker's learner has `isManager` true and `Manager Fundamentals` enrolled; audit `verify_enrollment` ok.
  - `test_vancouver_transfer_is_queued` with reason `UNKNOWN_LOCATION` and detail containing `Vancouver, BC`.
  - `test_duplicate_email_is_queued` → `AMBIGUOUS_LEARNER` for `W-1042`.
  - `test_termination_terminates_learner`.
  - `test_missing_learner_is_created` and `test_missing_learner_queued_when_create_disabled`.
  - `test_rate_limit_stops_without_advancing_watermark`: monkeypatch `learning.patch_learner` to raise `RateLimited` on the 3rd call; watermark unchanged; `failed == 1`; rerun after removing the patch finishes the rest and the earlier changes are `skipped` not re-patched.
  - `test_watermark_boundary_does_not_reprocess`: POST a new change with `completedDate` equal to the watermark; run → processed once.
- [ ] **Step 2: Run** → fail. **Step 3: Implement** store then engine. **Step 4: Run** → PASS. **Step 5: Commit** `feat(hrsync): idempotent sync engine with audit log and review queue`.

### Task 5: CLI (`hrsync/cli.py`)

**Files:**
- Create: `hrsync/cli.py`, `hrsync/config.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- `config.Settings` from env with defaults: `HRIS_URL=http://127.0.0.1:8001`, `ETHENA_URL=http://127.0.0.1:8002`, `ETHENA_API_KEY=test-key`, `SYNC_DB=./data/sync.db`, `HRIS_DB=./data/hris.db`, `ETHENA_DB=./data/ethena.db`, `RULES_PATH=hrsync/rules.default.json`.
- `main(argv=None) -> int`. Subcommands:
  - `seed` — create both mock DBs with seed data (idempotent: deletes and recreates).
  - `serve` — run both mocks with uvicorn on 8001/8002 (threads), blocks until Ctrl-C.
  - `run [--no-create] [--json]` — prints the `RunSummary` as a table or JSON.
  - `queue [--all]` — open items as a table: id, reason, worker, detail, age.
  - `resolve <id> (--map-learner <learnerId> | --skip) [--note TEXT]` — `--map-learner` verifies the learner exists via the client, writes `id_map` with source `manual`, marks resolved, and reruns that single change; unknown learner → exit 2, item stays open.
  - `report` — delegates to Task 6.
- For tests, `cli.main` accepts an injected `clients` factory via `hrsync.cli._build_clients` monkeypatch so tests use TestClients, not sockets.

- [ ] **Step 1: Tests**: `run` prints `changes_seen` and exits 0; `queue` lists the Vancouver item; `resolve <id> --map-learner L999999` exits 2 and item remains open; `resolve` of the `AMBIGUOUS_LEARNER` item with the correct learner id resolves it and the worker's learner is patched on the embedded rerun.
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Smoke** by hand: `uv run hrsync seed && uv run hrsync serve &` then `uv run hrsync run` against real sockets. **Step 6: Commit** `feat(cli): hrsync seed/serve/run/queue/resolve`.

### Task 6: Reconciliation report (`reconcile/`)

**Files:**
- Create: `reconcile/__init__.py`, `reconcile/queries.sql`, `reconcile/report.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- `report.build(hris_db, ethena_db, sync_db, now) -> str` (Markdown). ATTACHes the three DBs to one connection and runs the named queries in `queries.sql` (sections split by `-- name: <section>` comments):
  - `overdue_by_department`: learner-training-campaigns past `deadline` not COMPLETED, grouped by HRIS department.
  - `in_hris_not_ethena`: active workers with no learner by email and no `id_map`.
  - `in_ethena_not_hris`: ACTIVE learners whose email matches no active worker.
  - `terminated_but_active`: terminated workers mapped to ACTIVE learners.
  - `stale_enrollments`: enrollments whose campaign no longer matches the learner's current attributes (uses the mock `audience` column; documented as "requires campaign audience export in a real tenant").
  - `queue_age`: open review items with age in days.
- `hrsync report [--out PATH]` writes the Markdown (default `data/report.md`) and prints it.

- [ ] **Step 1: Tests** on seeded DBs before any sync run: `terminated_but_active` has 1 row; `in_hris_not_ethena` has 6; after `engine.run`, `in_hris_not_ethena` is 0 and `terminated_but_active` is 0; report string contains all six section headers.
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(reconcile): cross-system drift report`.

### Task 7: MCP server (`mcp_server/`)

**Files:**
- Create: `mcp_server/__init__.py`, `mcp_server/server.py`, `mcp_server/tools.py`
- Test: `tests/test_mcp.py`

**Interfaces:**
- `tools.py` pure functions taking `(hris, learning, store)`: `get_worker(worker_id)`, `get_learner_status(worker_id)` (learner + campaigns + completion), `get_audit_trail(worker_id, limit=20)`, `list_queue()`, `explain_gap(worker_id, course_name) -> str` (walks audit + queue and returns a plain-language sentence: e.g. `"Job change JC-0007 on 2026-10-20 is in the review queue (UNKNOWN_LOCATION: 'Vancouver, BC'); no attributes were written, so Ethena never enrolled this learner."`), `resolve_queue_item(item_id, learner_id, confirm: bool)` and `rerun_sync(confirm: bool)` which return an error dict unless `confirm is True`.
- `server.py`: `MCPServer("hr-sync-lab")`, registers the seven tools with docstrings that state cost and side effects; stdio transport; reads `Settings` from env. Follow `catalog-mcp/mcp_server.py` structure (thin wrapper, absolute DB paths pinned at import).

- [ ] **Step 1: Tests**: unit tests on `tools.py` (explain_gap for the Vancouver worker contains `UNKNOWN_LOCATION`; `resolve_queue_item(..., confirm=False)` returns `{"error": "confirm required"}` and changes nothing) and one stdio smoke test that launches `server.py` via `mcp` client session, calls `list_tools`, and calls `list_queue`.
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(mcp): read tools plus gated resolve/rerun`. **Step 6:** add a `claude_desktop_config.json` snippet to README.

### Task 8: Docs, README, demo GIF

**Files:**
- Create: `docs/design.md`, `docs/ownership-map.md`, `docs/runbook.md`, `docs/decisions/0001-polling-not-webhooks.md`, `0002-attributes-not-assignments.md`, `0003-review-queue-not-guessing.md`, `0004-idempotency-key.md`, `0005-agent-read-only-by-default.md`, `docs/patterns.md`, `README.md` (full), `demo/demo.tape` (vhs)
- Modify: none

- [ ] **Step 1:** `docs/design.md`: the problem (Priya example), components, data flow diagram (ASCII), what is mocked and why (no public Workday/Ethena sandboxes; shapes taken from Workday Staffing REST connector docs and the Ethena OpenAPI 1.4.0 spec), what is deliberately out (ADP, n8n, UI, scheduler, auth, deploy) with one sentence each on how it would be added.
- [ ] **Step 2:** `docs/ownership-map.md`: table — system / owning HR team / what IT owns / what this role owns, mirroring the SPS posting's split (IT: security, integration infrastructure, system health; this role: automations and agents on top).
- [ ] **Step 3:** `docs/runbook.md`: run, schedule (cron line), every queue reason with the HR Ops action, failure modes (429, mock down, watermark stuck), credential rotation (IT), how to add a location or profile to the rules table.
- [ ] **Step 4:** decision records, each ≤ 20 lines: context, decision, consequences.
- [ ] **Step 5:** `docs/patterns.md` for the AI Engineering team: idempotent writer, audit row shape, human-in-the-loop gate (`confirm=True`), no PII in prompts, client-behind-interface for sandbox-less vendors.
- [ ] **Step 6:** README: one-paragraph problem, GIF, quickstart (`uv sync`, `uv run hrsync seed`, `serve`, `run`, `queue`, `report`), MCP setup, test count, links to docs.
- [ ] **Step 7:** `demo/demo.tape` (vhs, like catalog-mcp): seed → run → queue → resolve → run → report. Render `demo/demo.gif`.
- [ ] **Step 8: Commit** `docs: design, ownership map, runbook, decisions, patterns, README`; push.
