import sqlite3

import pytest
from starlette.testclient import TestClient

from hrsync.config import Settings
from hrsync.seeding import bad_day, seed_all
from webapp.app import create_app


@pytest.fixture
def settings(tmp_path):
    s = Settings(hris_url="", ethena_url="", ethena_api_key="test-key",
                 sync_db=str(tmp_path / "sync.db"), hris_db=str(tmp_path / "hris.db"),
                 ethena_db=str(tmp_path / "ethena.db"),
                 rules_path="hrsync/rules.default.json")
    seed_all(s.hris_db, s.ethena_db)
    bad_day(s.hris_db, s.ethena_db)
    return s


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


BUCKETS = ("skipped", "queued", "created", "terminated", "patched", "ok", "failed")


def test_run_returns_narratives(client):
    body = client.post("/api/run").json()
    s = body["summary"]
    assert s["changes_seen"] > 12 and sum(s[b] for b in BUCKETS) == s["changes_seen"]
    assert len(body["changes"]) == s["changes_seen"]
    sentences = [c["sentence"] for c in body["changes"]]
    hit = [x for x in sentences if "W-1038" in x]
    assert hit and any("Manager Fundamentals" in x for x in hit)
    assert any("now a manager" in x.lower() and "Training confirmed" in x for x in hit)
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
    r = client.get("/api/workers", params={"q": "W-1038"}).json()["data"]
    assert len(r) == 1 and r[0]["id"] == "W-1038"
    assert len(client.get("/api/workers", params={"q": "a"}).json()["data"]) <= 25
    warehouse = client.get("/api/workers", params={"q": "warehouse"}).json()["data"]
    assert warehouse and all("Warehouse" in w["jobProfile"] for w in warehouse)
    toronto = client.get("/api/workers", params={"q": "toronto"}).json()["data"]
    assert toronto and all(w["location"] == "Toronto, ON" for w in toronto)
    body = client.get("/api/workers", params={"limit": 500}).json()
    assert body["total"] >= 100 and len(body["data"]) == body["total"]
    page2 = client.get("/api/workers", params={"limit": 25, "offset": 25}).json()
    page1_ids = {w["id"] for w in client.get("/api/workers", params={"limit": 25}).json()["data"]}
    assert len(page2["data"]) == 25 and page2["data"][0]["id"] not in page1_ids


def test_worker_detail_has_audit_after_run(client):
    client.post("/api/run")
    d = client.get("/api/workers/W-1038").json()
    assert d["worker"]["id"] == "W-1038" and d["learner_id"]
    assert d["audit"] and d["enrollments"]
    assert set(d["enrollments"][0]) == {"campaignName", "completionStatus", "deadline"}
    assert client.get("/api/workers/W-9999").status_code == 404


def test_campaigns_audience_text(client):
    camps = client.get("/api/campaigns").json()
    assert len(camps) == 7
    by = {c["campaignName"]: c["audience_text"] for c in camps}
    assert by["Manager Fundamentals"] == "isManager = true"
    assert by["Data Privacy Essentials"] == "everyone (no rules)"
    assert by["Harassment Prevention (US Employees)"] == "country = USA \u00b7 isManager = false"


def test_explain_absent_worker_no_change(client):
    r = client.post("/api/explain", json={"worker_id": "W-1008",
                                          "campaign_name": "Manager Fundamentals"})
    assert r.status_code == 200 and "No sync has processed" in r.json()["sentence"]
    assert client.post("/api/explain", json={"worker_id": "W-1008"}).status_code == 422
    assert client.post("/api/explain", json={"worker_id": "W-9999",
                                             "campaign_name": "Manager Fundamentals"}).status_code == 404


def test_noop_job_change_then_run_ok(client):
    first = client.post("/api/run").json()["changes"]
    wid = next(c["worker_id"] for c in first if c["outcome"] in ("ok", "patched"))
    w = client.get(f"/workday/workers/{wid}").json()
    r = client.post("/workday/jobChanges", json={
        "workerId": wid, "effectiveDate": "2026-10-22", "reason": "Data_Change",
        "businessTitle": "Senior " + w["businessTitle"]})
    assert r.status_code == 201 and r.json()["status"] == "PENDING"
    assert client.post(f"/workday/jobChanges/{r.json()['id']}/approve").status_code == 200
    body = client.post("/api/run").json()
    mine = [c for c in body["changes"] if c["change_id"] == r.json()["id"]]
    assert mine and mine[0]["outcome"] == "ok" and body["summary"]["failed"] == 0


def test_explain_unknown_campaign_is_422(client):
    r = client.post("/api/explain", json={"worker_id": "W-1001", "campaign_name": "nope"})
    assert r.status_code == 422 and r.json() == {"error": "unknown campaign"}


def test_concurrent_runs_process_each_change_once(client):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        rs = list(ex.map(lambda _: client.post("/api/run"), range(2)))
    assert all(r.status_code == 200 for r in rs)
    # The later run re-sees the boundary change at the watermark and skips it, so count
    # only changes that were actually processed.
    sums = [r.json()["summary"] for r in rs]
    first = max(sums, key=lambda x: x["changes_seen"] - x["skipped"])
    assert first["changes_seen"] - first["skipped"] > 12
    assert sum(x["patched"] + x["created"] + x["terminated"] for x in sums) == (
        first["patched"] + first["created"] + first["terminated"])


def test_bad_learner_id_is_422(client):
    r = client.post("/api/queue/1/resolve", json={"learner_id": "a b;", "note": ""})
    assert r.status_code == 422 and "error" in r.json()


def test_bad_worker_id_is_422(client):
    r = client.post("/api/explain", json={"worker_id": "W 1;", "campaign_name": "x"})
    assert r.status_code == 422 and "error" in r.json()
    r = client.get("/api/workers/W%201%3B")
    assert r.status_code == 422 and "error" in r.json()


def test_learners_have_enrollments(client):
    rows = client.get("/api/learners").json()
    assert len(rows) == 98
    assert all(isinstance(r["enrollments"], list) for r in rows)
    assert any(r["enrollments"] for r in rows)
    assert {"campaignName", "completionStatus", "deadline"} <= set(
        next(e for r in rows for e in r["enrollments"]))


def test_drift_before_and_after_run(client):
    d = client.get("/api/drift").json()
    assert d["count"] >= 10 and d["count"] == len(d["items"])
    kinds = {i["kind"] for i in d["items"]}
    assert "terminated" in kinds
    assert any(i["kind"] == "attrs" and i["worker_id"] == "W-1038" and "manager" in i["sentence"]
               for i in d["items"])
    assert all("—" not in i["sentence"] for i in d["items"])
    client.post("/api/run")
    after = client.get("/api/drift").json()
    assert after["count"] == len(after["items"])
    changed = {c["worker"]["id"] for c in
               client.get("/workday/jobChanges", params={"limit": 100}).json()["data"]}
    # Seeded drift with no job change behind it is invisible to the sync, so it stays.
    for i in after["items"]:
        assert (i["in_queue"] or i["kind"] in ("no_learner", "needs_rule")
                or (i["kind"] == "attrs" and i["worker_id"] not in changed)), i
    assert not {i["kind"] for i in after["items"]} & {"courses", "terminated"}
    assert not any(i["worker_id"] == "W-1038" for i in after["items"])
    open_q = {q["worker_id"] for q in client.get("/api/queue").json()}
    assert {i["worker_id"] for i in after["items"] if i["in_queue"]} <= open_q


def test_run_clears_pending_drift(client):
    body = client.post("/api/run").json()
    assert body["summary"]["changes_seen"] > 12
    assert any(c["reason"] == "Spot check" for c in body["changes"])
    after = client.get("/api/drift").json()
    assert not [i for i in after["items"]
                if not i["in_queue"] and i["kind"] in ("attrs", "courses", "terminated")]


def test_hire_then_sync_adds_to_ethena(client):
    r = client.post("workday/workers", json={"name": "Priya Nair", "jobProfile": "Software_Engineer",
                                             "location": "Toronto, ON"})
    assert r.status_code == 201
    wid = r.json()["id"]
    assert any(i["worker_id"] == wid and i["kind"] == "no_learner"
               for i in client.get("/api/drift").json()["items"])
    sentences = [c["sentence"] for c in client.post("/api/run").json()["changes"] if wid in c["sentence"]]
    assert any("Added to Ethena" in x for x in sentences)
    assert all(i["worker_id"] != wid for i in client.get("/api/drift").json()["items"])


def test_completing_seeded_overdue_clears_report(client, settings):
    import sqlite3
    auth = {"Authorization": "Basic " + __import__("base64").b64encode(b"web:test-key").decode()}
    late = sqlite3.connect(settings.ethena_db).execute(
        "SELECT id FROM enrollments WHERE deadline < strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " AND completion_status != 'COMPLETED'").fetchall()
    assert len(late) == 2
    for (eid,) in late:
        assert client.post(f"ethena/v1/learner-training-campaigns/{eid}/complete",
                           headers=auth).status_code == 204
    secs = {s["name"]: s for s in client.get("/api/report").json()["sections"]}
    assert secs["overdue_by_department"]["rows"] == []


def test_clean_seed_has_no_drift(tmp_path):
    s = Settings(hris_url="", ethena_url="", ethena_api_key="test-key",
                 sync_db=str(tmp_path / "sync.db"), hris_db=str(tmp_path / "hris.db"),
                 ethena_db=str(tmp_path / "ethena.db"), rules_path="hrsync/rules.default.json")
    facts = seed_all(s.hris_db, s.ethena_db)
    assert facts["plans"] == [] and facts["absent_workers"] == []
    c = TestClient(create_app(s))
    assert c.get("/api/drift").json() == {"count": 0, "items": []}
    secs = {x["name"]: x for x in c.get("/api/report").json()["sections"]}
    assert secs["overdue_by_department"]["rows"] == []
    assert secs["terminated_but_active"]["rows"] == []
    assert c.get("/workday/jobChanges", params={"limit": 100}).json()["data"] == []


# ---------- translation rules ----------

def test_get_rules_covers_everything(client):
    from hris.db import JOB_PROFILES, LOCATIONS
    r = client.get("/api/rules").json()
    assert [o["office"] for o in r["offices"]] == LOCATIONS
    assert [j["job"] for j in r["jobs"]] == JOB_PROFILES
    by = {o["office"]: o for o in r["offices"]}
    assert {k: by["Toronto, ON"][k] for k in ("office", "country", "region")} == {
        "office": "Toronto, ON", "country": "CAN", "region": "ON"}
    assert "Harassment Prevention (Ontario)" in by["Toronto, ON"]["courses"]
    assert by["Vancouver, BC"]["courses"] == []
    jobs = {j["job"]: j for j in r["jobs"]}
    assert "Manager Fundamentals" in jobs["Sales_Team_Lead"]["courses"]
    assert "Workplace Safety (Warehouse)" in jobs["Warehouse_Associate"]["courses"]
    assert r["everyone"] == ["Data Privacy Essentials"]
    assert by["Austin, TX"]["region"] == "TX" and by["Vancouver, BC"]["country"] is None
    jobs = {j["job"]: j for j in r["jobs"]}
    assert jobs["Warehouse_Supervisor"]["isManager"] is True
    assert "category" not in jobs["Warehouse_Supervisor"] and "categories" not in r["choices"]
    assert sorted(jobs["Warehouse_Supervisor"]["courses"]) == ["Manager Fundamentals", "Workplace Safety (Warehouse)"]
    assert jobs["Warehouse_Associate"]["courses"] == ["Workplace Safety (Warehouse)"]
    assert jobs["Sales_Rep"]["courses"] == [] and jobs["Sales_Rep"]["label"] == "Sales Rep"
    assert {c["value"] for c in r["choices"]["regions"]["CAN"]} == {"ON", "BC", "AB", "QC"}


def test_put_rules_validation(client):
    put = lambda path, body: client.put("/api/rules/" + path, json=body)
    r = put("office", {"office": "Vancouver, BC", "country": "CAN", "region": "TX"})
    assert r.status_code == 422 and "error" in r.json()
    assert put("office", {"office": "Atlantis", "country": "CAN", "region": "ON"}).status_code == 422
    assert put("office", {"office": "Vancouver, BC", "country": "FRA"}).status_code == 422
    assert put("job", {"job": "Nope", "isManager": True}).status_code == 422
    ok = put("office", {"office": "Vancouver, BC", "country": "CAN", "region": "BC"}).json()
    assert ok == {"office": "Vancouver, BC", "country": "CAN", "region": "BC"}
    cleared = put("office", {"office": "Vancouver, BC", "country": None, "region": "BC"}).json()
    assert cleared["country"] is None and cleared["region"] is None
    job = put("job", {"job": "Sales_Rep", "isManager": True}).json()
    assert job == {"job": "Sales_Rep", "label": "Sales Rep", "isManager": True}


def test_rules_persist_and_reset_restores(settings, client):
    client.put("/api/rules/office", json={"office": "Vancouver, BC", "country": "CAN", "region": "BC"})
    client.put("/api/rules/office", json={"office": "Austin, TX", "country": None})
    again = TestClient(create_app(settings))
    by = {o["office"]: o for o in again.get("/api/rules").json()["offices"]}
    assert by["Vancouver, BC"]["region"] == "BC" and by["Austin, TX"]["country"] is None
    again.post("/api/reset")
    by = {o["office"]: o for o in again.get("/api/rules").json()["offices"]}
    assert by["Vancouver, BC"]["country"] is None and by["Austin, TX"]["region"] == "TX"


def test_needs_rule_sentence_points_at_translation_rules(client):
    items = client.get("/api/drift").json()["items"]
    s = [i["sentence"] for i in items if i["kind"] == "needs_rule"]
    assert s and all("courses not specified" in x and "Course rules" in x for x in s)


def test_adding_vancouver_rule_clears_manual_review(client):
    client.post("/api/run")
    vq = [i for i in client.get("/api/queue").json() if i["reason"] == "UNKNOWN_LOCATION"]
    assert len(vq) == 3
    client.put("/api/rules/office", json={"office": "Vancouver, BC", "country": "CAN", "region": "BC"})
    client.post("/api/run")
    still = [i for i in client.get("/api/queue").json() if i["reason"] == "UNKNOWN_LOCATION"]
    assert still == []
    all_items = client.get("/api/queue?all=1").json()
    closed = [i for i in all_items if i["reason"] == "UNKNOWN_LOCATION"]
    assert len(closed) == 3 and all(i["resolution"] == "auto: rule added" for i in closed)
    for i in closed:
        w = client.get(f"/api/workers/{i['worker_id']}").json()
        lrn = w.get("learner") or {}
        assert lrn.get("customCategory") == "BC" and lrn.get("country") == "CAN"
    items = client.get("/api/drift").json()["items"]
    assert not [i for i in items if i["kind"] == "needs_rule"]
    assert not [i for i in items if i["worker_id"] in {c["worker_id"] for c in closed}]


def test_toronto_warehouse_worker_gets_both_province_and_safety_courses(tmp_path):
    s = Settings(hris_url="", ethena_url="", ethena_api_key="test-key",
                 sync_db=str(tmp_path / "sync.db"), hris_db=str(tmp_path / "hris.db"),
                 ethena_db=str(tmp_path / "ethena.db"), rules_path="hrsync/rules.default.json")
    seed_all(s.hris_db, s.ethena_db)
    c = TestClient(create_app(s))
    h = sqlite3.connect(s.hris_db)
    email = h.execute("SELECT email FROM workers WHERE job_profile LIKE 'Warehouse%' "
                      "AND location='Toronto, ON' ORDER BY id LIMIT 1").fetchone()[0]
    row = next(r for r in c.get("/api/learners").json() if r["email"] == email)
    got = {e["campaignName"] for e in row["enrollments"]}
    assert {"Harassment Prevention (Ontario)", "Workplace Safety (Warehouse)"} <= got
    assert row["customCategory"] == "ON" and row["department"] == "Operations"
