# 0003: Queue uncertain cases for a person instead of guessing

**Context.** Some changes cannot be applied safely: a location or job profile the rules table does not cover, two learners with the same email, no learner at all, or Ethena not enrolling someone as expected. A wrong guess either assigns harassment training for the wrong jurisdiction or attaches one person's record to another.

**Decision.** For the first four cases the engine writes nothing to Ethena for that change; for `ENROLLMENT_MISSING` the attributes were already written and only the check failed. In every case it opens a row in `review_queue` with a reason code (`UNKNOWN_LOCATION`, `UNKNOWN_PROFILE`, `AMBIGUOUS_LEARNER`, `NO_LEARNER_MATCH`, `ENROLLMENT_MISSING`) and a detail string, and audits it as `queue`/`queued`. HR Operations closes items with `hrsync resolve --map-learner` or `--skip`. An identical open item is not opened twice.

**Consequences.** The sync keeps going past a hard case instead of stopping. Someone has to work the queue; the runbook gives the action for each code and the report shows queue age. A queued change is not retried automatically, so fixing the rules table is not enough on its own: the item must be resolved to rerun the change.
