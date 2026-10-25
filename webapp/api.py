"""JSON endpoints for the demo page. Every write goes through the sync engine or store."""
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool

from hrsync import engine, store
from hrsync import rules as rules_mod
from ethena import db as ethena_db
from ethena.db import normalize_audience
from hris.db import DEPARTMENTS, JOB_PROFILES, LOCATIONS, profile_descriptor
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
    # ENROLLMENT_MISSING is no longer raised (Ethena decides who gets which course); a
    # legacy row shows its code, which the .get() fallbacks below allow.
}


REASON_LABEL: dict[str, str] = {
    "UNKNOWN_LOCATION": "Courses not specified for this office",
    "UNKNOWN_PROFILE": "Courses not specified for this job",
    "AMBIGUOUS_LEARNER": "Two Ethena accounts share this email",
    "NO_LEARNER_MATCH": "No Ethena account for this person",
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


_REGION_NAMES = {r["value"]: r["label"] for rs in rules_mod.CHOICES["regions"].values() for r in rs}
_COUNTRY_NAMES = {"CAN": "Canada", "USA": "USA"}


def _phrase(detail: str) -> str:
    """Turn the patch diff (a JSON dict of changed attributes) into plain words a person
    in HR would use: what happened to the employee, not which field changed."""
    try:
        diff = dict(json.loads(detail))
    except (ValueError, TypeError):
        return detail
    parts = []
    if "isManager" in diff:
        parts.append("became a manager" if diff.pop("isManager") else "is no longer a manager")
    country = diff.pop("country", None)
    region = diff.pop("state", None) or diff.pop("customCategory", None)
    diff.pop("state", None), diff.pop("customCategory", None)
    if country or region:
        where = ", ".join(x for x in (_REGION_NAMES.get(region, region),
                                      _COUNTRY_NAMES.get(country, country)) if x)
        parts.append(f"now works in {where}")
    if diff.get("department"):
        parts.append(f"moved to the {diff.pop('department')} department")
    return ", ".join(parts) or "details updated"


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
    held = by.get("verify_enrollment", {}).get("detail", "")
    now_word = "now has" if outcome in ("created", "patched") else "has"
    trained = (f"Ethena {now_word} them in: {held}." if held
               else "Ethena has no courses for them.")
    if outcome == "skipped":
        return f"{head} Already handled in an earlier run."
    if outcome == "failed":
        bad = next(r for r in rows if r["outcome"] == "failed")
        return f"{head} Could not finish: Ethena or Workday did not answer ({bad['detail']}). The next run retries."
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
    return _narrate(request, summary.to_dict())


@router.get("/run/last")
def last_run(request: Request):
    """The most recent run, in the same shape POST /run returns, so the page can show it
    again after a reload. {"summary": null, "changes": []} before the first run."""
    with _Store(request) as conn:
        last = store.last_run(conn)
    if not last:
        return {"summary": None, "changes": [], "notes": []}
    return _narrate(request, last)


def _narrate(request: Request, summary: dict) -> dict:
    b = _bundle(request)
    with _Store(request) as conn:
        cur = conn.execute("SELECT * FROM audit WHERE run_id=? ORDER BY id", (summary["run_id"],))
        cols = [c[0] for c in cur.description]
        grouped: dict[str, list[dict]] = {}
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            grouped.setdefault(row["change_id"], []).append(row)
    names: dict = {}
    changes = []
    for change_id, rows in grouped.items():
        wid = rows[0]["worker_id"]
        outcome = _outcome(rows)
        if change_id.startswith(engine.RECON_PREFIX):
            reason = "Missing from Ethena" if outcome == "created" else "Spot check"
        else:
            try:
                reason = b.hris.get_job_change(change_id)["reason"]["descriptor"]
            except (HrSyncClientError, httpx.HTTPError):
                reason = "Job change"
        name = _name(b, names, wid)
        changes.append({
            "change_id": change_id, "worker_id": wid, "worker_name": name, "reason": reason,
            "outcome": outcome, "group": GROUP[outcome],
            "actions": [{"action": r["action"], "outcome": r["outcome"], "detail": r["detail"]}
                        for r in rows],
            "sentence": _sentence(name, wid, reason, outcome, rows)})
    notes = []
    n = summary.get("auto_resolved") or 0
    if n:
        notes.append(f"{n} manual-review item{'s' if n != 1 else ''} cleared automatically "
                     "because a course rule now covers them.")
    return {"summary": summary, "changes": changes, "notes": notes}


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
                return _err(404, f"There is no Ethena account {body.learner_id}.")
            return _err(502, str(exc))
        except (HrSyncClientError, httpx.HTTPError) as exc:
            return _err(502, str(exc))


@router.post("/queue/{item_id}/skip")
def skip(item_id: int, body: SkipIn, request: Request):
    at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with WRITE_LOCK, _Store(request) as conn:
        if not store.skip_item(conn, item_id, body.note, at):
            return _err(404, f"That item was already dealt with.")
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


def _group_text(g: dict) -> str:
    # The stored rule as field = value, unreworded, so what the page shows is the data.
    parts = []
    for key, val in g.items():
        if isinstance(val, list):
            val = ", ".join(map(str, val))
        elif isinstance(val, bool):
            val = "true" if val else "false"
        parts.append(f"{key} = {val}")
    return " \u00b7 ".join(parts) or "everyone (no rules)"


def _audience_text(aud) -> str:
    groups = normalize_audience(aud)
    if not groups:
        return "nobody (no groups)"
    return " OR ".join(_group_text(g) for g in groups)


def _group_edit(g: dict) -> dict:
    """A stored group in the editable shape the page uses."""
    country = (g.get("country") or [None])[0]
    region = (g.get("customCategory") or g.get("state") or [None])[0]
    return {"country": country, "region": region, "isManager": g.get("isManager"),
            "department": (g.get("department") or [None])[0]}


def _read_audiences(path) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {cid: normalize_audience(json.loads(a)) for cid, a in
                conn.execute("SELECT id, audience FROM campaigns")}
    finally:
        conn.close()


def _campaign_view(c: dict, groups: list[dict]) -> dict:
    return {"id": c["id"], "campaignName": c["campaignName"], "courseName": c["courseName"],
            "audience_text": _audience_text(groups), "groups": [_group_edit(g) for g in groups]}


@router.get("/campaigns")
def campaigns(request: Request):
    b = _bundle(request)
    # The Ethena API does not expose a campaign's audience. This reads the mock's sqlite file
    # directly, only to display it; the sync itself never does this.
    auds = _read_audiences(b.settings.ethena_db)
    return [_campaign_view(c, auds.get(c["id"], [{}])) for c in b.learning.list_campaigns()]


class GroupIn(BaseModel):
    country: str | None = None
    region: str | None = None
    isManager: bool | None = None
    department: str | None = None


class AudienceIn(BaseModel):
    groups: list[GroupIn]


@router.put("/campaigns/{campaign_id}/audience")
def put_audience(body: AudienceIn, request: Request,
                 campaign_id: str = Path(pattern=r"^[A-Za-z0-9]+$")):
    ch = rules_mod.CHOICES
    stored = []
    for g in body.groups:
        if g.country is not None and g.country not in _valid(ch["countries"]):
            return _err(422, f"unknown country {g.country!r}")
        if g.region is not None:
            if g.country is None:
                return _err(422, "a province or state needs a country")
            if g.region not in _valid(ch["regions"][g.country]):
                return _err(422, f"region {g.region!r} does not belong to country {g.country}")
        if g.department is not None and g.department not in DEPARTMENTS:
            return _err(422, f"unknown department {g.department!r}")
        out = {}
        if g.country:
            out["country"] = [g.country]
        if g.region:
            out["state" if g.country == "USA" else "customCategory"] = [g.region]
        if g.isManager is not None:
            out["isManager"] = g.isManager
        if g.department:
            out["department"] = [g.department]
        stored.append(out)
    b = _bundle(request)
    # This edits the mock's store directly: real Ethena audiences are set in its admin UI,
    # not through its API, so there is no API call to make. Ethena's audience engine then
    # enrolls matching people itself and removes unfinished courses from people who no longer
    # match (SYSTEM_REMOVED; the real trigger is an assumption). enroll() does both here.
    with WRITE_LOCK:
        conn = ethena_db.connect(b.settings.ethena_db)
        try:
            if conn.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone() is None:
                return _err(404, "unknown campaign")
            conn.execute("UPDATE campaigns SET audience=? WHERE id=?",
                         (json.dumps(stored), campaign_id))
            conn.commit()
            for (lid,) in conn.execute("SELECT id FROM learners WHERE status='ACTIVE'").fetchall():
                ethena_db.enroll(conn, lid)
        finally:
            conn.close()
    c = next((c for c in b.learning.list_campaigns() if c["id"] == campaign_id), None)
    if c is None:
        return _err(404, "unknown campaign")
    return _campaign_view(c, stored)


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
    rows = b.learning.list_learners()
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
                kind, sentence = "ambiguous", (f"{name} has two Ethena accounts with the same "
                                               "email. A person has to pick the right one.")
            elif found:
                learner = found[0]
            elif not terminated:
                kind, sentence = "no_learner", f"{name} is in Workday but has no Ethena account yet."
            else:
                continue
        if kind is None and terminated:
            if learner.get("status") != "ACTIVE":
                continue
            kind = "terminated"
            sentence = f"In Workday, {name} left the company. Ethena still has them active."
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
                    # The difference may come from Workday or from a course-rule edit, so
                    # the sentence does not say which.
                    kind = "attrs"
                    sentence = f"{name} needs updating in Ethena: {_phrase(json.dumps(diff))}."
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
    isManager: StrictBool | None = None


@router.get("/rules")
def get_rules(request: Request):
    b = _bundle(request)
    data = _rules_data(b)
    offices = [rules_mod.office_row(data, o) for o in LOCATIONS]
    jobs = [rules_mod.job_row(data, j, profile_descriptor(j)) for j in JOB_PROFILES]
    return {"offices": offices, "jobs": jobs,
            "choices": rules_mod.CHOICES, "departments": DEPARTMENTS}


@router.put("/rules/office")
def put_office(body: OfficeIn, request: Request):
    ch = rules_mod.CHOICES
    if body.office not in LOCATIONS:
        return _err(422, f"unknown office {body.office!r}")
    country, region = body.country, body.region
    if country is not None and country not in _valid(ch["countries"]):
        return _err(422, f"unknown country {country!r}")
    if country is None:
        if region is not None:
            return _err(422, "a province or state needs a country")
    elif region is None:
        return _err(422, "pick a province or state")
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
