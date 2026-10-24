import pytest
from starlette.testclient import TestClient

from hrsync.config import Settings
from hrsync.seeding import seed_all
from webapp.app import create_app


@pytest.fixture
def settings(tmp_path):
    s = Settings(hris_url="", ethena_url="", ethena_api_key="test-key",
                 sync_db=str(tmp_path / "sync.db"), hris_db=str(tmp_path / "hris.db"),
                 ethena_db=str(tmp_path / "ethena.db"),
                 rules_path="hrsync/rules.default.json")
    seed_all(s.hris_db, s.ethena_db)
    return s


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


BUCKETS = ("skipped", "queued", "created", "terminated", "patched", "ok", "failed")


def test_run_returns_narratives(client):
    body = client.post("/api/run").json()
    s = body["summary"]
    assert s["changes_seen"] == 12 and sum(s[b] for b in BUCKETS) == 12
    assert len(body["changes"]) == 12
    sentences = [c["sentence"] for c in body["changes"]]
    hit = [x for x in sentences if "W-1038" in x]
    assert hit and any("Manager Fundamentals" in x for x in hit)
    assert all("—" not in x for x in sentences)
    c = body["changes"][0]
    assert {"change_id", "worker_id", "worker_name", "reason", "outcome", "actions"} <= set(c)


def test_run_again_skips(client):
    client.post("/api/run")
    s = client.post("/api/run").json()["summary"]
    assert s["skipped"] == 1 or s["changes_seen"] == 1 and s["skipped"] == 1


def _queue(client):
    client.post("/api/run")
    return client.get("/api/queue").json()


def test_queue_has_help_and_candidates(client):
    items = _queue(client)
    amb = next(i for i in items if i["reason"] == "AMBIGUOUS_LEARNER")
    assert len(amb["candidates"]) == 2 and amb["reason_help"] and amb["worker_name"]
    assert set(amb["candidates"][0]) == {"id", "name", "email"}
    loc = next(i for i in items if i["reason"] == "UNKNOWN_LOCATION")
    assert loc["needs_rule"] is True and amb["needs_rule"] is False


def test_resolve_closes_item_and_reports_outcome(client):
    amb = next(i for i in _queue(client) if i["reason"] == "AMBIGUOUS_LEARNER")
    r = client.post(f"/api/queue/{amb['id']}/resolve",
                    json={"learner_id": amb["candidates"][0]["id"], "note": "test"})
    assert r.status_code == 200 and "outcome" in r.json()
    assert all(i["id"] != amb["id"] for i in client.get("/api/queue").json())
    again = client.post(f"/api/queue/{amb['id']}/resolve",
                        json={"learner_id": amb["candidates"][0]["id"], "note": ""})
    assert again.status_code == 404 and "error" in again.json()


def test_skip_item(client):
    item = _queue(client)[0]
    r = client.post(f"/api/queue/{item['id']}/skip", json={"note": "not needed"})
    assert r.status_code == 200
    assert all(i["id"] != item["id"] for i in client.get("/api/queue").json())
    allq = client.get("/api/queue?all=1").json()
    assert next(i for i in allq if i["id"] == item["id"])["resolution"] == "skipped:not needed"
    assert client.post(f"/api/queue/{item['id']}/skip", json={"note": ""}).status_code == 404


def test_workers_search(client):
    r = client.get("/api/workers", params={"q": "W-1038"}).json()
    assert len(r) == 1 and r[0]["id"] == "W-1038"
    assert len(client.get("/api/workers", params={"q": "a"}).json()) <= 25


def test_worker_detail_has_audit_after_run(client):
    client.post("/api/run")
    d = client.get("/api/workers/W-1038").json()
    assert d["worker"]["id"] == "W-1038" and d["learner_id"]
    assert d["audit"] and d["enrollments"]
    assert set(d["enrollments"][0]) == {"campaignName", "completionStatus", "deadline"}
    assert client.get("/api/workers/W-9999").status_code == 404


def test_campaigns_audience_text(client):
    camps = client.get("/api/campaigns").json()
    assert len(camps) == 6
    by = {c["campaignName"]: c["audience_text"] for c in camps}
    assert by["Manager Fundamentals"] == "managers"
    assert by["Data Privacy Essentials"] == "everyone"
    assert by["Harassment Prevention (US Employees)"] == "USA + non-managers"


def test_explain_absent_worker_no_change(client):
    r = client.post("/api/explain", json={"worker_id": "W-1008",
                                          "campaign_name": "Manager Fundamentals"})
    assert r.status_code == 200 and "No job change" in r.json()["sentence"]
    assert client.post("/api/explain", json={"worker_id": "W-1008"}).status_code == 422
    assert client.post("/api/explain", json={"worker_id": "W-9999",
                                             "campaign_name": "x"}).status_code == 404


def test_noop_job_change_then_run_ok(client):
    first = client.post("/api/run").json()["changes"]
    wid = next(c["worker_id"] for c in first if c["outcome"] in ("ok", "patched"))
    w = client.get(f"/workday/workers/{wid}").json()
    r = client.post("/workday/jobChanges", json={
        "workerId": wid, "effectiveDate": "2026-10-22", "reason": "Data_Change",
        "jobProfile": w["jobProfile"]["id"], "location": w["location"]["descriptor"]})
    assert r.status_code == 201
    body = client.post("/api/run").json()
    mine = [c for c in body["changes"] if c["change_id"] == r.json()["id"]]
    assert mine and mine[0]["outcome"] == "ok" and body["summary"]["failed"] == 0


def test_concurrent_runs_process_each_change_once(client):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        rs = list(ex.map(lambda _: client.post("/api/run"), range(2)))
    assert all(r.status_code == 200 for r in rs)
    # The later run re-sees the boundary change at the watermark and skips it, so count
    # only changes that were actually processed.
    sums = [r.json()["summary"] for r in rs]
    assert sum(x["changes_seen"] - x["skipped"] for x in sums) == 12


def test_bad_learner_id_is_422(client):
    r = client.post("/api/queue/1/resolve", json={"learner_id": "a b;", "note": ""})
    assert r.status_code == 422 and "error" in r.json()


def test_bad_worker_id_is_422(client):
    r = client.post("/api/explain", json={"worker_id": "W 1;", "campaign_name": "x"})
    assert r.status_code == 422 and "error" in r.json()
    r = client.get("/api/workers/W%201%3B")
    assert r.status_code == 422 and "error" in r.json()
