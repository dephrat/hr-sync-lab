import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ethena.app import create_app
from hris.app import create_app as create_hris
from hrsync.seeding import bad_day, seed_all
from hrsync import engine, rules, store
from hrsync.clients import EthenaClient, WorkdayClient
from reconcile import report

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
SECTIONS = ["Overdue training by department", "In HRIS, not in Ethena",
            "In Ethena, not in HRIS", "Terminated in HRIS but active in Ethena",
            "Stale enrollments", "Review queue age"]


@pytest.fixture
def env(tmp_path):
    hc = TestClient(create_hris(tmp_path / "hris.db"))
    ec = TestClient(create_app(tmp_path / "ethena.db"))
    ec.auth = ("anyone", "test-key")
    r = rules.load(Path(engine.__file__).parent / "rules.default.json")
    db = store.connect(tmp_path / "sync.db")
    seed_all(tmp_path / "hris.db", tmp_path / "ethena.db")
    facts = bad_day(tmp_path / "hris.db", tmp_path / "ethena.db")
    return {"facts": facts, "hc": hc, "hris": WorkdayClient(hc), "learning": EthenaClient(ec), "rules": r,
            "db": db, "paths": (tmp_path / "hris.db", tmp_path / "ethena.db",
                                tmp_path / "sync.db")}


def build(env):
    return report.build(*env["paths"], NOW)


def count(md, title):
    section = md.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]
    return int(section.split("Count: ", 1)[1].split("\n", 1)[0])


def test_seeded_drift_before_sync(env):
    md = build(env)
    assert md.startswith("# HR sync reconciliation, 2026-10-23T12:00:00Z")
    assert count(md, "Overdue training by department") >= 1
    for title in SECTIONS:
        assert f"## {title}\n" in md
    assert count(md, "Terminated in HRIS but active in Ethena") == 1
    assert count(md, "In HRIS, not in Ethena") == len(env["facts"]["absent_workers"])


def test_after_sync(env):
    engine.run(env["hris"], env["learning"], env["rules"], env["db"], now=NOW, full=False)
    md = build(env)
    assert count(md, "Terminated in HRIS but active in Ethena") == 0
    assert count(md, "In HRIS, not in Ethena") == len(env["facts"]["absent_workers"])
    jc = env["hc"].post("/jobChanges", json={
        "workerId": env["facts"]["absent_workers"][0], "effectiveDate": "2026-10-22", "reason": "Data_Change",
        "businessTitle": "Senior Specialist"}).json()
    env["hc"].post(f"/jobChanges/{jc['id']}/approve")
    engine.run(env["hris"], env["learning"], env["rules"], env["db"], now=NOW, full=False)
    assert count(build(env), "In HRIS, not in Ethena") == len(env["facts"]["absent_workers"]) - 1


def test_sections_shape(env):
    secs = report.sections(*env["paths"], NOW)
    assert [s["name"] for s in secs] == [n for n, _ in report.SECTIONS]
    assert [s["title"] for s in secs] == SECTIONS
    for s in secs:
        assert isinstance(s["headers"], list) and isinstance(s["rows"], list)
    md = build(env)
    assert all(f"## {t}" in md for t in SECTIONS)


def test_terminated_learner_courses_leave_the_overdue_report(env):
    """People who left stop counting: their unfinished courses become REMOVED, and a
    returning learner gets them reopened."""
    import sqlite3
    hris_db, ethena_db, sync_db = env["paths"]
    e = sqlite3.connect(ethena_db)
    stamp = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    lid, = e.execute("SELECT learner_id FROM enrollments WHERE deadline < ? LIMIT 1",
                     (stamp,)).fetchone()
    before = report.sections(hris_db, ethena_db, sync_db, NOW)
    overdue_before = sum(r[1] for r in next(x for x in before
                                           if x["name"] == "overdue_by_department")["rows"])
    env["learning"].patch_learner(lid, {"status": "TERMINATED"})
    statuses = {r[0] for r in e.execute(
        "SELECT completion_status FROM enrollments WHERE learner_id=?", (lid,))}
    assert statuses <= {"REMOVED", "COMPLETED"}
    after = report.sections(hris_db, ethena_db, sync_db, NOW)
    overdue_after = sum(r[1] for r in next(x for x in after
                                          if x["name"] == "overdue_by_department")["rows"])
    assert overdue_after < overdue_before
    env["learning"].patch_learner(lid, {"status": "ACTIVE"})
    assert "REMOVED" not in {r[0] for r in e.execute(
        "SELECT completion_status FROM enrollments WHERE learner_id=?", (lid,))}
