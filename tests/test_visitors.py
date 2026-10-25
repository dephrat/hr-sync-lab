import pytest
from starlette.testclient import TestClient

from hrsync.config import Settings
from hrsync.seeding import seed_all
from webapp.app import create_app
from webapp.visitors import Site


@pytest.fixture
def site(tmp_path):
    s = Settings(hris_url="", ethena_url="", ethena_api_key="test-key",
                 sync_db=str(tmp_path / "sync.db"), hris_db=str(tmp_path / "hris.db"),
                 ethena_db=str(tmp_path / "ethena.db"), rules_path="hrsync/rules.default.json")
    seed_all(s.hris_db, s.ethena_db)
    (tmp_path / "visitors.txt").write_text("Tasha\nrecruiter\n")
    return TestClient(Site(s, create_app)), tmp_path


def test_invited_visitor_gets_own_copy(site):
    c, tmp = site
    assert "Workday (mock)" in c.get("/tasha/").text
    assert c.post("/tasha/api/bad_day").json()["changes"] == 12
    # Tasha's bad day is hers alone
    assert c.post("/api/bad_day").json()["changes"] == 12
    assert c.post("/recruiter/api/bad_day").json()["changes"] == 12
    assert c.post("/tasha/api/bad_day").json() == {"already": True}
    assert (tmp / "visitors" / "tasha" / "hris.db").exists()
    assert c.get("/tasha/workday/workers?limit=1").status_code == 200
    assert c.get("/tasha/static/app.js").status_code == 200


def test_reset_only_touches_own_copy(site):
    c, _ = site
    c.post("/api/bad_day"); c.post("/tasha/api/bad_day")
    assert c.post("/tasha/api/reset").json()["changes"] == 0
    assert c.post("/api/bad_day").json() == {"already": True}


def test_uninvited_name_is_not_found(site):
    c, tmp = site
    assert c.get("/stranger/").status_code == 404
    assert c.get("/stranger/api/state").status_code == 404
    assert not (tmp / "visitors" / "stranger").exists()
    assert c.get("/api/state").status_code == 200 and c.get("/").status_code == 200
