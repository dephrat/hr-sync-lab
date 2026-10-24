# 0007: Ethena owns course assignment; the sync does not second-guess it

**Context.** Who gets each course is set by campaign audiences in Ethena, and Enablement edits them there. The sync kept its own copy of those audiences in the rules file and queued `ENROLLMENT_MISSING` whenever Ethena's enrollments differed from the copy. After any audience edit in Ethena the copy was wrong, and the queue filled with items HR Operations could not fix.

**Decision.** The sync has no expectation about courses. The rules file maps Workday jobs and offices to learner attributes only. After writing attributes the sync reads the learner's courses back from Ethena, leaves out removed ones, and audits `verify_enrollment ok` with the course names. Nothing is queued for a missing course, and the drift view has no course kind. When someone asks why a person is not in a course and their attributes are in sync, the answer is that Ethena does not give that course to people with their details, and that audiences are set in Ethena under Courses.

**Consequences.** One source of truth for course assignment, so no un-fixable queue items. The sync can no longer notice that Ethena failed to enroll someone it should have; that is Ethena's job, and the report's stale-enrollments section still shows enrollments that no longer match their audience. Old `ENROLLMENT_MISSING` rows still display with their code.
