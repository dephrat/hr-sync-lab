"""Sync engine: Workday job changes -> Ethena learners, idempotent via audit + watermark."""
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import httpx

from hrsync import store
from hrsync.clients import HrisClient, HrSyncClientError, LearningClient
from hrsync.rules import Rules

RECON_PREFIX = "RECON-"
ATTR_KEYS = ("isManager", "country", "state", "customCategory", "department")


@dataclass
class RunSummary:
    """Counts for one run. Each change lands in exactly one bucket, named by the final
    outcome _sync_worker returned for it (via process_change), so skipped + queued + created + terminated +
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
    mode: str = "sync"

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
    cid, wid = change["id"], change["worker"]["id"]
    if not force and store.already_processed(store_conn, cid):
        at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            store.audit(store_conn, run_id, cid, wid, None, "skip", "skipped",
                        "already processed", at)
        finally:
            store_conn.commit()
        return "skipped"
    return _sync_worker(hris, learning, rules, store_conn, wid, cid, change["reason"]["id"],
                        run_id, now, create_missing)


def _sync_worker(hris, learning, rules: Rules, store_conn, wid: str, cid: str, reason_id: str,
                 run_id: str, now: datetime, create_missing: bool = True) -> str:
    """Bring one worker's learner in line with Workday. `cid` is the audit/queue key: a job
    change id, or RECON-<worker id> for a reconcile. Commits on return."""
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    recon = cid.startswith(RECON_PREFIX)
    audit = lambda lid, action, outcome, detail: store.audit(
        store_conn, run_id, cid, wid, lid, action, outcome, detail, at)
    lid = None
    try:
        worker = _step("load_worker", hris.get_worker, wid)
        profile, location = worker["jobProfile"]["id"], worker["location"]["descriptor"]
        attrs = rules.attributes_for(profile, location,
                                     worker["primarySupervisoryOrganization"]["descriptor"])
        terminating = reason_id == "Termination"
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
            elif terminating and recon:
                audit(None, "terminate_learner", "skipped", "no learner for terminated worker")
                return "ok"
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
            if recon and _step("terminate_learner", learning.get_learner, lid).get(
                    "status") == "TERMINATED":
                audit(lid, "terminate_learner", "skipped", "already TERMINATED")
                return "ok"
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


def _reconcile_pass(hris, learning, rules, store_conn, summary: RunSummary, now: datetime,
                    create_missing: bool, skip_workers: frozenset = frozenset()) -> bool:
    """Check every Workday worker (terminated ones too) not in `skip_workers`, keyed by
    RECON-<worker id>. Workers with an open review item are skipped too. Counts into `summary`.
    A failed worker stops the rest of the pass; the next run resumes from the top. False when a client error stopped the pass."""
    try:
        workers = hris.list_workers()
    except (HrSyncClientError, httpx.HTTPError):
        summary.failed += 1
        return False
    # A rule added since the item was queued unblocks it: close it and fall through to the
    # normal check of that worker below.
    for item_id, wid in store_conn.execute(
            "SELECT id, worker_id FROM review_queue WHERE status='open' AND"
            " reason IN ('UNKNOWN_LOCATION','UNKNOWN_PROFILE')").fetchall():
        w = next((x for x in workers if x["id"] == wid), None)
        if w is not None and rules.missing(w["jobProfile"]["id"], w["location"]["descriptor"]) is None:
            store_conn.execute("UPDATE review_queue SET status='resolved', resolved_at=?,"
                               " resolution='auto: rule added' WHERE id=?",
                               (now.strftime("%Y-%m-%dT%H:%M:%SZ"), item_id))
            store_conn.commit()
    # A person with an open review item is left alone until a reviewer closes it.
    open_workers = {r[0] for r in store_conn.execute(
        "SELECT worker_id FROM review_queue WHERE status='open'")}
    for w in workers:
        if w["id"] in skip_workers or w["id"] in open_workers:
            continue
        summary.changes_seen += 1
        terminated = bool(w.get("workerStatus", {}).get("terminated"))
        outcome = _sync_worker(hris, learning, rules, store_conn, w["id"],
                               RECON_PREFIX + w["id"],
                               "Termination" if terminated else "Reconcile",
                               summary.run_id, now, create_missing)
        setattr(summary, outcome, getattr(summary, outcome) + 1)
        if outcome == "failed":
            return False
    return True


def run(hris: HrisClient, learning: LearningClient, rules: Rules, store_conn,
        now: datetime | None = None, create_missing: bool = True,
        full: bool = True) -> RunSummary:
    """Pass 1: job changes since the watermark. Pass 2 (`full`): every worker not touched in
    pass 1, so drift with no Workday event is fixed too. One run_id, one summary.
    `full=False` is incremental only, for large tenants on a short schedule."""
    now = now or datetime.now(UTC)
    summary = RunSummary(run_id=new_run_id(now))
    started_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    completed_from = store.get_watermark(store_conn) or "1970-01-01T00:00:00Z"
    try:
        changes = hris.job_changes_since(completed_from)
    except (HrSyncClientError, httpx.HTTPError):
        summary.failed = 1
        store.record_run(store_conn, summary.to_dict(), started_at)
        return summary
    touched: set[str] = set()
    for change in changes:
        summary.changes_seen += 1
        outcome = process_change(hris, learning, rules, store_conn, change, summary.run_id,
                                 now, create_missing)
        if outcome == "failed":
            summary.failed += 1
            store.record_run(store_conn, summary.to_dict(), started_at)
            return summary
        setattr(summary, outcome, getattr(summary, outcome) + 1)
        if outcome != "skipped":  # a skipped change (inclusive watermark) did not reconcile
            touched.add(change["worker"]["id"])
    if changes:
        store.set_watermark(store_conn, max(c["completedDate"] for c in changes))
        store_conn.commit()
    if full:
        _reconcile_pass(hris, learning, rules, store_conn, summary, now, create_missing,
                        frozenset(touched))
    if changes or full:
        store.record_run(store_conn, summary.to_dict(), started_at)
    return summary


def reconcile(hris: HrisClient, learning: LearningClient, rules: Rules, store_conn,
              now: datetime | None = None, create_missing: bool = True) -> RunSummary:
    """Only the full pass: every worker, no job-change pass, watermark untouched."""
    now = now or datetime.now(UTC)
    summary = RunSummary(run_id=new_run_id(now), mode="reconcile")
    _reconcile_pass(hris, learning, rules, store_conn, summary, now, create_missing)
    store.record_run(store_conn, summary.to_dict(), now.strftime("%Y-%m-%dT%H:%M:%SZ"))
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
    if change_id.startswith(RECON_PREFIX):
        outcome = _sync_worker(hris, learning, rules, store_conn, worker_id, change_id,
                               "Reconcile", new_run_id(now), now, create_missing=True)
    else:
        change = hris.get_job_change(change_id)
        outcome = process_change(hris, learning, rules, store_conn, change, new_run_id(now),
                                 now, create_missing=True, force=True)
    return {"item_id": item_id, "learner_id": learner_id, "change_id": change_id,
            "outcome": outcome}
