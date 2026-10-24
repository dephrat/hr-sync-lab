# Demo Web Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A hosted single page with Workday / Ethena / Sync / Report tabs where a non-technical reader can make a job change, run the sync, work the review queue and read the drift report.

**Architecture:** One FastAPI app (`webapp/`) mounts the two existing mock apps, serves one static HTML file, and exposes JSON endpoints that call the existing engine, tools and report code. The engine talks to the mounted mocks in-process via `starlette.testclient.TestClient`. One container behind Traefik on the VPS.

**Tech Stack:** Python 3.12, FastAPI, starlette TestClient as in-process HTTP client, vanilla HTML/JS/CSS (no build), Docker, Traefik labels.

**Spec:** `docs/design-ui.md`

## Global Constraints

- Same repo, `main`, uv; `uv run pytest` must stay green (59 tests now). stdlib sqlite3 only.
- No new Python dependencies beyond what is installed (fastapi, uvicorn, httpx already present; `starlette.testclient` ships with starlette).
- All page fetches use relative URLs (no leading `/`), so the page works at `https://host/<KEY>/` behind Traefik strip-prefix.
- Every write from the page goes through the existing audited paths: `engine.run`, `engine.resolve_with_learner`, the mock's own `POST /jobChanges`. The page never writes to sync.db or ethena.db directly except Reset.
- Ethena tab is read-only. Rules table is not editable from the page.
- Copy rules: plain prose, no em dashes, "mock" in the Workday and Ethena tab titles, "mock systems, fake data" label in the top bar.
- Commit after each task with footer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; push to origin.

## Review Focus

1. Reset while a request is mid-flight: Reset must not corrupt; connections are per request, and Reset deletes and recreates files, so a concurrent request may 500 but the next one works. Test in Task 1: reset twice in a row, then `/api/state` is 200.
2. Resolve with a learner id that exists but belongs to a different person: the engine maps it anyway (that is the human's call); the page must show the rerun outcome, not hide it. Test in Task 2: resolve returns `outcome` and the queue item is closed.
3. A job change posted from the Workday tab with the same profile and location as the worker already has (no-op change): the next run must end `ok`, not fail. Test in Task 2 via the mounted mock + `/api/run`.
4. `explain_gap` for a worker with no learner and no change (one of the six absent workers): must return the "no job change processed" sentence, not 500. Test in Task 2.
5. Report tab after Reset but before any run: sections render with the seeded counts (terminated_but_active 1, overdue 2), no error about missing sync tables. Test in Task 1 (`/api/report` right after `/api/reset`).

---

### Task 1: Web app shell, report sections, runs table, Reset, `hrsync web`

**Files:**
- Create: `webapp/__init__.py`, `webapp/app.py`, `webapp/clients.py`, `webapp/static/index.html` (placeholder: title, four empty tabs)
- Modify: `reconcile/report.py` (add `sections`), `hrsync/store.py` (add `runs` table + `record_run`), `hrsync/engine.py` (call `store.record_run` at the end of `run`), `hrsync/cli.py` (add `web` subcommand), `hrsync/config.py` (add `web_host="127.0.0.1"`, `web_port=8003`)
- Test: `tests/test_webapp.py`, `tests/test_reconcile.py` (sections)

**Interfaces:**
- `reconcile.report.sections(hris_db, ethena_db, sync_db, now) -> list[dict]` each `{"name", "title", "headers": [str], "rows": [[...]]}` in `SECTIONS` order; `build(...)` renders Markdown from `sections(...)` (same output as today).
- `hrsync.store`: table `runs(run_id TEXT PRIMARY KEY, started_at TEXT, summary TEXT)`; `record_run(conn, summary: RunSummary-like dict, started_at: str)`; `last_run(conn) -> dict | None` returning `{"run_id", "started_at", **summary}`.
- `webapp.clients.Bundle` dataclass: `settings, hris_app, ethena_app, hris: WorkdayClient, learning: EthenaClient, rules: Rules`; `make_bundle(settings) -> Bundle` builds `TestClient(hris_app)` and `TestClient(ethena_app)` with `.auth = ("web", settings.ethena_api_key)` and wraps them in the existing clients. Docstring: in-process ASGI client, same as the test suite, one process one port.
- `webapp.app.create_app(settings: Settings) -> FastAPI`: mounts `bundle.hris_app` at `/workday`, `bundle.ethena_app` at `/ethena`; `GET /` serves `static/index.html`; `GET /api/state` → `{"last_run": last_run|None, "watermark": str|None, "open_queue": int, "failed_last_run": int}`; `POST /api/reset` → runs `seeding.seed_all(settings.hris_db, settings.ethena_db)`, deletes `settings.sync_db`, returns `{"ok": true, "workers": 100, "learners": 95, "changes": 12}`; `GET /api/report` → `{"generated_at": iso, "sections": sections(...)}`.
- CLI: `hrsync web [--host H] [--port P]` runs uvicorn on `create_app(settings)`; defaults from `Settings`.
- Store connections are opened per request (`store.connect(settings.sync_db)`) and closed; no module-level sqlite connection.

- [ ] **Step 1: Tests.** `tests/test_reconcile.py::test_sections_shape`: `sections(...)` returns six dicts with the `SECTIONS` names in order and `build(...)` still contains every title. `tests/test_webapp.py` fixture: `Settings` with tmp paths, `seeding.seed_all`, `TestClient(create_app(settings))`. Tests: `test_root_serves_html` (200, `text/html`, contains `Workday (mock)`); `test_mounted_mocks` (`GET /workday/workers?limit=1` 200 with `total`; `GET /ethena/v1/learners?limit=1` 401 without auth, 200 with); `test_state_before_run` (`last_run` None, `open_queue` 0); `test_reset_twice_then_state_ok`; `test_report_after_reset_before_run` (section `terminated_but_active` has 1 row, `overdue_by_department` ≥ 1).
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → PASS (full suite). **Step 5: Smoke**: `uv run hrsync web` and open `http://127.0.0.1:8003/` in a browser. **Step 6: Commit** `feat(web): app shell, mounted mocks, reset, report sections, runs table`.

### Task 2: Sync and lookup endpoints

**Files:**
- Create: `webapp/api.py` (router with the endpoints below; `app.py` includes it)
- Modify: `mcp_server/tools.py` only if a helper must be shared (prefer calling existing functions)
- Test: `tests/test_webapp_api.py`

**Interfaces (all under `/api`):**
- `POST /run` → `{"summary": RunSummary dict, "changes": [ChangeNarrative]}` where `ChangeNarrative = {"change_id", "worker_id", "worker_name", "reason", "outcome", "actions": [{"action", "outcome", "detail"}], "sentence": str}`. `sentence` is built from the audit rows of that run for that change: e.g. `"Priya Nair (W-1038): Promotion. Set isManager=true in Ethena. Verified: Manager Fundamentals, Harassment Prevention (Ontario)."`; queued: `"... Queued for review: UNKNOWN_LOCATION, location 'Vancouver, BC' not in rules."`; skipped: `"... already processed, skipped."`
- `GET /queue?all=0|1` → list of items each with `reason_help: str` (one sentence per reason code, copied from the runbook table's "what happened" column), `needs_rule: bool` (true for UNKNOWN_*), `candidates: [{"id","name","email"}]` (for `AMBIGUOUS_LEARNER` the ids parsed from detail; otherwise `learning.find_learners_by_email(worker email)`).
- `POST /queue/{id}/resolve` body `{"learner_id", "note"}` → `engine.resolve_with_learner(...)` result; 404 `{"error"}` on ValueError; 502 `{"error"}` on `HrSyncClientError`.
- `POST /queue/{id}/skip` body `{"note"}` → sets status `skipped`, resolution `skipped:<note>` (same SQL as the CLI; factor into `store.skip_item(conn, item_id, note, at) -> bool`, used by both).
- `GET /workers?q=` → up to 25 workers `{id, descriptor, primaryWorkEmail, jobProfile, location, department, isManager, workerStatus}` matching id/name/email (case-insensitive), from the mounted HRIS via the client (add `WorkdayClient.list_workers(limit=100, offset=0)` paging helper if missing).
- `GET /workers/{id}` → `{"worker", "learner_id", "learner", "candidates", "enrollments": [{campaignName, completionStatus, deadline}], "audit": [...newest first, limit 50]}` using `mcp_server.tools.get_learner_status` and `get_audit_trail`.
- `GET /campaigns` → the six campaigns `{id, campaignName, courseName, audience_text}` where `audience_text` is words: `isManager true` → "managers", `false` → "non-managers", `country` → the codes, `customCategory` → the values, `{}` → "everyone".
- `POST /explain` body `{"worker_id", "campaign_name"}` → `{"sentence"}` via `tools.explain_gap`.
- Reason help text constants live in `webapp/api.py` as `REASON_HELP: dict[str, str]`.

- [ ] **Step 1: Tests** (`tests/test_webapp_api.py`, same fixture as Task 1): `test_run_returns_narratives` (12 changes, a sentence containing `W-1038` and `Manager Fundamentals`, summary buckets sum to 12); `test_run_again_skips` (`skipped == 1`); `test_queue_has_help_and_candidates` (AMBIGUOUS item has 2 candidates, UNKNOWN_LOCATION has `needs_rule` true); `test_resolve_closes_item_and_reports_outcome`; `test_skip_item`; `test_workers_search` (`q=W-1038` returns 1); `test_worker_detail_has_audit_after_run`; `test_campaigns_audience_text` (Manager Fundamentals → "managers"); `test_explain_absent_worker_no_change` (W-1008 → sentence contains "No job change"); `test_noop_job_change_then_run_ok` (POST `/workday/jobChanges` with the worker's current profile/location, `/api/run` → that change `ok`, failed 0).
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(web): sync, queue, lookup, explain and campaign endpoints`.

### Task 3: The page

**Files:**
- Modify: `webapp/static/index.html` (full page), `webapp/static/app.js`, `webapp/static/style.css` (served by `StaticFiles` at `/static`; the HTML references them relatively as `static/app.js`)
- Test: `tests/test_webapp.py` adds `test_static_assets_served`

**Decisions:**
- Tabs via `location.hash` (`#workday`, `#ethena`, `#sync`, `#report`); default `#sync`. Tab link style: plain text, active underlined, with the tab's accent colour.
- Accents: Workday `--accent: #0B3D91` with secondary `#F38B00`; Ethena `#5B3DF5`; Sync `#1B7F5A`; Report `#555`. Everything else neutral (white, `#f6f6f6`, `#333`), system font, max width 1100px, tables with 1px borders. "mock systems, fake data" label in the top bar in a muted pill.
- Workday tab: search input filtering `/api/workers?q=` (debounced 250ms); table; clicking a row opens a detail card with the Job change form (reason select, profile select from the eight `JOB_PROFILES`, location select from the seven `LOCATIONS`, hard-coded in `app.js` from `hris/db.py`); submit → `POST workday/jobChanges` with `effectiveDate` = today; success toast "Change recorded. Go to Sync and press Run." Recent changes list from `GET workday/jobChanges?limit=20` sorted newest first.
- Ethena tab: `GET ethena/v1/learners?limit=100&cursor=` with header `Authorization: Basic base64("web:test-key")` (the key is the mock's fixed demo key; say so in a code comment); client-side filter; learner detail with `GET ethena/v1/learner-training-campaigns?learnerId=` joined to `/api/campaigns` for names; campaigns list with `audience_text`.
- Sync tab: status strip from `/api/state`; Run button → `/api/run`, renders summary chips and the `sentence` list; rerun note when `skipped == changes_seen`; queue table from `/api/queue` with per-row candidate select (+ free-text input) and Resolve / Skip buttons, "show closed" checkbox; "Look up a person" search → `/api/workers/{id}` renders three cards + audit table; "Why isn't this person in..." form → `/api/explain`.
- Report tab: `/api/report` sections as tables, Refresh button, generated_at.
- Reset button in the top bar → confirm() → `POST /api/reset` → reload current tab.
- Errors: every fetch failure shows a red toast with the server's `error` text; no silent failures.

- [ ] **Step 1:** Write the page. **Step 2:** `uv run hrsync web`, open the browser, walk the six-click story: promote a Toronto Sales_Rep in Workday, check Ethena, Run sync, see the sentence, check Ethena again, resolve the AMBIGUOUS item, Skip a Vancouver item, read the Report, Reset. Fix anything that breaks. **Step 3:** add `test_static_assets_served`. **Step 4:** full suite PASS. **Step 5: Commit** `feat(web): four-tab demo page`.

### Task 4: Container, deploy, docs

**Files:**
- Create: `Dockerfile`, `deploy/docker-compose.yml`, `deploy/README.md`
- Modify: `README.md` (new "Demo page" section with the six-click story; architecture diagram gains `webapp/`), `docs/runbook.md` (web surface, reset, how HR Ops uses the queue page), `docs/ownership-map.md` (page = HR Ops surface), `docs/design.md` (link to `design-ui.md`), `.dockerignore`

**Decisions:**
- `Dockerfile`: `python:3.12-slim`, `COPY pyproject.toml README.md ./` + packages, `pip install --no-cache-dir .`, `ENV HRIS_DB=/app/data/hris.db ETHENA_DB=/app/data/ethena.db SYNC_DB=/app/data/sync.db RULES_PATH=/app/hrsync/rules.default.json`, entrypoint script that runs `hrsync seed` if `/app/data/hris.db` is missing, then `hrsync web --host 0.0.0.0 --port 8003`.
- `deploy/docker-compose.yml` mirrors Logbook's: service `hrsync`, `build: ./app`, `restart: unless-stopped`, volume `./data:/app/data`, Traefik labels with `Host(\`hrsync.ephrat.ai\`) && PathPrefix(\`/${HRSYNC_KEY}\`)`, `stripprefix`, `certresolver letsencrypt`, port 8003, network as the other apps use (check `/docker/logbook/docker-compose.yml` for the network key; copy it).
- Deploy procedure (in `deploy/README.md`): `ssh root@2.25.77.143 "mkdir -p /docker/hrsync/app /docker/hrsync/data"`, `COPYFILE_DISABLE=1 git archive HEAD | ssh root@2.25.77.143 "tar -x -C /docker/hrsync/app"`, copy `deploy/docker-compose.yml` to `/docker/hrsync/docker-compose.yml`, write `/docker/hrsync/.env` with `HRSYNC_KEY=<random 24 hex>` (generated with `openssl rand -hex 12`; never committed or printed in the repo), `docker compose up -d --build`. Dan adds the DNS A record `hrsync.ephrat.ai → 2.25.77.143`.

- [ ] **Step 1:** Dockerfile + compose + entrypoint; `docker build` locally is NOT required (no Docker Desktop; Colima may be stopped), so validate by `uv run hrsync web` and by building on the VPS. **Step 2:** Deploy per the procedure; `curl -I https://hrsync.ephrat.ai/<KEY>/` → 200 once DNS resolves (if DNS is not yet set, verify via `curl -H "Host: hrsync.ephrat.ai" http://2.25.77.143/<KEY>/` and report). **Step 3:** Docs. **Step 4:** full suite PASS. **Step 5: Commit** `feat(deploy): container and Traefik route for the demo page; docs`.
