"""Plain functions behind the MCP tools. Reads never write; the two writes need confirm=True."""
from dataclasses import asdict
from datetime import UTC, datetime

from hrsync import engine, store
from hrsync.clients import HrSyncClientError

CONFIRM_REQUIRED = {"error": "confirm required"}


def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_worker(hris, store_conn, worker_id: str) -> dict:
    return {**hris.get_worker(worker_id), "learner_id": store.mapped_learner(store_conn, worker_id)}


def get_learner_status(hris, learning, store_conn, worker_id: str) -> dict:
    lid = store.mapped_learner(store_conn, worker_id)
    out = {"worker_id": worker_id, "learner": None, "campaigns": []}
    if lid is None:
        found = learning.find_learners_by_email(hris.get_worker(worker_id)["primaryWorkEmail"])
        if len(found) > 1:
            out["ambiguous"] = True
            out["candidates"] = [f["id"] for f in found]
            return out
        if not found:
            return out
        lid = found[0]["id"]
    out["learner"] = learning.get_learner(lid)
    out["campaigns"] = [{k: c.get(k) for k in ("campaignName", "completionStatus", "deadline")}
                        for c in learning.campaigns_for_learner(lid, include_removed=True)]
    return out


def get_audit_trail(store_conn, worker_id: str, limit: int = 20) -> list[dict]:
    return _rows(store_conn.execute(
        "SELECT * FROM audit WHERE worker_id=? ORDER BY id DESC LIMIT ?", (worker_id, limit)))


def list_queue(store_conn) -> list[dict]:
    return _rows(store_conn.execute("SELECT * FROM review_queue WHERE status='open' ORDER BY id"))


# Plain-language names for review reasons, the same wording the demo page uses.
# (A legacy ENROLLMENT_MISSING row falls back to the generic wording.)
REASON_WORDS = {"UNKNOWN_LOCATION": "no courses are specified for their office yet",
                "UNKNOWN_PROFILE": "no courses are specified for their job yet",
                "AMBIGUOUS_LEARNER": "two Ethena accounts share their email",
                "NO_LEARNER_MATCH": "they have no Ethena account"}

ATTR_KEYS = ("isManager", "country", "state", "customCategory", "department")


def _learner_id(learning, store_conn, worker_id, worker) -> str | None:
    """The mapped learner, or before any mapping the single learner with the worker's email."""
    lid = store.mapped_learner(store_conn, worker_id)
    if lid or not worker:
        return lid
    try:
        found = learning.find_learners_by_email(worker["primaryWorkEmail"])
    except HrSyncClientError:
        return None
    return found[0]["id"] if len(found) == 1 else None


def explain_gap(hris, learning, store_conn, rules, worker_id: str, campaign_name: str) -> str:
    """One plain sentence on why a person is or is not in a course, for someone in HR."""
    try:
        worker = hris.get_worker(worker_id)
    except HrSyncClientError:
        worker = None
    lid = _learner_id(learning, store_conn, worker_id, worker)
    name = (worker or {}).get("descriptor") or worker_id
    course = campaign_name
    held = []
    if lid:
        try:
            held = [c for c in learning.campaigns_for_learner(lid)
                    if c.get("campaignName") == campaign_name]
        except HrSyncClientError:
            held = []
    if worker and worker.get("workerStatus", {}).get("terminated"):
        if any(c.get("completionStatus") == "COMPLETED" for c in held):
            return (f"{name} has left the company and completed {course} before leaving; "
                    "the record is kept.")
        return (f"{name} has left the company. Ethena gives no training to people who left, "
                "and their unfinished courses were removed.")
    for c in held:  # campaigns_for_learner already leaves out REMOVED enrollments
        if c.get("completionStatus") == "COMPLETED":
            return f"{name} is enrolled in {course} and has completed it."
        due = str(c.get("deadline") or "")[:10]
        return f"{name} is enrolled in {course}" + (f", due {due}." if due else ".")
    item = store_conn.execute(
        "SELECT change_id, reason, detail FROM review_queue WHERE worker_id=? AND status='open'"
        " ORDER BY id DESC LIMIT 1", (worker_id,)).fetchone()
    if item:
        _, reason, _ = item
        why = REASON_WORDS.get(reason, "the sync could not decide on its own")
        return (f"{name} is waiting for manual review because {why}. Until someone deals with "
                f"it, Ethena has not been updated, so {name} is not in {course} yet.")
    try:
        pending = hris.pending_changes(worker_id)
    except (HrSyncClientError, AttributeError):
        pending = []
    if pending:
        p = pending[0]
        return (f"{name}'s {p['reason']['descriptor'].lower()} is waiting for approval in "
                "Workday. The sync only acts on approved changes.")
    mark = store.get_watermark(store_conn)
    if mark:
        try:
            newer = [c for c in hris.job_changes_since(mark)
                     if c["worker"]["id"] == worker_id and c["completedDate"] > mark]
        except (HrSyncClientError, AttributeError):
            newer = []
        if newer:
            return (f"{name}'s {newer[-1]['reason']['descriptor'].lower()} was approved in "
                    "Workday after the last sync. Run sync to carry it to Ethena.")
    if lid and worker:
        try:
            learner = learning.get_learner(lid)
        except HrSyncClientError:
            learner = None
        attrs = rules.attributes_for(worker["jobProfile"]["id"], worker["location"]["descriptor"],
                                     worker["primarySupervisoryOrganization"]["descriptor"])
        if learner and attrs is not None and all(learner.get(k) == attrs[k] for k in ATTR_KEYS):
            return (f"{course} is not given to {name} by Ethena with their current details. "
                    "Who gets each course is set in Ethena under Courses.")
    last = store_conn.execute(
        "SELECT created_at FROM audit WHERE worker_id=? ORDER BY id DESC LIMIT 1",
        (worker_id,)).fetchone()
    if last is None:
        return f"No sync has looked at {name} yet. Press Run sync."
    return (f"The sync last checked {name} at {str(last[0]).replace('T', ' ').replace('Z', ' UTC')} "
            f"and found nothing that explains why {course} is missing. Run sync to check again.")


def resolve_queue_item(hris, learning, rules, store_conn, item_id: int, learner_id: str,
                       confirm: bool = False) -> dict:
    if confirm is not True:
        return dict(CONFIRM_REQUIRED)
    try:
        return engine.resolve_with_learner(hris, learning, rules, store_conn, item_id,
                                           learner_id, datetime.now(UTC), "via mcp")
    except (ValueError, HrSyncClientError) as exc:
        return {"error": str(exc)}


def rerun_sync(hris, learning, rules, store_conn, confirm: bool = False) -> dict:
    if confirm is not True:
        return dict(CONFIRM_REQUIRED)
    return asdict(engine.run(hris, learning, rules, store_conn))
