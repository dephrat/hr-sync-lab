# 0001: Poll the job-change feed instead of waiting for webhooks

**Context.** The sync needs to learn about promotions, transfers, hires and terminations in Workday. Workday can push outbound notifications, but setting them up is tenant configuration owned by HR Technology and IT, and a missed push is lost unless something replays it. The Staffing API can list job changes, and the mock adds a `completedFrom` filter to make that list a change feed.

**Decision.** Each run calls `GET /jobChanges?completedFrom=<watermark>` and pages through the results. The watermark is the latest `completedDate` seen, stored in `sync.db`. The filter is inclusive, so the boundary change is fetched twice and skipped the second time by the audit check (0004).

**Consequences.** Nothing is lost while the sync is down: the next run catches up from the watermark. Latency equals the cron interval (15 minutes in the runbook), which is fine for 30-day training deadlines. The watermark moves only after a clean pass, so a failing change blocks the ones after it until fixed; the runbook covers that case. Webhooks could later trigger a run sooner without changing this logic.
