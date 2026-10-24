from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from hris.app import create_app
from hris.db import connect
from hris.seed import seed


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "hris.db"
    conn = connect(path)
    seed(conn)
    conn.close()
    return TestClient(create_app(path))


def test_list_workers_envelope(client):
    body = client.get("/workers").json()
    assert body["total"] == 100
    assert len(body["data"]) == 25
    for key in ("id", "descriptor", "href", "primaryWorkEmail", "businessTitle",
                "isManager", "jobProfile", "location", "workerStatus"):
        assert key in body["data"][0]


def test_get_worker_404(client):
    assert client.get("/workers/W-9999").status_code == 404


def test_job_changes_since_filters_and_orders(client):
    everything = client.get("/jobChanges", params={"limit": 100}).json()
    assert everything["total"] == 12
    cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = client.get("/jobChanges", params={"completedFrom": cutoff}).json()
    assert 0 < recent["total"] < 12
    dates = [c["completedDate"] for c in recent["data"]]
    assert dates == sorted(dates)
    assert all(d >= cutoff for d in dates)


def test_post_job_change_applies_to_worker_after_approval(client):
    worker = client.get("/workers/W-1001").json()
    resp = client.post("/jobChanges", json={
        "workerId": "W-1001",
        "effectiveDate": "2026-10-01",
        "reason": "Promotion",
        "jobProfile": "Sales_Team_Lead",
    })
    assert resp.status_code == 201
    pending = resp.json()
    assert pending["status"] == "PENDING" and pending["completedDate"] is None
    assert client.get("/workers/W-1001").json() == worker
    resp = client.post(f"/jobChanges/{pending['id']}/approve")
    assert resp.status_code == 200
    change = resp.json()
    assert change["status"] == "COMPLETED" and change["completedDate"]
    assert change["worker"]["id"] == "W-1001"
    assert client.get(f"/jobChanges/{change['id']}").json() == change
    after = client.get("/workers/W-1001").json()
    assert after["jobProfile"]["id"] == "Sales_Team_Lead"
    assert after["isManager"] is True
    assert after["location"] == worker["location"]


def test_post_job_change_unknown_worker_404(client):
    resp = client.post("/jobChanges", json={
        "workerId": "W-9999", "effectiveDate": "2026-10-01", "reason": "Transfer"})
    assert resp.status_code == 404


def test_post_job_change_accepts_completed_date(client):
    resp = client.post("/jobChanges", json={
        "workerId": "W-1002", "effectiveDate": "2026-10-01", "reason": "Data_Change",
        "businessTitle": "Senior Thing", "completedDate": "2026-10-02T03:04:05Z"})
    assert resp.json()["completedDate"] == "2026-10-02T03:04:05Z"
    assert client.get("/workers/W-1002").json()["businessTitle"] == "Senior Thing"


def test_seed_is_deterministic(tmp_path):
    def names(name):
        conn = connect(tmp_path / name)
        seed(conn)
        return [r["descriptor"] for r in conn.execute("SELECT descriptor FROM workers ORDER BY id")]
    assert names("a.db") == names("b.db")


def test_terminated_worker_rejects_change(client):
    t = client.post("/jobChanges", json={
        "workerId": "W-1001", "effectiveDate": "2026-10-01", "reason": "Termination"})
    assert t.status_code == 201
    assert client.post(f"/jobChanges/{t.json()['id']}/approve").status_code == 200
    r = client.post("/jobChanges", json={
        "workerId": "W-1001", "effectiveDate": "2026-10-02", "reason": "Transfer",
        "location": "Toronto, ON"})
    assert r.status_code == 422 and "left the company" in r.json()["detail"]


def test_noop_change_rejected_unless_termination(client):
    w = client.get("/workers/W-1002").json()
    for body in ({}, {"jobProfile": w["jobProfile"]["id"], "location": w["location"]["descriptor"],
                      "businessTitle": w["businessTitle"]}):
        r = client.post("/jobChanges", json={"workerId": "W-1002", "effectiveDate": "2026-10-01",
                                             "reason": "Data_Change", **body})
        assert r.status_code == 422 and "must change" in r.json()["detail"]
    assert client.post("/jobChanges", json={"workerId": "W-1002", "effectiveDate": "2026-10-01",
                                            "reason": "Termination"}).status_code == 201


def test_hire_creates_worker_and_hire_change(client):
    r = client.post("/workers", json={"name": "Ana  O'Brien-Lee", "jobProfile": "Sales_Team_Lead",
                                      "location": "Toronto, ON"})
    assert r.status_code == 201
    w = r.json()
    assert w["id"] == "W-1101" and w["descriptor"] == "Ana O'Brien-Lee"
    assert w["primaryWorkEmail"] == "ana.obrienlee.1101@example.com"
    assert w["isManager"] is True and w["workerStatus"]["active"] is True
    assert w["primarySupervisoryOrganization"]["id"] == "Sales"
    assert client.get("/workers/W-1101").status_code == 200
    changes = client.get("/jobChanges", params={"limit": 100}).json()["data"]
    mine = [c for c in changes if c["worker"]["id"] == "W-1101"]
    assert len(mine) == 1 and mine[0]["reason"]["id"] == "Hire"
    assert mine[0]["completedDate"]


def test_hire_rejects_bad_input(client):
    ok = {"name": "Ana Lee", "jobProfile": "Sales_Rep", "location": "Toronto, ON"}
    assert client.post("/workers", json={**ok, "jobProfile": "Astronaut"}).status_code == 422
    assert client.post("/workers", json={**ok, "location": "Mars"}).status_code == 422
    assert client.post("/workers", json={**ok, "name": "Madonna"}).status_code == 422
    assert client.get("/workers").json()["total"] == 100


def test_hire_business_title(client):
    base = {"name": "Ana Lee", "jobProfile": "Sales_Rep", "location": "Toronto, ON"}
    w = client.post("/workers", json={**base, "businessTitle": "Head of Vibes"}).json()
    assert w["businessTitle"] == "Head of Vibes"
    d = client.post("/workers", json={**base, "name": "Bo Lee"}).json()
    assert d["businessTitle"] == d["jobProfile"]["descriptor"]
    assert client.post("/workers", json={**base, "businessTitle": "x"}).status_code == 422


def _pending(client, wid="W-1003"):
    r = client.post("/jobChanges", json={"workerId": wid, "effectiveDate": "2026-10-01",
                                         "reason": "Transfer", "location": "Remote - Canada"})
    assert r.status_code == 201 and r.json()["status"] == "PENDING"
    return r.json()


def test_pending_change_invisible_to_completed_from(client):
    jc = _pending(client)
    recent = client.get("/jobChanges", params={"completedFrom": "2000-01-01", "limit": 100}).json()
    assert jc["id"] not in {c["id"] for c in recent["data"]}
    pend = client.get("/jobChanges", params={"status": "pending"}).json()
    assert [c["id"] for c in pend["data"]] == [jc["id"]]
    done = client.get("/jobChanges", params={"status": "completed", "limit": 100}).json()
    assert all(c["status"] == "COMPLETED" for c in done["data"])
    client.post(f"/jobChanges/{jc['id']}/approve", json={"completedDate": "2026-10-05T00:00:00Z"})
    recent = client.get("/jobChanges", params={"completedFrom": "2026-10-05T00:00:00Z"}).json()
    assert jc["id"] in {c["id"] for c in recent["data"]}
    assert client.get("/workers/W-1003").json()["location"]["descriptor"] == "Remote - Canada"


def test_approve_and_deny_conflicts(client):
    jc = _pending(client)
    assert client.post(f"/jobChanges/{jc['id']}/approve").status_code == 200
    assert client.post(f"/jobChanges/{jc['id']}/approve").status_code == 409
    assert client.post(f"/jobChanges/{jc['id']}/deny").status_code == 409
    assert client.post("/jobChanges/JC-9999/approve").status_code == 404
    assert client.post("/jobChanges/JC-9999/deny").status_code == 404


def test_deny_removes_pending_change(client):
    before = client.get("/workers/W-1004").json()
    jc = _pending(client, "W-1004")
    r = client.post(f"/jobChanges/{jc['id']}/deny")
    assert r.status_code == 200 and r.json()["status"] == "DENIED"
    assert client.get(f"/jobChanges/{jc['id']}").status_code == 404
    assert client.get("/workers/W-1004").json() == before


def test_approve_revalidates(client):
    jc = _pending(client, "W-1003")
    term = client.post("/jobChanges", json={"workerId": "W-1003", "effectiveDate": "2026-10-02",
                                            "reason": "Termination"}).json()
    assert client.post(f"/jobChanges/{term['id']}/approve").status_code == 200
    r = client.post(f"/jobChanges/{jc['id']}/approve")
    assert r.status_code == 422 and "left the company" in r.json()["detail"]
    assert client.post(f"/jobChanges/{jc['id']}/deny").status_code == 200


def test_approve_noop_change_rejected(client):
    a = _pending(client, "W-1006")
    b = client.post("/jobChanges", json={"workerId": "W-1006", "effectiveDate": "2026-10-03",
                                         "reason": "Transfer", "location": "Remote - Canada"})
    assert b.status_code == 201
    assert client.post(f"/jobChanges/{a['id']}/approve").status_code == 200
    r = client.post(f"/jobChanges/{b.json()['id']}/approve")
    assert r.status_code == 422 and "no longer changes anything" in r.json()["detail"]
    assert client.post(f"/jobChanges/{b.json()['id']}/deny").status_code == 200


def test_migration_finishes_interrupted_copy(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    raw = sqlite3.connect(p)
    raw.executescript(
        "CREATE TABLE job_changes_old (seq INTEGER PRIMARY KEY, worker_id TEXT, effective_date TEXT,"
        " completed_date TEXT, reason TEXT, job_profile TEXT, location TEXT, business_title TEXT,"
        " department TEXT);"
        "INSERT INTO job_changes_old VALUES (7,'W-1','2026-01-01','2026-01-02T00:00:00Z','Hire',"
        "NULL,NULL,NULL,NULL);")
    raw.commit()
    raw.close()
    conn = connect(p)
    assert conn.execute("SELECT seq, status FROM job_changes").fetchall()[0][:] == (7, "completed")
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='job_changes_old'").fetchone() is None
