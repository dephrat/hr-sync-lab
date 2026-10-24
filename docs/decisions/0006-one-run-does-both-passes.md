# 0006: One run does both passes

**Context.** The incremental sync only looks at Workday job changes since the watermark. Drift with no Workday event behind it, such as a learner hand-edited in Ethena or a mismatch that was already there, is never examined, so it stays until someone notices. The drift report already compares every person; the engine could not act on it.

**Decision.** `hrsync run` does two passes under one run id. Pass 1 is the existing job-change pass. Pass 2 lists every Workday worker, skips those pass 1 touched, and runs the same per-person logic keyed `RECON-<worker id>` with reason "Reconcile". Pass 2 does not move the watermark. The summary counts both passes, so buckets still add up to `changes_seen`. There is one button, one tool and one command; `hrsync run --changes-only` restores the old behaviour.

**Consequences.** Ethena matches Workday for everyone after one run, and a second run writes nothing. A person with nothing to fix is audited as `patch_learner skipped`, so repeated runs do not pile up writes or queue rows (identical open items are deduplicated). Cost grows with headcount: at least one Workday call per worker on every run. A large tenant on a short schedule should run `--changes-only` often and a full run nightly.
