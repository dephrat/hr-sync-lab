import json
import sqlite3

from ethena.db import audience_matches, enroll

from tests.test_webapp_api import client, settings  # noqa: F401

L = {"country": "CAN", "custom_category": "ON", "state": None, "is_manager": 1, "department": "Sales"}


def test_semantics():
    on = {"country": ["CAN"], "customCategory": ["ON"]}
    assert audience_matches([on, {"isManager": False}], L)
    assert not audience_matches([{"isManager": False}, {"country": ["USA"]}], L)
    assert not audience_matches([{"country": ["CAN"], "isManager": False}], L)
    assert not audience_matches([], L)
    assert audience_matches([{}], L)
    assert audience_matches(on, L)  # old dict form
    assert not audience_matches({"country": ["USA"]}, L)


def _enrolled(settings, cid):
    c = sqlite3.connect(settings.ethena_db)
    return {r[0] for r in c.execute("SELECT learner_id FROM enrollments WHERE campaign_id=?", (cid,))}


def test_put_validation(client):
    u = "/api/campaigns/C000004/audience"
    assert client.put(u, json={"groups": [{"country": "CAN", "region": "TX"}]}).status_code == 422
    assert client.put(u, json={"groups": [{"department": "Nope"}]}).status_code == 422
    assert client.put("/api/campaigns/C999999/audience", json={"groups": []}).status_code == 404


def test_edit_keeps_enrollments_and_blocks_new_us_manager(client, settings):
    before = _enrolled(settings, "C000004")
    r = client.put("/api/campaigns/C000004/audience",
                   json={"groups": [{"isManager": True, "country": "CAN"}]})
    assert r.status_code == 200 and r.json()["groups"][0]["country"] == "CAN"
    assert before <= _enrolled(settings, "C000004")
    c = sqlite3.connect(settings.ethena_db)
    c.execute("INSERT INTO learners (id,name,email,country,state,is_manager,department) "
              "VALUES ('LNEW','N','n@x.com','USA','MN',1,'Sales')")
    c.commit()
    c.row_factory = sqlite3.Row
    enroll(c, "LNEW")
    assert "LNEW" not in _enrolled(settings, "C000004")
    got = {x["id"]: x for x in client.get("/api/campaigns").json()}
    assert got["C000004"]["groups"] == [{"country": "CAN", "region": None, "isManager": True,
                                         "department": None}]


def test_second_group_enrolls_bc(client, settings):
    # Move Toronto to BC via the course rules, run sync so Ethena learners there become BC.
    assert client.put("/api/rules/office", json={"office": "Toronto, ON", "country": "CAN",
                                                 "region": "BC"}).status_code == 200
    client.post("/api/run")
    c = sqlite3.connect(settings.ethena_db)
    bc = {r[0] for r in c.execute("SELECT id FROM learners WHERE custom_category='BC' AND status='ACTIVE'")}
    assert bc
    r = client.put("/api/campaigns/C000001/audience", json={"groups": [
        {"country": "CAN", "region": "ON"}, {"country": "CAN", "region": "BC"}]})
    assert r.status_code == 200
    assert bc <= _enrolled(settings, "C000001")
    stored = json.loads(c.execute("SELECT audience FROM campaigns WHERE id='C000001'").fetchone()[0])
    assert len(stored) == 2 and stored[1] == {"country": ["CAN"], "customCategory": ["BC"]}
    rules = client.get("/api/rules").json()
    assert "Harassment Prevention (Ontario)" in next(
        o for o in rules["offices"] if o["office"] == "Toronto, ON")["courses"]
