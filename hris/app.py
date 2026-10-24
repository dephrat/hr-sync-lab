"""Mock Workday staffing API (FastAPI)."""
import re
from datetime import date
from typing import Literal

from fastapi.responses import JSONResponse
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from hris.db import (DEPARTMENTS, JOB_PROFILES, LOCATIONS, PROFILE_DEPARTMENT, apply_job_change,
                     approve_change, create_pending_change,
                     connect, is_manager_profile, location_id, profile_descriptor, utc_now)


class JobChangeIn(BaseModel):
    workerId: str
    effectiveDate: str
    reason: Literal["Promotion", "Transfer", "Hire", "Termination", "Data_Change"]
    jobProfile: str | None = None
    location: str | None = None
    businessTitle: str | None = None
    supervisoryOrganization: str | None = None
    completedDate: str | None = None  # test hook: pin the completion timestamp


class ApproveIn(BaseModel):
    # Test-only hook: pins the completion timestamp so tests control the watermark.
    # Real approvals omit it and get the server clock.
    completedDate: str | None = None


class HireIn(BaseModel):
    name: str
    jobProfile: str
    location: str
    businessTitle: str | None = None


def _ref(id_, descriptor):
    return {"id": id_, "descriptor": descriptor}


def worker_json(r) -> dict:
    return {
        "id": r["id"],
        "descriptor": r["descriptor"],
        "href": f"/workers/{r['id']}",
        "primaryWorkEmail": r["email"],
        "businessTitle": r["business_title"],
        "isManager": bool(r["is_manager"]),
        "hireDate": r["hire_date"],
        "jobProfile": _ref(r["job_profile"], profile_descriptor(r["job_profile"])),
        "location": _ref(location_id(r["location"]), r["location"]),
        "primarySupervisoryOrganization": _ref(r["department"], r["department"]),
        "primaryPosition": _ref(r["position_id"], r["business_title"]),
        "workerStatus": {"active": bool(r["active"]), "terminated": bool(r["terminated"]),
                         "statusDate": r["status_date"]},
    }


def job_change_json(r, worker_name) -> dict:
    jc_id = f"JC-{r['seq']:04d}"
    out = {
        "id": jc_id,
        "descriptor": f"{r['reason'].replace('_', ' ')}: {worker_name}",
        "href": f"/jobChanges/{jc_id}",
        "worker": _ref(r["worker_id"], worker_name),
        "effectiveDate": r["effective_date"],
        "completedDate": r["completed_date"],
        "status": r["status"].upper(),
        "reason": _ref(r["reason"], r["reason"].replace("_", " ")),
    }
    if r["job_profile"]:
        out["jobProfile"] = _ref(r["job_profile"], profile_descriptor(r["job_profile"]))
    if r["location"]:
        out["location"] = _ref(location_id(r["location"]), r["location"])
    if r["business_title"]:
        out["businessTitle"] = r["business_title"]
    if r["department"]:
        out["supervisoryOrganization"] = _ref(r["department"], r["department"])
    return out


_JC_SELECT = ("SELECT j.*, w.descriptor AS worker_name FROM job_changes j"
              " JOIN workers w ON w.id = j.worker_id")


def create_app(db_path) -> FastAPI:
    app = FastAPI(title="Mock Workday", docs_url=None, redoc_url=None, openapi_url=None)

    def get_conn():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/workers")
    def list_workers(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
                     conn=Depends(get_conn)):
        total = conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
        rows = conn.execute("SELECT * FROM workers ORDER BY id LIMIT ? OFFSET ?",
                            (limit, offset)).fetchall()
        return {"total": total, "data": [worker_json(r) for r in rows]}

    @app.post("/workers", status_code=201)
    def hire_worker(body: HireIn, conn=Depends(get_conn)):
        """Workday owns people: creates an active worker and a completed Hire job change."""
        name = " ".join(body.name.split())
        if not 2 <= len(name) <= 80 or len(name.split()) < 2:
            raise HTTPException(422, "name must be 2 to 80 characters with at least two words")
        if body.jobProfile not in JOB_PROFILES:
            raise HTTPException(422, f"unknown jobProfile {body.jobProfile}")
        if body.location not in LOCATIONS:
            raise HTTPException(422, f"unknown location {body.location}")
        title = None
        if body.businessTitle is not None:
            title = " ".join(body.businessTitle.split())
            if not 2 <= len(title) <= 80:
                raise HTTPException(422, "businessTitle must be 2 to 80 characters")
        top = conn.execute("SELECT MAX(CAST(SUBSTR(id, 3) AS INTEGER)) FROM workers").fetchone()[0]
        num = (top or 1000) + 1
        wid = f"W-{num:04d}"
        words = [re.sub(r"[^a-z]", "", w.lower()) for w in name.split()]
        words = [w for w in words if w]
        if len(words) < 2:
            raise HTTPException(422, "name must contain letters in at least two words")
        email = f"{words[0]}.{words[-1]}.{num}@example.com"
        today = date.today().isoformat()
        profile = body.jobProfile
        conn.execute(
            "INSERT INTO workers (id, descriptor, email, business_title, is_manager, hire_date,"
            " job_profile, location, department, position_id, status_date)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wid, name, email, title or profile_descriptor(profile), int(is_manager_profile(profile)),
             today, profile, body.location, PROFILE_DEPARTMENT[profile], f"P-{num}", today))
        apply_job_change(conn, worker_id=wid, effective_date=today, reason="Hire",
                         completed_date=utc_now(), job_profile=profile, location=body.location,
                         business_title=title,
                         department=PROFILE_DEPARTMENT[profile])
        r = conn.execute("SELECT * FROM workers WHERE id=?", (wid,)).fetchone()
        return JSONResponse(worker_json(r), status_code=201)

    @app.get("/workers/{worker_id}")
    def get_worker(worker_id: str, conn=Depends(get_conn)):
        r = conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "worker not found")
        return worker_json(r)

    @app.get("/jobChanges")
    def list_job_changes(completedFrom: str | None = None,
                         status: Literal["pending", "completed", "PENDING", "COMPLETED"] | None = None,
                         limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
                         conn=Depends(get_conn)):
        conds, args = [], []
        if completedFrom:  # pending rows have NULL completed_date, so this excludes them
            conds.append("j.completed_date >= ?")
            args.append(completedFrom)
        if status:
            conds.append("j.status = ?")
            args.append(status.lower())
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = conn.execute(f"SELECT COUNT(*) FROM job_changes j{where}", args).fetchone()[0]
        rows = conn.execute(f"{_JC_SELECT}{where} ORDER BY j.completed_date, j.seq LIMIT ? OFFSET ?",
                            [*args, limit, offset]).fetchall()
        return {"total": total, "data": [job_change_json(r, r["worker_name"]) for r in rows]}

    def find_change(conn, change_id):
        try:
            seq = int(change_id.removeprefix("JC-"))
        except ValueError:
            raise HTTPException(404, "job change not found")
        r = conn.execute(f"{_JC_SELECT} WHERE j.seq=?", (seq,)).fetchone()
        if r is None:
            raise HTTPException(404, "job change not found")
        return r

    @app.get("/jobChanges/{change_id}")
    def get_job_change(change_id: str, conn=Depends(get_conn)):
        r = find_change(conn, change_id)
        return job_change_json(r, r["worker_name"])

    @app.post("/jobChanges/{change_id}/approve")
    def approve_job_change(change_id: str, body: ApproveIn | None = None,
                           conn=Depends(get_conn)):
        """Approve a pending change: it takes effect on the worker and gets a completedDate."""
        r = find_change(conn, change_id)
        if r["status"] != "pending":
            raise HTTPException(409, "job change is already completed")
        w = conn.execute("SELECT * FROM workers WHERE id=?", (r["worker_id"],)).fetchone()
        if w["terminated"]:
            raise HTTPException(422, "worker has left the company; no further job changes")
        if r["reason"] != "Termination" and not any([
                r["job_profile"] and r["job_profile"] != w["job_profile"],
                r["location"] and r["location"] != w["location"],
                r["business_title"] and r["business_title"] != w["business_title"],
                r["department"] and r["department"] != w["department"]]):
            raise HTTPException(422, "change no longer changes anything")
        approve_change(conn, r["seq"], (body and body.completedDate) or utc_now())
        r = find_change(conn, change_id)
        return job_change_json(r, r["worker_name"])

    @app.post("/jobChanges/{change_id}/deny")
    def deny_job_change(change_id: str, conn=Depends(get_conn)):
        """Deny a pending change: it is deleted and the worker is untouched."""
        r = find_change(conn, change_id)
        if r["status"] != "pending":
            raise HTTPException(409, "job change is already completed")
        conn.execute("DELETE FROM job_changes WHERE seq=?", (r["seq"],))
        conn.commit()
        return {"id": f"JC-{r['seq']:04d}", "status": "DENIED"}

    @app.post("/jobChanges", status_code=201)
    def post_job_change(body: JobChangeIn, conn=Depends(get_conn)):
        """Submit a job change for approval. It is stored as PENDING with no completedDate and
        does not touch the worker until POST /jobChanges/{id}/approve. Test hook: a body with
        completedDate skips approval and completes the change at that timestamp."""
        cur_w = conn.execute("SELECT * FROM workers WHERE id=?", (body.workerId,)).fetchone()
        if cur_w is None:
            raise HTTPException(404, "worker not found")
        if cur_w["terminated"]:
            raise HTTPException(422, "worker has left the company; no further job changes")
        if body.jobProfile is not None and body.jobProfile not in JOB_PROFILES:
            raise HTTPException(422, f"unknown jobProfile {body.jobProfile}")
        if body.location is not None and body.location not in LOCATIONS:
            raise HTTPException(422, f"unknown location {body.location}")
        if body.supervisoryOrganization is not None and body.supervisoryOrganization not in DEPARTMENTS:
            raise HTTPException(422, f"unknown supervisoryOrganization {body.supervisoryOrganization}")
        if body.reason != "Termination":
            changes = [
                body.jobProfile is not None and body.jobProfile != cur_w["job_profile"],
                body.location is not None and body.location != cur_w["location"],
                body.businessTitle is not None and body.businessTitle != cur_w["business_title"],
                body.supervisoryOrganization is not None
                and body.supervisoryOrganization != cur_w["department"]]
            if not any(changes):
                raise HTTPException(422, "a job change must change the job profile, location, "
                                         "title or organization, unless the reason is Termination")
        fields = dict(worker_id=body.workerId, effective_date=body.effectiveDate,
                      reason=body.reason, job_profile=body.jobProfile, location=body.location,
                      business_title=body.businessTitle, department=body.supervisoryOrganization)
        if body.completedDate:
            seq = apply_job_change(conn, completed_date=body.completedDate, **fields)
        else:
            seq = create_pending_change(conn, **fields)
        r = conn.execute(f"{_JC_SELECT} WHERE j.seq=?", (seq,)).fetchone()
        return job_change_json(r, r["worker_name"])

    return app
