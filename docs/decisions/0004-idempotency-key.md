# 0004: A terminal audit row marks a change as done

**Context.** Runs can stop halfway (a 429, a timeout) and are rerun by cron. A change must not be applied twice in a harmful way, and a change that failed halfway must not be skipped.

**Decision.** The audit table is append-only with `UNIQUE(run_id, change_id, action)`. A change counts as processed only when a terminal row exists for its id: `verify_enrollment` ok, `terminate_learner` ok, or `queue` queued. Anything else (for example a `create_learner` ok followed by a failed `patch_learner`) is reprocessed from the top on the next run. The steps are written so that repeating them is safe: the id map is checked before creating, the patch contains only differing fields, and terminating twice sets the same status. `hrsync resolve --map-learner` passes `force=True` to reprocess one change that already has a terminal `queue` row.

**Consequences.** No separate "done" table to keep in step with the audit log; the log is the source of truth. Each run gets its own run id, so a rerun adds new rows instead of overwriting. Every new step added to the engine must itself be safe to repeat, or the guarantee breaks.
