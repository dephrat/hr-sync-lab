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


def test_post_job_change_applies_to_worker(client):
    worker = client.get("/workers/W-1001").json()
    resp = client.post("/jobChanges", json={
        "workerId": "W-1001",
        "effectiveDate": "2026-10-01",
        "reason": "Promotion",
        "jobProfile": "Sales_Team_Lead",
    })
    assert resp.status_code == 201
    change = resp.json()
    assert change["completedDate"]
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
    assert client.post("/jobChanges", json={
        "workerId": "W-1001", "effectiveDate": "2026-10-01", "reason": "Termination"}).status_code == 201
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
