"""JSON endpoints for the demo page. Every write goes through the sync engine or store."""
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hrsync import engine, store
from hrsync import rules as rules_mod
from hris.db import JOB_PROFILES, LOCATIONS, PROFILE_DEPARTMENT, profile_descriptor
from hrsync.engine import ATTR_KEYS
from hrsync.clients import ClientError, HrSyncClientError
from mcp_server import tools

router = APIRouter(prefix="/api")

# One lock for every write path (run, resolve, skip, reset) so concurrent clicks cannot interleave.
WRITE_LOCK = threading.Lock()
WORKER_ID = r"^[A-Za-z0-9-]+$"

# One sentence per reason code, taken from the "What happened" column of docs/runbook.md.
REASON_HELP: dict[str, str] = {
    "UNKNOWN_LOCATION": "No courses are specified for this person's office yet. Add the "
                        "office under Course rules, then run sync.",
    "UNKNOWN_PROFILE": "No courses are specified for this person's job yet. Add the job "
                       "under Course rules, then run sync.",
    "AMBIGUOUS_LEARNER": "Two or more Ethena learners share the worker's email, so the sync "
                         "cannot tell which one is the real person.",
    "NO_LEARNER_MATCH": "No Ethena learner has the worker's email, and the sync was told not to "
                        "create one, or the change is a termination for someone never in Ethena.",
    "ENROLLMENT_MISSING": "Attributes were written, but Ethena did not enroll the person in "
                          "every campaign the rules expect.",
}


REASON_LABEL: dict[str, str] = {
    "UNKNOWN_LOCATION": "Courses not specified for this office",
    "UNKNOWN_PROFILE": "Courses not specified for this job",
    "AMBIGUOUS_LEARNER": "Two Ethena accounts share this email",
    "NO_LEARNER_MATCH": "No Ethena account for this person",
    "ENROLLMENT_MISSING": "An expected course did not appear",
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


def _phrase(detail: str) -> str:
    """Turn the patch diff (a JSON dict of changed attributes) into plain words."""
    try:
        diff = dict(json.loads(detail))
    except (ValueError, TypeError):
        return detail
    parts = []
    if "isManager" in diff:
        parts.append("now a manager" if diff.pop("isManager") else "no longer a manager")
    if "country" in diff:
        text = f"country set to {diff.pop('country')}"
        if diff.get("state"):
            text += f" ({diff.pop('state')})"
        parts.append(text)
    if "state" in diff:
        v = diff.pop("state")
        parts.append("state cleared" if v is None else f"state set to {v}")
    if "customCategory" in diff:
        v = diff.pop("customCategory")
        parts.append("category cleared" if v is None else f"category set to {v}")
    if "department" in diff:
        parts.append(f"department now {diff.pop('department')}")
    return ", ".join(parts) or "attributes updated"


GROUP = {"queued": "needs_person", "patched": "updated", "created": "updated",
         "terminated": "left", "ok": "unchanged", "skipped": "unchanged", "failed": "failed"}


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
    trained = f"Training confirmed: {by.get('verify_enrollment', {}).get('detail', '')}."
    if outcome == "skipped":
        return f"{head} Already handled in an earlier run."
    if outcome == "failed":
        bad = next(r for r in rows if r["outcome"] == "failed")
        return f"{head} Failed at {bad['action']}: {bad['detail']}"
    if outcome == "queued":
        code, _, detail = by["queue"]["detail"].partition(": ")
        # Quote the offending value instead of repeating the engine's detail text.
        quoted = re.search(r"'([^']+)'", detail)
        tail = f" ({quoted.group(1)})" if quoted else ""
        return f"{head} Needs a person: {REASON_LABEL.get(code, code)}{tail}."
    if outcome == "terminated":
        return f"{head} Marked as left the company; Ethena will assign no more training."
    if outcome == "created":
        return f"{head} Added to Ethena. {trained}"
    if outcome == "patched":
        phrase = _phrase(by["patch_learner"]["detail"])
        return f"{head} {phrase[:1].upper() + phrase[1:]}. {trained}"
    return f"{head} Nothing to change. {trained}"


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
        if change_id.startswith(engine.RECON_PREFIX):
            reason = "Spot check"
        else:
            try:
                reason = b.hris.get_job_change(change_id)["reason"]["descriptor"]
            except (HrSyncClientError, httpx.HTTPError):
                reason = "Job change"
        name = _name(b, names, wid)
        outcome = _outcome(rows)
        changes.append({
            "change_id": change_id, "worker_id": wid, "worker_name": name, "reason": reason,
            "outcome": outcome, "group": GROUP[outcome],
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
        it["reason_label"] = REASON_LABEL.get(it["reason"], it["reason"])
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
        except ClientError as exc:
            if exc.status == 404:
                return _err(404, f"learner {body.learner_id} not found in Ethena")
            return _err(502, str(exc))
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
def search_workers(request: Request, q: str = "", limit: int = Query(25, ge=1, le=500),
                   offset: int = Query(0, ge=0)):
    """Filtered, paged worker list: {"total", "data"} like the mock's own envelopes."""
    b = _bundle(request)
    needle = q.strip().lower()
    out = []
    for w in b.hris.list_workers():
        haystack = [w["id"], w["descriptor"], w["primaryWorkEmail"],
                    w["jobProfile"]["descriptor"], w["location"]["descriptor"],
                    w["primarySupervisoryOrganization"]["descriptor"]]
        if needle and not any(needle in str(v).lower() for v in haystack):
            continue
        out.append({"id": w["id"], "descriptor": w["descriptor"],
                    "primaryWorkEmail": w["primaryWorkEmail"],
                    "jobProfile": w["jobProfile"]["descriptor"],
                    "location": w["location"]["descriptor"],
                    "department": w["primarySupervisoryOrganization"]["descriptor"],
                    "isManager": w["isManager"], "workerStatus": w["workerStatus"]})
    return {"total": len(out), "data": out[offset:offset + limit]}


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
    # The stored rule as field = value, unreworded, so what the page shows is the data.
    parts = []
    for key, val in aud.items():
        if isinstance(val, list):
            val = ", ".join(map(str, val))
        elif isinstance(val, bool):
            val = "true" if val else "false"
        parts.append(f"{key} = {val}")
    return " \u00b7 ".join(parts) or "everyone (no rules)"


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
    if body.campaign_name not in {c["campaignName"] for c in b.learning.list_campaigns()}:
        return _err(422, "unknown campaign")
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


def _enrollment_map(b) -> tuple[list[dict], dict]:
    camps = {c["id"]: c["campaignName"] for c in b.learning.list_campaigns()}
    by_learner: dict[str, list[dict]] = {}
    for e in b.learning.list_all_enrollments():
        by_learner.setdefault(e["learnerId"], []).append({
            "id": e["id"], "campaignName": camps.get(e["trainingCampaignId"], e["trainingCampaignId"]),
            "completionStatus": e.get("completionStatus"), "deadline": e.get("deadline")})
    return b.learning.list_learners(), by_learner


@router.get("/learners")
def learners(request: Request):
    b = _bundle(request)
    rows, enr = _enrollment_map(b)
    return [{"id": l["id"], "name": l.get("name"), "email": l.get("email"),
             "status": l.get("status"), "country": l.get("country"), "state": l.get("state"),
             "isManager": l.get("isManager"), "customCategory": l.get("customCategory"),
             "department": l.get("department"), "managerLearnerId": l.get("managerLearnerId"),
             "workerType": l.get("workerType"), "enrollments": enr.get(l["id"], [])}
            for l in rows]


@router.get("/drift")
def drift(request: Request):
    b = _bundle(request)
    rules = b.rules
    rows, enr = _enrollment_map(b)
    by_id = {l["id"]: l for l in rows}
    by_email: dict[str, list[dict]] = {}
    for l in rows:
        by_email.setdefault((l.get("email") or "").lower(), []).append(l)
    with _Store(request) as conn:
        maps = dict(conn.execute("SELECT worker_id, learner_id FROM id_map").fetchall())
        queued: dict[str, list[int]] = {}
        for qid, wid in conn.execute("SELECT id, worker_id FROM review_queue WHERE status='open'"):
            queued.setdefault(wid, []).append(qid)
    items = []
    for w in b.hris.list_workers():
        wid, name = w["id"], w["descriptor"]
        terminated = bool(w.get("workerStatus", {}).get("terminated"))
        learner, kind, sentence = by_id.get(maps.get(wid)), None, None
        if learner is None:
            found = by_email.get((w.get("primaryWorkEmail") or "").lower(), [])
            if len(found) > 1:
                kind, sentence = "ambiguous", (f"{name}: two Ethena accounts share this email; "
                                               "a person has to pick.")
            elif found:
                learner = found[0]
            elif not terminated:
                kind, sentence = "no_learner", f"{name}: in Workday, no Ethena account yet."
            else:
                continue
        if kind is None and terminated:
            if learner.get("status") != "ACTIVE":
                continue
            kind = "terminated"
            sentence = f"{name}: left the company in Workday; Ethena still has them active."
        elif kind is None:
            profile, loc = w["jobProfile"]["id"], w["location"]["descriptor"]
            attrs = rules.attributes_for(
                profile, loc, w["primarySupervisoryOrganization"]["descriptor"])
            if attrs is None:
                kind = "needs_rule"
                if rules.missing(profile, loc) == "location":
                    sentence = (f"({loc} office): courses not specified for this office. "
                                "Add it under Course rules.")
                else:
                    sentence = (f"({w['jobProfile']['descriptor']}): courses not specified "
                                "for this job. Add it under Course rules.")
                sentence = f"{name} {sentence}"
            else:
                diff = {k: attrs[k] for k in ATTR_KEYS if learner.get(k) != attrs[k]}
                if diff:
                    kind = "attrs"
                    sentence = (f"{name}: Workday says {_phrase(json.dumps(diff))}; "
                                "Ethena has not been told.")
                else:
                    have = {e["campaignName"] for e in enr.get(learner["id"], [])}
                    missing = sorted(rules.expected_campaigns(attrs) - have)
                    if missing:
                        kind = "courses"
                        sentence = (f"{name}: should be in {', '.join(missing)} "
                                    "but is not enrolled.")
        if kind is None:
            continue
        items.append({"worker_id": wid, "worker_name": name,
                      "learner_id": learner["id"] if learner else None, "kind": kind,
                      "sentence": sentence, "in_queue": wid in queued,
                      "open_queue": queued.get(wid, [])})
    return {"count": len(items), "items": items}


# ---------- translation rules ----------

def _rules_data(b) -> dict:
    return json.loads(b.rules_path.read_text())


def _valid(options) -> set:
    return {o["value"] for o in options}


class OfficeIn(BaseModel):
    office: str
    country: str | None = None
    region: str | None = None


class JobIn(BaseModel):
    job: str
    isManager: bool | None = None


def _audiences(b) -> list[tuple[str, dict]]:
    """(campaignName, audience) for every campaign. Read from the mock's sqlite for display
    only; the Ethena API does not expose audiences and the sync never reads them."""
    conn = sqlite3.connect(b.settings.ethena_db)
    try:
        return [(name, json.loads(aud)) for name, aud in
                conn.execute("SELECT campaign_name, audience FROM campaigns WHERE status='ACTIVE'")]
    finally:
        conn.close()


def _office_courses(auds, row) -> list[str]:
    """Location-based courses an office leads to: campaigns that key on country/region."""
    if not row.get("country"):
        return []
    region = row.get("region")
    out = []
    for name, a in auds:
        if not ({"country", "state", "customCategory"} & a.keys()):
            continue
        if "country" in a and row["country"] not in a["country"]:
            continue
        if "state" in a and region not in a["state"]:
            continue
        if a.get("customCategory") and region not in a["customCategory"]:
            continue
        if "isManager" in a:
            name += " if a manager" if a["isManager"] else " if not a manager"
        out.append(name)
    return out


def _job_courses(auds, row) -> list[str]:
    """Role-based courses a job leads to: campaigns that key only on manager and department."""
    out = []
    dept = PROFILE_DEPARTMENT.get(row["job"])
    for name, a in auds:
        if not a or not a.keys() <= {"isManager", "department"}:
            continue
        if "isManager" in a and row.get("isManager") is not a["isManager"]:
            continue
        if "department" in a and dept not in a["department"]:
            continue
        out.append(name)
    return out


@router.get("/rules")
def get_rules(request: Request):
    b = _bundle(request)
    data = _rules_data(b)
    auds = _audiences(b)
    offices = [rules_mod.office_row(data, o) for o in LOCATIONS]
    jobs = [rules_mod.job_row(data, j, profile_descriptor(j)) for j in JOB_PROFILES]
    for r in offices:
        r["courses"] = _office_courses(auds, r)
    for r in jobs:
        r["courses"] = _job_courses(auds, r)
    return {"offices": offices, "jobs": jobs, "everyone": [n for n, a in auds if not a],
            "choices": rules_mod.CHOICES}


@router.put("/rules/office")
def put_office(body: OfficeIn, request: Request):
    ch = rules_mod.CHOICES
    if body.office not in LOCATIONS:
        return _err(422, f"unknown office {body.office!r}")
    country, region = body.country, body.region
    if country is not None and country not in _valid(ch["countries"]):
        return _err(422, f"unknown country {country!r}")
    if country is None:
        region = None
    elif region is not None and region not in _valid(ch["regions"][country]):
        return _err(422, f"region {region!r} does not belong to country {country}")
    b = _bundle(request)
    with WRITE_LOCK:
        data = _rules_data(b)
        rules_mod.set_office(data, body.office, country, region)
        rules_mod.save(b.rules_path, data)
    return rules_mod.office_row(data, body.office)


@router.put("/rules/job")
def put_job(body: JobIn, request: Request):
    if body.job not in JOB_PROFILES:
        return _err(422, f"unknown job {body.job!r}")
    b = _bundle(request)
    with WRITE_LOCK:
        data = _rules_data(b)
        rules_mod.set_job(data, body.job, body.isManager)
        rules_mod.save(b.rules_path, data)
    return rules_mod.job_row(data, body.job, profile_descriptor(body.job))
