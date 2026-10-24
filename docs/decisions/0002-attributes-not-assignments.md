# 0002: Write learner attributes, then verify enrollment

**Context.** Ethena's public API (OpenAPI 1.4.0) has no call to assign a course to a person. Learners are enrolled by training campaigns whose audience is a rule over learner attributes (`isManager`, `country`, `state`, `customCategory`, `department`). Enablement owns those audiences.

**Decision.** The sync maps Workday job profile and location to those attributes through `hrsync/rules.default.json`, patches only the attributes that differ, and lets Ethena enroll. It then lists the learner's campaigns and records them in the audit log.

**Consequences.** The sync never overrides Enablement's campaign setup; it feeds it correct data. The reconciliation report's stale-enrollments section catches the reverse case. When attributes change, Ethena marks unfinished enrollments the person no longer matches as removed (SYSTEM_REMOVED in its API; the exact real trigger is an assumption) and keeps completed ones, so the stale-enrollments section should normally be empty.

**Update (October 2026).** The rules table used to repeat the campaign audiences (`campaigns[].when`) so the verify step could queue `ENROLLMENT_MISSING` when an expected course did not appear. Audiences are edited in Ethena, so that copy drifted and the sync queued problems nobody could fix from the queue. The copy and the reason code are gone; see [0007](0007-ethena-owns-course-assignment.md).
