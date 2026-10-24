"""Mock Workday staffing API (FastAPI)."""
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from hris.db import (DEPARTMENTS, JOB_PROFILES, LOCATIONS, apply_job_change, connect,
                     location_id, profile_descriptor, utc_now)


class JobChangeIn(BaseModel):
    workerId: str
    effectiveDate: str
    reason: Literal["Promotion", "Transfer", "Hire", "Termination", "Data_Change"]
    jobProfile: str | None = None
    location: str | None = None
    businessTitle: str | None = None
    supervisoryOrganization: str | None = None
    completedDate: str | None = None  # test hook: pin the completion timestamp


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

    @app.get("/workers/{worker_id}")
    def get_worker(worker_id: str, conn=Depends(get_conn)):
        r = conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "worker not found")
        return worker_json(r)

    @app.get("/jobChanges")
    def list_job_changes(completedFrom: str | None = None,
                         limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
                         conn=Depends(get_conn)):
        where, args = "", []
        if completedFrom:
            where, args = " WHERE j.completed_date >= ?", [completedFrom]
        total = conn.execute(f"SELECT COUNT(*) FROM job_changes j{where}", args).fetchone()[0]
        rows = conn.execute(f"{_JC_SELECT}{where} ORDER BY j.completed_date, j.seq LIMIT ? OFFSET ?",
                            [*args, limit, offset]).fetchall()
        return {"total": total, "data": [job_change_json(r, r["worker_name"]) for r in rows]}

    @app.get("/jobChanges/{change_id}")
    def get_job_change(change_id: str, conn=Depends(get_conn)):
        try:
            seq = int(change_id.removeprefix("JC-"))
        except ValueError:
            raise HTTPException(404, "job change not found")
        r = conn.execute(f"{_JC_SELECT} WHERE j.seq=?", (seq,)).fetchone()
        if r is None:
            raise HTTPException(404, "job change not found")
        return job_change_json(r, r["worker_name"])

    @app.post("/jobChanges", status_code=201)
    def post_job_change(body: JobChangeIn, conn=Depends(get_conn)):
        """Mock simplification of Workday's initiate/submit/approve flow: the change is
        applied to the worker immediately and completedDate is set to now (or the
        optional completedDate supplied by the caller)."""
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
        seq = apply_job_change(
            conn, worker_id=body.workerId, effective_date=body.effectiveDate,
            reason=body.reason, completed_date=body.completedDate or utc_now(),
            job_profile=body.jobProfile, location=body.location,
            business_title=body.businessTitle, department=body.supervisoryOrganization)
        r = conn.execute(f"{_JC_SELECT} WHERE j.seq=?", (seq,)).fetchone()
        return job_change_json(r, r["worker_name"])

    return app
