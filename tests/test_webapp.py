import pytest
from starlette.testclient import TestClient

from hrsync import store
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


def test_root_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Workday (mock)" in r.text


def test_mounted_mocks(client):
    r = client.get("/workday/workers?limit=1")
    assert r.status_code == 200 and "total" in r.json()
    assert client.get("/ethena/v1/learners?limit=1").status_code == 401
    assert client.get("/ethena/v1/learners?limit=1", auth=("x", "test-key")).status_code == 200


def test_state_before_run(client):
    s = client.get("/api/state").json()
    assert s["last_run"] is None and s["open_queue"] == 0
    assert s["watermark"] is None and s["failed_last_run"] == 0


def test_reset_twice_then_state_ok(client):
    for _ in range(2):
        r = client.post("/api/reset")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["workers"] > 0 and body["learners"] > 0
        assert body["changes"] == 0  # reset is the clean state
    assert client.get("/api/state").status_code == 200


def test_bad_day_endpoint_then_already_then_reset(client):
    assert client.post("/api/reset").status_code == 200
    assert client.post("/api/bad_day").json() == {"changes": 12, "duplicates": 1,
                                                  "missing": 3, "overdue": 2}
    assert client.post("/api/bad_day").json() == {"already": True}
    assert client.post("/api/reset").json()["changes"] == 0
    assert client.post("/api/bad_day").json()["changes"] == 12


def test_report_after_bad_day_before_run(client):
    client.post("/api/reset")
    client.post("/api/bad_day")
    rep = client.get("/api/report").json()
    assert "generated_at" in rep
    secs = {s["name"]: s for s in rep["sections"]}
    assert len(secs["terminated_but_active"]["rows"]) == 1
    assert len(secs["overdue_by_department"]["rows"]) >= 1


def test_run_recorded(settings):
    from datetime import UTC, datetime
    from webapp.clients import make_bundle
    from hrsync import engine
    b = make_bundle(settings)
    conn = store.connect(settings.sync_db)
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
    summary = engine.run(b.hris, b.learning, b.rules, conn, now=now)
    last = store.last_run(conn)
    assert last["run_id"] == summary.run_id
    assert last["started_at"] == "2026-10-23T12:00:00Z"
    assert last["changes_seen"] == summary.changes_seen
    conn.close()


def test_static_assets_served(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    r = client.get("/static/style.css")
    assert r.status_code == 200 and "css" in r.headers["content-type"]


def test_index_has_tabs_and_no_em_dash(client):
    html = client.get("/").text
    for title in ("Workday (mock)", "Sync", "Ethena (mock)", "Report"):
        assert title in html
    assert "—" not in html
    assert "__ASSET_VERSION__" not in html
    assert 'static/app.js?v=' in html and 'static/style.css?v=' in html


def test_index_has_trailing_slash_redirect_script(client):
    html = client.get("/").text
    head = html.split("<head>", 1)[1]
    assert "location.pathname.endsWith('/')" in head.split("</script>", 1)[0]


def test_docs_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
