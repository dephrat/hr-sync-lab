import pytest
from fastapi.testclient import TestClient

from ethena.app import create_app
from ethena.db import connect
from ethena.seed import seed
from hris.db import connect as hris_connect
from hris.seed import seed as hris_seed

AUTH = ("anyone", "test-key")


@pytest.fixture
def client(tmp_path):
    h = hris_connect(tmp_path / "hris.db")
    hris_seed(h)
    e = connect(tmp_path / "ethena.db")
    seed(e, h)
    e.close()
    h.close()
    c = TestClient(create_app(tmp_path / "ethena.db"))
    c.auth = AUTH
    return c


def all_learners(client):
    out, cursor = [], None
    while True:
        params = {"limit": 30}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/v1/learners", params=params).json()
        out += body["data"]
        cursor = body.get("nextCursor")
        if not cursor:
            return out


def campaign_names(client, learner_id):
    camps = {c["id"]: c["campaignName"] for c in client.get("/v1/training-campaigns", params={"limit": 100}).json()["data"]}
    ltcs = client.get("/v1/learner-training-campaigns", params={"learnerId": learner_id, "limit": 100}).json()["data"]
    return ltcs, sorted(camps[x["trainingCampaignId"]] for x in ltcs)


def test_requires_basic_auth(client):
    assert client.get("/v1/learners", auth=None).status_code == 401
    assert client.get("/v1/learners", auth=("u", "wrong")).status_code == 401
    assert client.get("/v1/learners").status_code == 200


def test_learners_cursor_pagination(client):
    learners = all_learners(client)
    ids = [x["id"] for x in learners]
    assert len(ids) == len(set(ids)) == 100 - 6 + 1
    assert learners[0]["id"].startswith("L") and ids[0].isalnum()


def test_seeded_mismatches(client):
    learners = all_learners(client)
    assert len([x for x in learners if x["email"] == "emma.park.1042@example.com"]) == 2
    assert client.get("/v1/learners", params={"email": "emma.park.1042@example.com"}).json()["data"].__len__() == 2
    assert client.get("/v1/learners/L999999").status_code == 404


def test_patch_learner_triggers_enrollment(client):
    lid = next(x["id"] for x in all_learners(client)
               if not x["isManager"] and x["status"] == "ACTIVE" and x["country"] == "CAN")
    r = client.patch(f"/v1/learners/{lid}",
                     json={"id": lid, "isManager": True, "country": "USA", "state": "MN"})
    assert r.status_code == 204
    _, names = campaign_names(client, lid)
    assert "Manager Fundamentals" in names
    assert "Harassment Prevention (US Managers)" in names


def test_patch_is_idempotent(client):
    lid = next(x["id"] for x in all_learners(client) if x["status"] == "ACTIVE")
    body = {"id": lid, "isManager": True, "country": "USA", "state": "MN"}
    client.patch(f"/v1/learners/{lid}", json=body)
    first, _ = campaign_names(client, lid)
    client.patch(f"/v1/learners/{lid}", json=body)
    second, _ = campaign_names(client, lid)
    assert len(first) == len(second) > 0


def test_terminated_learner_not_enrolled(client):
    r = client.post("/v1/learners", json={"name": "T Person", "email": "t@example.com",
                                          "status": "TERMINATED", "country": "USA", "state": "MN",
                                          "isManager": True})
    lid = r.json()["id"]
    client.patch(f"/v1/learners/{lid}", json={"id": lid, "isManager": False})
    ltcs, _ = campaign_names(client, lid)
    assert ltcs == []


def test_modules_mirror_enrollments(client):
    lid = next(x["id"] for x in all_learners(client) if x["status"] == "ACTIVE")
    ltcs, _ = campaign_names(client, lid)
    mods = client.get("/v1/learner-training-modules", params={"learnerId": lid}).json()["data"]
    assert len(mods) == len(ltcs) > 0
    assert client.get("/v1/learner-training-campaigns/" + ltcs[0]["id"]).status_code == 200
