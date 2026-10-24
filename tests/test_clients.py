import httpx
import pytest
from fastapi.testclient import TestClient

from ethena.app import create_app
from hris.app import create_app as create_hris
from hrsync.seeding import bad_day, seed_all
from hrsync.clients import ClientError, EthenaClient, RateLimited, WorkdayClient


@pytest.fixture
def apps(tmp_path):
    seed_all(tmp_path / "hris.db", tmp_path / "ethena.db")
    bad_day(tmp_path / "hris.db", tmp_path / "ethena.db")
    hc = TestClient(create_hris(tmp_path / "hris.db"))
    ec = TestClient(create_app(tmp_path / "ethena.db"))
    ec.auth = ("anyone", "test-key")
    return hc, ec


def test_workday(apps):
    w = WorkdayClient(apps[0])
    assert w.get_worker("W-1042")["primaryWorkEmail"] == "emma.park.1042@example.com"
    changes = w.job_changes_since("2000-01-01T00:00:00Z")
    assert len(changes) == 12
    assert w.job_changes_since("2999-01-01T00:00:00Z") == []


def test_workday_pages_past_limit(apps):
    calls = []
    orig = apps[0].request
    apps[0].request = lambda m, u, **kw: (calls.append(kw["params"]["offset"]), orig(m, u, **kw))[1]
    w = WorkdayClient(apps[0])
    w.PAGE = 5
    assert len(w.job_changes_since("2000-01-01T00:00:00Z")) == 12
    assert calls == [0, 5, 10]


def test_ethena_duplicates_and_campaigns(apps):
    e = EthenaClient(apps[1])
    found = e.find_learners_by_email("emma.park.1042@example.com")
    assert len(found) == 2
    assert e.get_learner(found[0]["id"])["email"] == "emma.park.1042@example.com"
    camps = e.campaigns_for_learner(found[0]["id"])
    assert camps and all(c["courseName"] and c["campaignName"] for c in camps)
    assert len(e.list_campaigns()) == 6
    hp = [c for c in camps if c["campaignName"].startswith("Harassment")]
    assert hp and all(c["courseName"] == "Harassment Prevention" != c["campaignName"] for c in hp)


def test_create_and_patch(apps):
    e = EthenaClient(apps[1])
    lid = e.create_learner({"name": "New Person", "email": "new.person@example.com",
                            "country": "CAN", "isManager": False, "department": "Sales",
                            "customCategory": "ON", "workerType": "Employee", "language": "en"})
    e.patch_learner(lid, {"department": "Finance"})
    assert e.get_learner(lid)["department"] == "Finance"


def test_client_error_on_404(apps):
    with pytest.raises(ClientError) as ei:
        EthenaClient(apps[1]).get_learner("L999999")
    assert ei.value.status == 404 and ei.value.title == "Not Found"


def test_rate_limited():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "3"}, json={"title": "Too Many"})

    e = EthenaClient(httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x"))
    with pytest.raises(RateLimited) as ei:
        e.get_learner("L1")
    assert ei.value.retry_after == 3.0
