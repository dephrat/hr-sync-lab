"""JSON endpoints for the demo page. Every write goes through the sync engine or store."""
import json
import sqlite3
import threading
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hrsync import engine, store
from hrsync.clients import ClientError, HrSyncClientError
from mcp_server import tools

router = APIRouter(prefix="/api")

# One lock for every write path (run, resolve, skip, reset) so concurrent clicks cannot interleave.
WRITE_LOCK = threading.Lock()
WORKER_ID = r"^[A-Za-z0-9-]+$"

# One sentence per reason code, taken from the "What happened" column of docs/runbook.md.
REASON_HELP: dict[str, str] = {
    "UNKNOWN_LOCATION": "The worker's Workday location is not in the rules table, so the sync "
                        "does not know which country or province rules apply. Nothing was "
                        "written to Ethena.",
    "UNKNOWN_PROFILE": "The worker's Workday job profile is not in the rules table, so the sync "
                       "does not know which attributes apply. Nothing was written to Ethena.",
    "AMBIGUOUS_LEARNER": "Two or more Ethena learners share the worker's email, so the sync "
                         "cannot tell which one is the real person.",
    "NO_LEARNER_MATCH": "No Ethena learner has the worker's email, and the sync was told not to "
                        "create one, or the change is a termination for someone never in Ethena.",
    "ENROLLMENT_MISSING": "Attributes were written, but Ethena did not enroll the person in "
                          "every campaign the rules expect.",
}


class ResolveIn(BaseModel):
    learner_id: str = Field(pattern=r"^[A-Za-z0-9]+$")
    note: str = ""


class SkipIn(BaseModel):
    note: str = ""


class ExplainIn(BaseModel):
    worker_id: str = Field(pattern=WORKER_ID)
    campaign_name: str


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _bundle(request: Request):
    return request.app.state.bundle


class _Store:
    """Per-request store connection, closed on exit."""
    def __init__(self, request: Request):
        self.path = _bundle(request).settings.sync_db

    def __enter__(self):
        self.conn = store.connect(self.path)
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()


def _name(b, cache: dict, worker_id: str) -> str:
    if worker_id not in cache:
        try:
            cache[worker_id] = b.hris.get_worker(worker_id)["descriptor"]
        except (HrSyncClientError, httpx.HTTPError):
            cache[worker_id] = worker_id
    return cache[worker_id]


def _kv(detail: str) -> str:
    try:
        diff = json.loads(detail)
        return ", ".join(f"{k}={json.dumps(v)}" for k, v in diff.items())
    except (ValueError, AttributeError):
        return detail


def _outcome(rows: list[dict]) -> str:
    acts = {(r["action"], r["outcome"]) for r in rows}
    if ("skip", "skipped") in acts:
        return "skipped"
    if any(r["outcome"] == "failed" for r in rows):
        return "failed"
    if ("queue", "queued") in acts:
        return "queued"
    if ("terminate_learner", "ok") in acts:
        return "terminated"
    if ("create_learner", "ok") in acts:
        return "created"
    if ("patch_learner", "ok") in acts:
        return "patched"
    return "ok"


def _sentence(name, wid, reason, outcome, rows) -> str:
    head = f"{name} ({wid}): {reason}."
    by = {r["action"]: r for r in rows}
    verified = by.get("verify_enrollment", {}).get("detail", "")
    if outcome == "skipped":
        return f"{head} Already processed, skipped."
    if outcome == "failed":
        bad = next(r for r in rows if r["outcome"] == "failed")
        return f"{head} Failed at {bad['action']}: {bad['detail']}."
    if outcome == "queued":
        return f"{head} Queued for review: {by['queue']['detail']}."
    if outcome == "terminated":
        return f"{head} Learner set to TERMINATED."
    if outcome == "created":
        return f"{head} Created learner in Ethena. Verified: {verified}."
    if outcome == "patched":
        return (f"{head} Set {_kv(by['patch_learner']['detail'])} in Ethena. "
                f"Verified: {verified}.")
    return f"{head} Attributes already correct. Verified: {verified}."


@router.post("/run")
def run_sync(request: Request):
    with WRITE_LOCK:
        return _run_sync(request)


def _run_sync(request: Request):
    b = _bundle(request)
    with _Store(request) as conn:
        summary = engine.run(b.hris, b.learning, b.rules, conn)
        cur = conn.execute("SELECT * FROM audit WHERE run_id=? ORDER BY id", (summary.run_id,))
        cols = [c[0] for c in cur.description]
        grouped: dict[str, list[dict]] = {}
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            grouped.setdefault(row["change_id"], []).append(row)
    names: dict = {}
    changes = []
    for change_id, rows in grouped.items():
        wid = rows[0]["worker_id"]
        try:
            reason = b.hris.get_job_change(change_id)["reason"]["descriptor"]
        except (HrSyncClientError, httpx.HTTPError):
            reason = "Job change"
        name = _name(b, names, wid)
        outcome = _outcome(rows)
        changes.append({
            "change_id": change_id, "worker_id": wid, "worker_name": name, "reason": reason,
            "outcome": outcome,
            "actions": [{"action": r["action"], "outcome": r["outcome"], "detail": r["detail"]}
                        for r in rows],
            "sentence": _sentence(name, wid, reason, outcome, rows)})
    return {"summary": summary.to_dict(), "changes": changes}


def _candidates(b, item: dict) -> list[dict]:
    try:
        if item["reason"] == "AMBIGUOUS_LEARNER":
            ids = [i.strip() for i in item["detail"].split("matches ", 1)[1].split(",")]
            found = [b.learning.get_learner(i) for i in ids]
        else:
            found = b.learning.find_learners_by_email(
                b.hris.get_worker(item["worker_id"])["primaryWorkEmail"])
    except (HrSyncClientError, httpx.HTTPError, IndexError):
        return []
    return [{"id": f["id"], "name": f.get("name"), "email": f.get("email")} for f in found]


@router.get("/queue")
def get_queue(request: Request, all: int = 0):
    b = _bundle(request)
    with _Store(request) as conn:
        where = "" if all else " WHERE status='open'"
        cur = conn.execute(f"SELECT * FROM review_queue{where} ORDER BY id")
        cols = [c[0] for c in cur.description]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]
    names: dict = {}
    for it in items:
        it["worker_name"] = _name(b, names, it["worker_id"])
        it["reason_help"] = REASON_HELP.get(it["reason"], "")
        it["needs_rule"] = it["reason"].startswith("UNKNOWN_")
        it["candidates"] = _candidates(b, it)
    return items


@router.post("/queue/{item_id}/resolve")
def resolve(item_id: int, body: ResolveIn, request: Request):
    b = _bundle(request)
    with WRITE_LOCK, _Store(request) as conn:
        try:
            return engine.resolve_with_learner(b.hris, b.learning, b.rules, conn, item_id,
                                               body.learner_id, datetime.now(UTC), body.note)
        except ValueError as exc:
            return _err(404, str(exc))
        except (HrSyncClientError, httpx.HTTPError) as exc:
            return _err(502, str(exc))


@router.post("/queue/{item_id}/skip")
def skip(item_id: int, body: SkipIn, request: Request):
    at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with WRITE_LOCK, _Store(request) as conn:
        if not store.skip_item(conn, item_id, body.note, at):
            return _err(404, f"queue item {item_id} not found or not open")
    return {"item_id": item_id, "status": "skipped"}


@router.get("/workers")
def search_workers(request: Request, q: str = ""):
    b = _bundle(request)
    needle = q.strip().lower()
    out = []
    for w in b.hris.list_workers():
        if needle and not any(needle in str(w.get(k, "")).lower()
                              for k in ("id", "descriptor", "primaryWorkEmail")):
            continue
        out.append({"id": w["id"], "descriptor": w["descriptor"],
                    "primaryWorkEmail": w["primaryWorkEmail"],
                    "jobProfile": w["jobProfile"]["descriptor"],
                    "location": w["location"]["descriptor"],
                    "department": w["primarySupervisoryOrganization"]["descriptor"],
                    "isManager": w["isManager"], "workerStatus": w["workerStatus"]})
        if len(out) == 25:
            break
    return out


@router.get("/workers/{worker_id}")
def worker_detail(request: Request, worker_id: str = Path(pattern=WORKER_ID)):
    b = _bundle(request)
    with _Store(request) as conn:
        try:
            worker = tools.get_worker(b.hris, conn, worker_id)
        except ClientError as exc:
            if exc.status == 404:
                return _err(404, f"worker {worker_id} not found")
            raise
        status = tools.get_learner_status(b.hris, b.learning, conn, worker_id)
        audit = tools.get_audit_trail(conn, worker_id, 50)
    candidates = []
    if status.get("ambiguous"):
        candidates = [{"id": c} for c in status["candidates"]]
    return {"worker": {k: v for k, v in worker.items() if k != "learner_id"},
            "learner_id": worker["learner_id"] or (status["learner"] or {}).get("id"),
            "learner": status["learner"], "candidates": candidates,
            "enrollments": status["campaigns"], "audit": audit}


def _audience_text(aud: dict) -> str:
    parts = []
    for key in ("country", "state", "customCategory"):
        if aud.get(key):
            parts.append(", ".join(aud[key]))
    if "isManager" in aud:
        parts.append("managers" if aud["isManager"] else "non-managers")
    return " + ".join(parts) or "everyone"


@router.get("/campaigns")
def campaigns(request: Request):
    b = _bundle(request)
    # The Ethena API does not expose a campaign's audience. This reads the mock's sqlite file
    # directly, only to display it; the sync itself never does this.
    conn = sqlite3.connect(b.settings.ethena_db)
    try:
        auds = {cid: json.loads(a) for cid, a in
                conn.execute("SELECT id, audience FROM campaigns")}
    finally:
        conn.close()
    return [{"id": c["id"], "campaignName": c["campaignName"], "courseName": c["courseName"],
             "audience_text": _audience_text(auds.get(c["id"], {}))}
            for c in b.learning.list_campaigns()]


@router.post("/explain")
def explain(body: ExplainIn, request: Request):
    b = _bundle(request)
    with _Store(request) as conn:
        try:
            b.hris.get_worker(body.worker_id)  # explain_gap alone would not notice a bad id
            sentence = tools.explain_gap(b.hris, b.learning, conn, b.rules, body.worker_id,
                                         body.campaign_name)
        except ClientError as exc:
            if exc.status == 404:
                return _err(404, f"worker {body.worker_id} not found")
            raise
    return {"sentence": sentence}
