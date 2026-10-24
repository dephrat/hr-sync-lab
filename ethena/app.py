"""Mock Ethena API (FastAPI), all under /v1 with HTTP Basic (any username, password = api_key).

Enrollment rule (mock-internal): after POST/PATCH /learners, for each ACTIVE campaign whose
audience matches the learner, a learner-training-campaign is ensured (enrollmentStatus ACTIVE,
completionStatus WITHIN_DEADLINE, deadline = now + deadlineInDays). Campaigns that no longer
match are left in place (Ethena does not auto-unenroll). TERMINATED learners get no new
enrollments. The rule lives in ethena.db.enroll, used here as _enroll.
"""
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from ethena.db import connect
from ethena.db import enroll as _enroll


class LearnerIn(BaseModel):
    name: str | None = None
    email: str | None = None
    status: Literal["ACTIVE", "DEACTIVATED", "TERMINATED"] | None = None
    country: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    state: str | None = Field(None, pattern=r"^[A-Z]{2}$")
    isManager: bool | None = None
    managerLearnerId: str | None = None
    department: str | None = None
    workerType: str | None = None
    customCategory: str | None = None
    language: str | None = None
    trainingUrl: str | None = None


class LearnerPatch(LearnerIn):
    id: str  # Ethena quirk: PATCH body must carry the id
    status: Literal["ACTIVE", "TERMINATED"] | None = None  # PATCH cannot set DEACTIVATED


_FIELDS = {"name": "name", "email": "email", "status": "status", "country": "country",
           "state": "state", "isManager": "is_manager", "managerLearnerId": "manager_learner_id",
           "department": "department", "workerType": "worker_type",
           "customCategory": "custom_category", "language": "language",
           "trainingUrl": "training_url"}


def learner_json(r) -> dict:
    return {"id": r["id"], "name": r["name"], "email": r["email"], "status": r["status"],
            "country": r["country"], "state": r["state"], "isManager": bool(r["is_manager"]),
            "managerLearnerId": r["manager_learner_id"], "department": r["department"],
            "workerType": r["worker_type"], "customCategory": r["custom_category"],
            "language": r["language"], "trainingUrl": r["training_url"]}


def campaign_json(r) -> dict:
    return {"id": r["id"], "courseName": r["course_name"], "campaignName": r["campaign_name"],
            "status": r["status"], "initialTrainingAt": r["initial_training_at"],
            "nextTrainingAt": r["next_training_at"], "deadlineInDays": r["deadline_in_days"]}


def enrollment_json(r) -> dict:
    return {"id": r["id"], "learnerId": r["learner_id"], "trainingCampaignId": r["campaign_id"],
            "enrollmentStatus": r["enrollment_status"], "completionStatus": r["completion_status"],
            "deadline": r["deadline"]}


def module_json(r) -> dict:
    return {"id": r["id"], "learnerId": r["learner_id"], "learnerTrainingCampaignId": r["enrollment_id"],
            "moduleName": r["module_name"], "lengthInMinutes": r["length_in_minutes"],
            "status": "USER_COMPLETED" if r["completion_status"] == "COMPLETED" else r["completion_status"],
            "assignedAt": r["assigned_at"]}


_NOT_NULL = {"name", "email", "status", "isManager", "language"}


def _error(status, title, detail):
    return JSONResponse({"title": title, "detail": detail}, status_code=status)


def create_app(db_path, api_key="test-key") -> FastAPI:
    app = FastAPI(title="Mock Ethena", docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic(auto_error=False)

    def get_conn():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def auth(creds: HTTPBasicCredentials | None = Depends(basic)):
        if creds is None or not secrets.compare_digest(creds.password.encode(), api_key.encode()):
            raise HTTPException(401, "invalid or missing credentials")

    @app.exception_handler(HTTPException)
    async def http_exc(_: Request, exc: HTTPException):
        titles = {401: "Unauthorized", 404: "Not Found"}
        return _error(exc.status_code, titles.get(exc.status_code, "Error"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_exc(_: Request, exc: RequestValidationError):
        return _error(422, "Validation Error", str(exc.errors()))

    r = APIRouter(prefix="/v1", dependencies=[Depends(auth)])

    def page(conn, table, where, args, limit, cursor, to_json, select="*", alias=""):
        """Envelope {limit, hasMore, data}; clients continue from data[-1]["id"]."""
        if cursor:
            where, args = [*where, f"{alias}id > ?"], [*args, cursor]
        sql = f"SELECT {select} FROM {table}" + (" WHERE " + " AND ".join(where) if where else "")
        rows = conn.execute(sql + f" ORDER BY {alias}id LIMIT ?", [*args, limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"limit": limit, "hasMore": more, "data": [to_json(x) for x in rows]}

    def one(conn, table, id_, label, to_json):
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (id_,)).fetchone()
        if row is None:
            raise HTTPException(404, f"{label} {id_} not found")
        return to_json(row)

    @r.get("/learners")
    def list_learners(email: str | None = None, status: str | None = None,
                      limit: int = Query(25, ge=1, le=100), cursor: str | None = None,
                      conn=Depends(get_conn)):
        where, args = [], []
        if email:
            where.append("email = ?"); args.append(email)
        if status:
            where.append("status = ?"); args.append(status)
        return page(conn, "learners", where, args, limit, cursor, learner_json)

    @r.post("/learners")
    def create_learner(body: LearnerIn, conn=Depends(get_conn)):
        if body.state is not None and body.country != "USA":
            raise HTTPException(422, "state is only allowed when country is USA")
        n = conn.execute("SELECT COUNT(*) FROM learners").fetchone()[0] + 100001
        while conn.execute("SELECT 1 FROM learners WHERE id=?", (f"L{n:06d}",)).fetchone():
            n += 1
        lid = f"L{n:06d}"
        data = {col: getattr(body, f) for f, col in _FIELDS.items()}
        data.update(id=lid, name=data["name"] or "", email=data["email"] or "",
                    status=data["status"] or "ACTIVE", is_manager=int(bool(data["is_manager"])),
                    language=data["language"] or "en")
        cols = ", ".join(data)
        conn.execute(f"INSERT INTO learners ({cols}) VALUES ({', '.join('?' * len(data))})",
                     list(data.values()))
        conn.commit()
        _enroll(conn, lid)
        return {"id": lid}

    @r.get("/learners/{learner_id}")
    def get_learner(learner_id: str, conn=Depends(get_conn)):
        return one(conn, "learners", learner_id, "learner", learner_json)

    @r.patch("/learners/{learner_id}", status_code=204)
    def patch_learner(learner_id: str, body: LearnerPatch, conn=Depends(get_conn)):
        cur = one(conn, "learners", learner_id, "learner", learner_json)
        sent = body.model_dump(exclude_unset=True, exclude={"id"})
        nulls = sorted(f for f in sent if sent[f] is None and f in _NOT_NULL)
        if nulls:
            raise HTTPException(422, f"fields cannot be null: {', '.join(nulls)}")
        country = sent.get("country", cur["country"])
        if "country" in sent and country != "USA" and "state" not in sent:
            sent["state"] = None  # state is USA-only
        if sent.get("state") is not None and country != "USA":
            raise HTTPException(422, "state is only allowed when country is USA")
        for f, v in sent.items():
            conn.execute(f"UPDATE learners SET {_FIELDS[f]}=? WHERE id=?",
                         (int(v) if f == "isManager" else v, learner_id))
        conn.commit()
        _enroll(conn, learner_id)
        return Response(status_code=204)

    @r.delete("/learners/{learner_id}", status_code=204)
    def delete_learner(learner_id: str, conn=Depends(get_conn)):
        one(conn, "learners", learner_id, "learner", learner_json)
        conn.execute("DELETE FROM modules WHERE learner_id=?", (learner_id,))
        conn.execute("DELETE FROM enrollments WHERE learner_id=?", (learner_id,))
        conn.execute("DELETE FROM learners WHERE id=?", (learner_id,))
        conn.commit()
        return Response(status_code=204)

    @r.get("/training-campaigns")
    def list_campaigns(status: str | None = None, limit: int = Query(25, ge=1, le=100),
                       cursor: str | None = None, conn=Depends(get_conn)):
        where, args = ([ "status = ?"], [status]) if status else ([], [])
        return page(conn, "campaigns", where, args, limit, cursor, campaign_json)

    @r.get("/training-campaigns/{campaign_id}")
    def get_campaign(campaign_id: str, conn=Depends(get_conn)):
        return one(conn, "campaigns", campaign_id, "training campaign", campaign_json)

    @r.get("/learner-training-campaigns")
    def list_enrollments(learnerId: str | None = None, trainingCampaignId: str | None = None,
                         completionStatus: str | None = None,
                         limit: int = Query(25, ge=1, le=100), cursor: str | None = None,
                         conn=Depends(get_conn)):
        where, args = [], []
        for val, col in ((learnerId, "learner_id"), (trainingCampaignId, "campaign_id"),
                         (completionStatus, "completion_status")):
            if val:
                where.append(f"{col} = ?"); args.append(val)
        return page(conn, "enrollments", where, args, limit, cursor, enrollment_json)

    @r.get("/learner-training-campaigns/{ltc_id}")
    def get_enrollment(ltc_id: str, conn=Depends(get_conn)):
        return one(conn, "enrollments", ltc_id, "learner training campaign", enrollment_json)

    @r.post("/learner-training-campaigns/{ltc_id}/complete", status_code=204)
    def complete_enrollment(ltc_id: str, conn=Depends(get_conn)):
        """Ethena owns completions: marks the enrollment COMPLETED (its module then reads
        USER_COMPLETED). The sync never writes this, so the two cannot conflict. Idempotent."""
        one(conn, "enrollments", ltc_id, "learner training campaign", enrollment_json)
        conn.execute("UPDATE enrollments SET completion_status='COMPLETED' WHERE id=?", (ltc_id,))
        conn.commit()
        return Response(status_code=204)

    @r.get("/learner-training-modules")
    def list_modules(learnerId: str | None = None, limit: int = Query(25, ge=1, le=100),
                     cursor: str | None = None, conn=Depends(get_conn)):
        where, args = (["m.learner_id = ?"], [learnerId]) if learnerId else ([], [])
        return page(conn, "modules m JOIN enrollments e ON e.id = m.enrollment_id", where, args,
                    limit, cursor, module_json, select="m.*, e.completion_status", alias="m.")

    app.include_router(r)
    return app
