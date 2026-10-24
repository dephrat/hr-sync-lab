import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ethena.app import create_app
from hris.app import create_app as create_hris
from hrsync.seeding import seed_all
from hrsync import engine, rules, store
from hrsync.clients import EthenaClient, RateLimited, WorkdayClient

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
WM = "job_changes.completed_from"


@pytest.fixture
def env(tmp_path):
    facts = seed_all(tmp_path / "hris.db", tmp_path / "ethena.db")
    hc = TestClient(create_hris(tmp_path / "hris.db"))
    ec = TestClient(create_app(tmp_path / "ethena.db"))
    ec.auth = ("anyone", "test-key")
    r = rules.load(Path(engine.__file__).parent / "rules.default.json")
    db = store.connect(tmp_path / "sync.db")
    return {"hc": hc, "hris": WorkdayClient(hc), "learning": EthenaClient(ec), "rules": r,
            "db": db, "facts": facts}


def run(env, full=False, **kw):
    return engine.run(env["hris"], env["learning"], env["rules"], env["db"], now=NOW,
                      full=full, **kw)


def change_for(env, reason, **match):
    for c in env["hris"].job_changes_since("2000-01-01T00:00:00Z"):
        if c["reason"]["id"] == reason and all(
                c.get(k, {}).get("id" if k == "jobProfile" else "descriptor") == v
                for k, v in match.items()):
            return c
    raise AssertionError(f"no {reason} change")


def learner_for(env, worker_id):
    return env["db"].execute("SELECT learner_id FROM id_map WHERE worker_id=?",
                             (worker_id,)).fetchone()[0]


def queue(env, reason):
    return env["db"].execute("SELECT worker_id, detail FROM review_queue WHERE reason=?",
                             (reason,)).fetchall()


def post_change(env, worker_id, **extra):
    resp = env["hc"].post("/jobChanges", json={
        "workerId": worker_id, "effectiveDate": "2026-10-22", "reason": "Data_Change",
        "businessTitle": "Senior Specialist", **extra})
    assert resp.status_code == 201
    return resp.json()


def drifted_engineer(env):
    """A non-manager Software_Engineer at a rules-known location who has a learner and no
    seeded job change."""
    skip = {*env["facts"]["absent_workers"], "W-1042", *planned(env),
            *env["facts"]["country_mismatch_workers"]}
    for w in (f"W-{1001 + i}" for i in range(100)):
        wk = env["hris"].get_worker(w)
        if (w not in skip and wk["jobProfile"]["id"] == "Software_Engineer"
                and wk["location"]["descriptor"] in env["rules"].locations):
            return w
    raise AssertionError("no suitable engineer in seed")


def planned(env):
    return [p["worker_id"] for p in env["facts"]["plans"]]


def promote(env, wid):
    return post_change(env, wid, reason="Promotion", jobProfile="Engineering_Manager",
                       supervisoryOrganization="Engineering")


def test_first_run_processes_seed_changes(env):
    s = run(env)
    assert s.changes_seen == 12 and s.failed == 0
    for c in env["hris"].job_changes_since("2000-01-01T00:00:00Z"):
        assert env["db"].execute("SELECT COUNT(*) FROM audit WHERE change_id=?",
                                 (c["id"],)).fetchone()[0] >= 1


def test_second_run_is_noop(env):
    promote(env, drifted_engineer(env))
    post_change(env, env["facts"]["absent_workers"][0])
    s1 = run(env)
    assert s1.patched >= 1 and s1.created == 1
    s = run(env)
    assert s.patched == s.created == s.terminated == 0
    assert s.failed == 0 and s.skipped >= 1


def test_promotion_sets_manager_and_enrolls(env):
    wid = drifted_engineer(env)
    c = promote(env, wid)
    run(env)
    lid = learner_for(env, wid)
    detail = env["db"].execute(
        "SELECT detail FROM audit WHERE change_id=? AND action='patch_learner' AND outcome='ok'",
        (c["id"],)).fetchone()[0]
    assert json.loads(detail)["isManager"] is True
    assert env["learning"].get_learner(lid)["isManager"] is True
    names = {x["campaignName"] for x in env["learning"].campaigns_for_learner(lid)}
    assert "Manager Fundamentals" in names
    assert env["db"].execute(
        "SELECT outcome FROM audit WHERE change_id=? AND action='verify_enrollment'",
        (c["id"],)).fetchone()[0] == "ok"


def test_vancouver_transfer_is_queued(env):
    c = change_for(env, "Transfer", location="Vancouver, BC")
    run(env)
    rows = queue(env, "UNKNOWN_LOCATION")
    assert any(w == c["worker"]["id"] and "Vancouver, BC" in d for w, d in rows)


def test_duplicate_email_is_queued(env):
    run(env)  # the seed plans a change for W-1042
    assert [w for w, _ in queue(env, "AMBIGUOUS_LEARNER")] == ["W-1042"]


def test_termination_terminates_learner(env):
    c = change_for(env, "Termination")
    s = run(env)
    assert s.terminated == 1
    lid = learner_for(env, c["worker"]["id"])
    assert env["learning"].get_learner(lid)["status"] == "TERMINATED"


def test_missing_learner_is_created(env):
    wid = env["facts"]["absent_workers"][0]
    post_change(env, wid)
    s = run(env)
    assert s.created == 1
    email = env["hris"].get_worker(wid)["primaryWorkEmail"]
    found = env["learning"].find_learners_by_email(email)
    assert len(found) == 1 and learner_for(env, wid) == found[0]["id"]
    assert env["db"].execute("SELECT source FROM id_map WHERE worker_id=?",
                             (wid,)).fetchone()[0] == "created"


def test_missing_learner_queued_when_create_disabled(env):
    wid = env["facts"]["absent_workers"][0]
    post_change(env, wid)
    s = run(env, create_missing=False)
    assert s.created == 0
    assert [w for w, _ in queue(env, "NO_LEARNER_MATCH")] == [wid]
    email = env["hris"].get_worker(wid)["primaryWorkEmail"]
    assert env["learning"].find_learners_by_email(email) == []


def test_rate_limit_stops_without_advancing_watermark(env):
    learning, orig = env["learning"], env["learning"].patch_learner
    calls = []

    def flaky(lid, body):
        calls.append(lid)
        if len(calls) == 3:
            raise RateLimited(1)
        return orig(lid, body)

    # Seeded learners already match HRIS, so add drift: three promotions to a manager profile.
    skip = {*env["facts"]["absent_workers"], "W-1042", *planned(env)}
    promoted = [w for w in (f"W-{1001 + i}" for i in range(100)) if w not in skip
                and not env["hris"].get_worker(w)["isManager"]][:3]
    for i, wid in enumerate(promoted):
        post_change(env, wid, reason="Promotion", jobProfile="Engineering_Manager",
                    completedDate=f"2026-10-23T0{i}:00:00Z")
    learning.patch_learner = flaky
    s = run(env)
    assert s.failed == 1
    assert env["db"].execute("SELECT value FROM watermark WHERE key=?", (WM,)).fetchone() is None
    done = {r[0] for r in env["db"].execute(
        "SELECT DISTINCT change_id FROM audit WHERE (action IN ('verify_enrollment',"
        "'terminate_learner') AND outcome='ok') OR (action='queue' AND outcome='queued')")}
    learning.patch_learner = orig
    s2 = run(env)
    assert s2.failed == 0 and s2.changes_seen == 15
    assert s2.skipped == len(done)
    assert env["db"].execute("SELECT value FROM watermark WHERE key=?", (WM,)).fetchone()
    failed = env["db"].execute("SELECT change_id, action FROM audit WHERE outcome='failed'").fetchall()
    assert len(failed) == 1
    assert env["db"].execute(
        "SELECT 1 FROM audit WHERE change_id=? AND action=? AND outcome='ok'", failed[0]).fetchone()


def test_watermark_boundary_does_not_reprocess(env):
    run(env)
    wm = env["db"].execute("SELECT value FROM watermark WHERE key=?", (WM,)).fetchone()[0]
    wid = env["facts"]["absent_workers"][1]
    jc = post_change(env, wid, completedDate=wm)
    s = run(env)
    assert s.created == 1
    s2 = run(env)
    assert s2.created == 0
    rows = env["db"].execute("SELECT action, outcome FROM audit WHERE change_id=?",
                             (jc["id"],)).fetchall()
    assert ("create_learner", "ok") in rows and ("skip", "skipped") in rows


def test_failure_after_patch_is_retried(env):
    wid = drifted_engineer(env)
    c = promote(env, wid)
    learning, orig = env["learning"], env["learning"].campaigns_for_learner
    target = []

    def flaky(lid):
        if not target and (lid,) == env["db"].execute(
                "SELECT learner_id FROM id_map WHERE worker_id=?", (wid,)).fetchone():
            target.append(lid)
            raise RateLimited(1)
        return orig(lid)

    learning.campaigns_for_learner = flaky
    s = run(env)
    assert s.failed == 1
    assert env["db"].execute("SELECT 1 FROM watermark WHERE key=?", (WM,)).fetchone() is None
    learning.campaigns_for_learner = orig
    s2 = run(env)
    assert s2.failed == 0
    rows = env["db"].execute("SELECT action, outcome FROM audit WHERE change_id=?",
                             (c["id"],)).fetchall()
    assert ("verify_enrollment", "failed") in rows and ("verify_enrollment", "ok") in rows
    assert ("skip", "skipped") not in rows


def test_enrollment_missing_is_queued(env):
    data = json.loads((Path(engine.__file__).parent / "rules.default.json").read_text())
    data["campaigns"].append({"campaignName": "Nonexistent Campaign", "when": {}})
    env["rules"] = rules.Rules(data)
    run(env)
    rows = env["db"].execute("SELECT detail FROM review_queue WHERE reason='ENROLLMENT_MISSING'"
                             " AND status='open'").fetchall()
    assert rows and all("Nonexistent Campaign" in d for (d,) in rows)


def bucket_total(s):
    return s.skipped + s.queued + s.created + s.terminated + s.patched + s.ok + s.failed


def test_first_run_patches_the_promotion(env):
    plan = next(p for p in env["facts"]["plans"] if p["reason"] == "Promotion"
                and p["fields"]["job_profile"] == "Sales_Team_Lead")
    wid = plan["worker_id"]
    email = env["hris"].get_worker(wid)["primaryWorkEmail"]
    (before,) = env["learning"].find_learners_by_email(email)
    assert before["isManager"] is False and before["customCategory"] == "ON"
    s = run(env)
    assert s.patched >= 1
    lid = learner_for(env, wid)
    assert env["learning"].get_learner(lid)["isManager"] is True
    names = {x["campaignName"] for x in env["learning"].campaigns_for_learner(lid)}
    assert {"Manager Fundamentals", "Harassment Prevention (Ontario)"} <= names


def test_seed_plants_no_country_mismatch_and_two_vancouver_workers(env):
    assert env["facts"]["country_mismatch_workers"] == []
    assert len(env["facts"]["absent_workers"]) == 3
    van = [w for w in env["hris"].list_workers() if w["location"]["descriptor"] == "Vancouver, BC"]
    assert len(van) == 3  # two at seed time plus the planned Transfer


def test_summary_buckets_sum_to_changes_seen(env):
    s1 = run(env)
    assert s1.changes_seen == 12 and bucket_total(s1) == 12
    post_change(env, env["facts"]["absent_workers"][0])
    s2 = run(env)
    assert s2.changes_seen >= 1 and bucket_total(s2) == s2.changes_seen


def test_failed_changes_fetch_is_recorded(env, monkeypatch):
    def boom(_since):
        raise RateLimited(1)
    monkeypatch.setattr(env["hris"], "job_changes_since", boom)
    s = run(env)
    assert s.failed == 1
    last = store.last_run(env["db"])
    assert last["run_id"] == s.run_id and last["failed"] == 1


def recon(env, **kw):
    return engine.reconcile(env["hris"], env["learning"], env["rules"], env["db"], now=NOW, **kw)


def test_reconcile_fixes_drift_then_is_idempotent(env):
    flipped = env["facts"]["country_mismatch_workers"]
    first = recon(env)
    assert first.mode == "reconcile" and first.failed == 0
    assert first.changes_seen == len(env["hris"].list_workers())
    assert first.patched >= len(flipped) and first.terminated >= 1
    for wid in flipped:
        rows = env["db"].execute(
            "SELECT outcome FROM audit WHERE change_id=? AND action='patch_learner'",
            (f"RECON-{wid}",)).fetchall()
        assert rows == [("ok",)]
    van = queue(env, "UNKNOWN_LOCATION")
    assert van and len({w for w, _ in van}) == len(van)
    n_open = env["db"].execute("SELECT COUNT(*) FROM review_queue WHERE status='open'").fetchone()[0]
    assert n_open > 0 and store.get_watermark(env["db"]) is None
    assert store.last_run(env["db"])["mode"] == "reconcile"
    second = recon(env)
    assert second.patched == second.created == second.terminated == 0
    assert env["db"].execute("SELECT COUNT(*) FROM review_queue WHERE status='open'"
                             ).fetchone()[0] == n_open


def test_run_after_reconcile_still_processes_changes(env):
    recon(env)
    s = run(env)
    assert s.changes_seen == 12 and s.skipped == 0 and s.failed == 0
    assert s.ok + s.queued > s.patched + s.created
    assert store.get_watermark(env["db"]) is not None


def test_full_run_is_one_run_and_second_is_noop(env):
    flipped = env["facts"]["country_mismatch_workers"]
    first = run(env, full=True)
    total = len(env["hris"].list_workers())
    changes = first.changes_seen - total + len({
        c["worker"]["id"] for c in env["hris"].job_changes_since("2000-01-01T00:00:00Z")})
    assert changes == 12 and first.failed == 0
    buckets = ("skipped", "queued", "created", "terminated", "patched", "ok", "failed")
    assert sum(getattr(first, b) for b in buckets) == first.changes_seen
    assert len(env["db"].execute("SELECT DISTINCT run_id FROM audit").fetchall()) == 1
    for wid in flipped:
        assert env["db"].execute("SELECT 1 FROM audit WHERE change_id=? AND action="
                                 "'patch_learner' AND outcome='ok'",
                                 (f"RECON-{wid}",)).fetchone() or env["db"].execute(
            "SELECT 1 FROM audit WHERE worker_id=? AND action='patch_learner' AND outcome='ok'",
            (wid,)).fetchone()
    second = run(env, full=True)
    assert second.patched == second.created == second.terminated == 0
    assert sum(getattr(second, b) for b in buckets) == second.changes_seen
