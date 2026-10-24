# Demo web page: design

One hosted page where a non-technical reader can cause the problem and watch the sync fix it. It sits beside the CLI (operator surface) and the MCP server (chat surface); it is the HR Operations surface.

## Who it is for

Someone in HR who will not install anything and may not have Claude. They open a link, click for two minutes, and should be able to say what the sync does and how a person works the exceptions. Nothing they click can leave the demo in a state they cannot reset.

## Layout

One page, four tabs across the top, each a plain text link that swaps the panel (the same weight as Production Line Builder's tabs). A top bar with the title, a one-line hint ("Try: promote someone in Workday, check Ethena, then Run sync"), a visible "mock systems, fake data" label, and a **Reset demo** button that reseeds all three databases.

### Workday tab (mock HRIS)
- Worker table with a search box (id, name, job profile, location, department, manager, status).
- Click a worker: record detail plus a **Job change** form: reason (Promotion, Transfer, Termination, Data_Change), new job profile (dropdown of the eight profiles), new location (dropdown of the seven locations). Submit posts to the mock's `POST /jobChanges`, which stores the change as pending without touching the worker.
- **Approvals** card between the Hire form and the search box, styled like a Workday inbox: one line per pending change ("Promotion for Chloe Okafor: profile to Sales Team Lead") with Approve and Deny buttons and the count in the title. It is hidden when nothing is pending. Approve applies the change to the worker and stamps its completed date; Deny deletes it. Hires skip approval.
- Recent job changes list (approved changes only, newest first), so the reader sees what the next sync will pick up.

### Ethena tab (mock learning platform)
- Learner table with search (id, name, email, status, country, state, manager, category).
- Click a learner: attributes plus enrollments (campaign, status, deadline). Each unfinished enrollment has a **Take course** button that opens a small inline course on a lavender panel: a short lesson, one multiple-choice question, and a Complete course button that unlocks only on the right answer. Completing calls the mock's complete endpoint. One course is open at a time.
- **Campaigns** sub-list: the six campaigns with their audience rules in words ("managers", "Canada + Ontario"), so the reader sees why a promotion produces those courses.

### Sync tab
- Status strip: last run time, watermark, open queue count, failed steps in the last run.
- **Run sync** button: runs `engine.run`, then shows the summary counts and a per-change list in English built from that run's audit rows: who, what change, what was written, which campaigns were verified, or why it was queued. A second run shows "1 seen, 1 skipped" with a note that this is the safe-to-rerun proof.
- **Review queue** table: id, reason, person, detail, age, with a one-line explanation per reason code. Row actions: **Map to learner** (dropdown of candidates: for `AMBIGUOUS_LEARNER` the ids in the detail; otherwise learners matching the worker's email, and a free-text fallback) with Resolve, and **Skip** with a note. For `UNKNOWN_LOCATION` / `UNKNOWN_PROFILE` the row says the rules table needs the value and names the owner. A "show closed" toggle.
- **Look up a person**: search workers; shows Workday record, mapped learner (or "no learner" / "two candidates"), enrollments, and the audit trail newest first.
- **Why isn't this person in ...?**: worker + campaign dropdown, prints the `explain_gap` sentence.

### Report tab
The six reconciliation sections as tables, with a Refresh button and the generation time.

## How it is built

- `webapp/app.py`: one FastAPI app. Mounts the existing mock apps at `/workday` and `/ethena` (they already are FastAPI apps). Serves `webapp/static/index.html` at `/`. JSON endpoints under `/api/`. The sync engine inside the web app talks to the two mounted mocks in-process through `starlette.testclient.TestClient` instances (an `httpx.Client` over ASGI, the same thing the test suite uses), so one process and one port serve everything.
- `webapp/static/index.html`: one file, vanilla JS, no build step. All fetches use relative URLs so the page works behind a stripped path prefix.
- Colours: each tab has an accent (Workday navy/orange, Ethena purple, Sync neutral green, Report grey) on the tab underline, header stripe and primary button only. "mock" appears in the Workday and Ethena tab titles.
- New CLI subcommand `hrsync web [--host 127.0.0.1] [--port 8003]`.
- `reconcile.report` gains `sections(...)` returning structured rows; `build(...)` renders Markdown from it.
- `hrsync.store` gains a `runs` table recording each run's summary so the status strip and the per-change narrative can be built after the fact.

## Hosting

One container on the VPS (`/docker/hrsync`), Traefik route `Host(hrsync.ephrat.ai) && PathPrefix(/<HRSYNC_KEY>)` with strip-prefix, the same pattern as Logbook. `data/` is a volume. Reset demo reseeds inside the container. No auth beyond the path key.

## Out of scope

Editing Ethena's learner attributes from the page (hiring in Workday and marking training complete in Ethena are in); editing the rules table from the page; a Claude "Ask" box (possible second step); mobile layout polish; accounts.
