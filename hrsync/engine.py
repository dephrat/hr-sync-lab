"""Sync engine: Workday job changes -> Ethena learners, idempotent via audit + watermark."""
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import httpx

from hrsync import store
from hrsync.clients import HrisClient, HrSyncClientError, LearningClient
from hrsync.rules import Rules

ATTR_KEYS = ("isManager", "country", "state", "customCategory", "department")


@dataclass
class RunSummary:
    """Counts for one run. Each change lands in exactly one bucket, named by the final
    outcome process_change returned for it, so skipped + queued + created + terminated +
    patched + ok + failed == changes_seen. A change that created a learner and was then
    queued (for example ENROLLMENT_MISSING) counts as queued, not created. `ok` means the
    learner's attributes were already correct and enrollment verified."""
    run_id: str
    changes_seen: int = 0
    patched: int = 0
    created: int = 0
    terminated: int = 0
    queued: int = 0
    skipped: int = 0
    failed: int = 0
    ok: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class _StepFailed(Exception):
    def __init__(self, action: str, exc: Exception):
        super().__init__(str(exc))
        self.action = action


def queue_item(store_conn, run_id, change_id, worker_id, learner_id, reason, detail, at) -> str:
    """Open a review item and audit it as `queue`; returns outcome 'queued'."""
    store.queue_item(store_conn, change_id, worker_id, reason, detail, at)
    store.audit(store_conn, run_id, change_id, worker_id, learner_id, "queue", "queued",
                f"{reason}: {detail}", at)
    return "queued"


def _step(action, fn, *args):
    try:
        return fn(*args)
    except (HrSyncClientError, httpx.HTTPError) as exc:
        raise _StepFailed(action, exc) from exc


def process_change(hris: HrisClient, learning: LearningClient, rules: Rules, store_conn,
                   change: dict, run_id: str, now: datetime, create_missing: bool = True,
                   force: bool = False) -> str:
    """Process one job change. Returns one of: skipped, queued, created, terminated, patched,
    ok, failed. Commits on return. A Termination for a worker with no learner is queued
    NO_LEARNER_MATCH regardless of create_missing. `force` reprocesses a change even if a
    terminal audit row exists (used when a reviewer resolves its queue item)."""
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cid, wid = change["id"], change["worker"]["id"]
    audit = lambda lid, action, outcome, detail: store.audit(
        store_conn, run_id, cid, wid, lid, action, outcome, detail, at)
    lid = None
    try:
        if not force and store.already_processed(store_conn, cid):
            audit(None, "skip", "skipped", "already processed")
            return "skipped"
        worker = _step("load_worker", hris.get_worker, wid)
        profile, location = worker["jobProfile"]["id"], worker["location"]["descriptor"]
        attrs = rules.attributes_for(profile, location,
                                     worker["primarySupervisoryOrganization"]["descriptor"])
        terminating = change["reason"]["id"] == "Termination"

        def unknown():
            kind = rules.missing(profile, location)
            value = profile if kind == "profile" else location
            return queue_item(store_conn, run_id, cid, wid, lid, f"UNKNOWN_{kind.upper()}",
                              f"{kind} {value!r} not in rules", at)

        # Resolve learner.
        created = False
        lid = store.mapped_learner(store_conn, wid)
        if lid is None:
            email = worker["primaryWorkEmail"]
            found = _step("map_learner", learning.find_learners_by_email, email)
            if len(found) > 1:
                return queue_item(store_conn, run_id, cid, wid, None, "AMBIGUOUS_LEARNER",
                                  f"{email} matches {', '.join(f['id'] for f in found)}", at)
            if found:
                lid = found[0]["id"]
                store.map_learner(store_conn, wid, lid, "email", at)
                audit(lid, "map_learner", "ok", "email")
            elif terminating or not create_missing:
                return queue_item(store_conn, run_id, cid, wid, None, "NO_LEARNER_MATCH",
                                  f"no learner with email {email}", at)
            elif attrs is None:
                return unknown()
            else:
                body = {"name": worker["descriptor"], "email": email, "language": "en",
                        "workerType": "Employee", **attrs}
                lid = _step("create_learner", learning.create_learner, body)
                store.map_learner(store_conn, wid, lid, "created", at)
                audit(lid, "create_learner", "ok", json.dumps(body, sort_keys=True))
                audit(lid, "map_learner", "ok", "created")
                created = True

        if terminating:
            _step("terminate_learner", learning.patch_learner, lid, {"status": "TERMINATED"})
            audit(lid, "terminate_learner", "ok", "status TERMINATED")
            return "terminated"

        if attrs is None:
            return unknown()

        learner = _step("patch_learner", learning.get_learner, lid)
        diff = {k: attrs[k] for k in ATTR_KEYS if learner.get(k) != attrs[k]}
        if diff:
            _step("patch_learner", learning.patch_learner, lid, diff)
            audit(lid, "patch_learner", "ok", json.dumps(diff, sort_keys=True))
        else:
            audit(lid, "patch_learner", "skipped", "no attribute changes")

        expected = rules.expected_campaigns(attrs)
        enrolled = {c["campaignName"] for c in
                    _step("verify_enrollment", learning.campaigns_for_learner, lid)}
        missing = sorted(expected - enrolled)
        if missing:
            return queue_item(store_conn, run_id, cid, wid, lid, "ENROLLMENT_MISSING",
                              ", ".join(missing), at)
        audit(lid, "verify_enrollment", "ok", ", ".join(sorted(expected)))
        return "created" if created else "patched" if diff else "ok"
    except _StepFailed as exc:
        audit(lid, exc.action, "failed", str(exc))
        return "failed"
    finally:
        # Partial writes (a mapping, a patch) are safe to commit: the change has no terminal
        # audit row yet, so the next run processes it again from the start.
        store_conn.commit()


def new_run_id(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ") + secrets.token_hex(2)


def run(hris: HrisClient, learning: LearningClient, rules: Rules, store_conn,
        now: datetime | None = None, create_missing: bool = True) -> RunSummary:
    now = now or datetime.now(UTC)
    summary = RunSummary(run_id=new_run_id(now))
    started_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    completed_from = store.get_watermark(store_conn) or "1970-01-01T00:00:00Z"
    try:
        changes = hris.job_changes_since(completed_from)
    except (HrSyncClientError, httpx.HTTPError):
        summary.failed = 1
        return summary
    for change in changes:
        summary.changes_seen += 1
        outcome = process_change(hris, learning, rules, store_conn, change, summary.run_id,
                                 now, create_missing)
        if outcome == "failed":
            summary.failed += 1
            store.record_run(store_conn, summary.to_dict(), started_at)
            return summary
        setattr(summary, outcome, getattr(summary, outcome) + 1)
    if changes:
        store.set_watermark(store_conn, max(c["completedDate"] for c in changes))
        store_conn.commit()
        store.record_run(store_conn, summary.to_dict(), started_at)
    return summary


def resolve_with_learner(hris, learning, rules: Rules, store_conn, item_id: int,
                         learner_id: str, now: datetime, note: str = "") -> dict:
    """Map the queue item's worker to `learner_id`, mark it resolved and reprocess its change.
    Raises ValueError when the item is missing or not open; HrSyncClientError propagates."""
    item = store_conn.execute("SELECT change_id, worker_id, status FROM review_queue WHERE id=?",
                              (item_id,)).fetchone()
    if item is None or item[2] != "open":
        raise ValueError(f"queue item {item_id} not found or not open")
    change_id, worker_id, _ = item
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    learning.get_learner(learner_id)
    store.map_learner(store_conn, worker_id, learner_id, "manual", at)
    store_conn.execute("UPDATE review_queue SET status='resolved', resolved_at=?, resolution=?"
                       " WHERE id=?", (at, f"mapped:{learner_id}{' ' + note if note else ''}", item_id))
    store_conn.commit()
    change = hris.get_job_change(change_id)
    outcome = process_change(hris, learning, rules, store_conn, change, new_run_id(now), now,
                             create_missing=True, force=True)
    return {"item_id": item_id, "learner_id": learner_id, "change_id": change_id,
            "outcome": outcome}
