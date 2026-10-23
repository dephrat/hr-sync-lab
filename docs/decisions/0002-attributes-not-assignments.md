# 0002: Write learner attributes, then verify enrollment

**Context.** Ethena's public API (OpenAPI 1.4.0) has no call to assign a course to a person. Learners are enrolled by training campaigns whose audience is a rule over learner attributes (`isManager`, `country`, `state`, `customCategory`, `department`). Enablement owns those audiences.

**Decision.** The sync maps Workday job profile and location to those attributes through `hrsync/rules.default.json`, patches only the attributes that differ, and lets Ethena enroll. It then lists the learner's campaigns and checks that every campaign the rules table expects is present. If any is missing it queues `ENROLLMENT_MISSING` with the campaign names.

**Consequences.** The sync never overrides Enablement's campaign setup; it feeds it correct data. The rules table repeats the campaign audiences so the sync can know what to expect, and the two can drift apart. The verify step catches that drift on the next change for an affected person, and the reconciliation report's stale-enrollments section catches the reverse case. Ethena never removes an enrollment when attributes change, so a demotion does not unenroll anyone.
