import pytest
from fastapi.testclient import TestClient

from ethena.app import create_app
from hrsync.seeding import bad_day, seed_all

AUTH = ("anyone", "test-key")


@pytest.fixture
def client(tmp_path):
    seed_all(tmp_path / "hris.db", tmp_path / "ethena.db")
    bad_day(tmp_path / "hris.db", tmp_path / "ethena.db")
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
        assert body["limit"] == 30 and "nextCursor" not in body
        if not body["hasMore"]:
            return out
        cursor = body["data"][-1]["id"]


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
    assert len(ids) == len(set(ids)) == 100 - 3 + 1
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


def test_envelope_has_more(client):
    body = client.get("/v1/learners", params={"limit": 5}).json()
    assert body["limit"] == 5 and body["hasMore"] is True and len(body["data"]) == 5
    last = client.get("/v1/training-campaigns", params={"limit": 100}).json()
    assert last["hasMore"] is False and len(last["data"]) == 6


def test_patch_null_on_required_field_is_422(client):
    lid = all_learners(client)[0]["id"]
    for field in ("isManager", "name", "email"):
        r = client.patch(f"/v1/learners/{lid}", json={"id": lid, field: None})
        assert r.status_code == 422
        assert set(r.json()) == {"title", "detail"}


def test_patch_non_usa_country_clears_state(client):
    lid = next(x["id"] for x in all_learners(client) if x["state"] == "MN")
    assert client.patch(f"/v1/learners/{lid}", json={"id": lid, "country": "CAN"}).status_code == 204
    got = client.get(f"/v1/learners/{lid}").json()
    assert got["country"] == "CAN" and got["state"] is None


def test_format_and_enum_validation(client):
    lid = all_learners(client)[0]["id"]
    bad = [{"status": "BOGUS"}, {"country": "ca"}, {"country": "CANADA"}, {"state": "mn"},
           {"country": "CAN", "state": "ON"}]
    for b in bad:
        assert client.patch(f"/v1/learners/{lid}", json={"id": lid, **b}).status_code == 422, b
        assert client.post("/v1/learners", json={"name": "x", "email": "x@e.com", **b}).status_code == 422, b
    assert client.patch(f"/v1/learners/{lid}", json={"id": lid, "status": "DEACTIVATED"}).status_code == 422
    assert client.post("/v1/learners", json={"name": "x", "email": "x@e.com", "status": "DEACTIVATED"}).status_code == 200


def _first_open_enrollment(client):
    lid = next(x["id"] for x in all_learners(client) if x["status"] == "ACTIVE")
    ltcs, _ = campaign_names(client, lid)
    return lid, ltcs[0]["id"]


def test_complete_enrollment(client):
    lid, ltc = _first_open_enrollment(client)
    assert client.post(f"/v1/learner-training-campaigns/{ltc}/complete").status_code == 204
    assert client.get(f"/v1/learner-training-campaigns/{ltc}").json()["completionStatus"] == "COMPLETED"
    mods = client.get("/v1/learner-training-modules", params={"learnerId": lid}).json()["data"]
    assert next(m for m in mods if m["learnerTrainingCampaignId"] == ltc)["status"] == "USER_COMPLETED"
    assert client.post(f"/v1/learner-training-campaigns/{ltc}/complete").status_code == 204
    assert client.get(f"/v1/learner-training-campaigns/{ltc}").json()["completionStatus"] == "COMPLETED"


def test_complete_unknown_is_404_and_needs_auth(client):
    assert client.post("/v1/learner-training-campaigns/LTC999999/complete").status_code == 404
    _, ltc = _first_open_enrollment(client)
    assert client.post(f"/v1/learner-training-campaigns/{ltc}/complete", auth=None).status_code == 401
