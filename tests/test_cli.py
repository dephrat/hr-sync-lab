import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ethena.app import create_app
from hris.app import create_app as create_hris
from hrsync import cli, store
from hrsync.clients import EthenaClient, WorkdayClient

RULES = Path(cli.__file__).parent / "rules.default.json"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"HRIS_DB": "hris.db", "ETHENA_DB": "ethena.db", "SYNC_DB": "sync.db"}.items():
        monkeypatch.setenv(k, str(tmp_path / v))
    monkeypatch.setenv("RULES_PATH", str(RULES))
    assert cli.main(["seed"]) == 0
    hc = TestClient(create_hris(tmp_path / "hris.db"))
    ec = TestClient(create_app(tmp_path / "ethena.db"))
    ec.auth = ("anyone", "test-key")
    monkeypatch.setattr(cli, "_build_clients", lambda s: (WorkdayClient(hc), EthenaClient(ec)))
    return {"hc": hc, "db": tmp_path / "sync.db", "learning": EthenaClient(ec), "hris": WorkdayClient(hc)}


def open_item(env, reason):
    if reason == "AMBIGUOUS_LEARNER" and not queued_ambiguous(env):
        resp = env["hc"].post("/jobChanges", json={
            "workerId": "W-1042", "effectiveDate": "2026-10-22", "reason": "Data_Change",
            "businessTitle": "Senior Specialist"})
        assert resp.status_code == 201
        cli.main(["run"])
    conn = store.connect(env["db"])
    return conn, conn.execute("SELECT id, worker_id, status FROM review_queue WHERE reason=?",
                              (reason,)).fetchone()


def queued_ambiguous(env):
    return store.connect(env["db"]).execute(
        "SELECT 1 FROM review_queue WHERE reason='AMBIGUOUS_LEARNER'").fetchone()


def test_run_prints_changes_seen(env, capsys):
    capsys.readouterr()
    assert cli.main(["run"]) == 0
    assert "changes_seen" in capsys.readouterr().out
    assert cli.main(["run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] > 0


def test_queue_lists_vancouver(env, capsys):
    cli.main(["run"])
    capsys.readouterr()
    assert cli.main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "UNKNOWN_LOCATION" in out and "Vancouver, BC" in out


def test_resolve_unknown_learner_exits_2(env, capsys):
    cli.main(["run"])
    conn, (item_id, _, _) = open_item(env, "AMBIGUOUS_LEARNER")
    assert cli.main(["resolve", str(item_id), "--map-learner", "L999999"]) == 2
    assert conn.execute("SELECT status FROM review_queue WHERE id=?", (item_id,)).fetchone()[0] == "open"


def test_resolve_ambiguous_maps_and_patches(env, capsys):
    cli.main(["run"])
    conn, (item_id, wid, _) = open_item(env, "AMBIGUOUS_LEARNER")
    email = env["hris"].get_worker(wid)["primaryWorkEmail"]
    lid = env["learning"].find_learners_by_email(email)[0]["id"]
    assert cli.main(["resolve", str(item_id), "--map-learner", lid, "--note", "dup"]) == 0
    assert conn.execute("SELECT learner_id, source FROM id_map WHERE worker_id=?",
                        (wid,)).fetchone() == (lid, "manual")
    status, res = conn.execute("SELECT status, resolution FROM review_queue WHERE id=?",
                               (item_id,)).fetchone()
    assert status == "resolved" and res.startswith(f"mapped:{lid}") and "dup" in res
    assert conn.execute("SELECT 1 FROM audit WHERE worker_id=? AND action='verify_enrollment'"
                        " AND outcome='ok'", (wid,)).fetchone()
    assert cli.main(["resolve", str(item_id), "--skip"]) == 2  # no longer open


def test_resolve_skip(env):
    cli.main(["run"])
    conn, (item_id, _, _) = open_item(env, "UNKNOWN_LOCATION")
    assert cli.main(["resolve", str(item_id), "--skip", "--note", "n/a"]) == 0
    assert conn.execute("SELECT status, resolution FROM review_queue WHERE id=?",
                        (item_id,)).fetchone() == ("skipped", "skipped:n/a")


def test_resolve_exits_1_when_rerun_is_queued(env, tmp_path, monkeypatch, capsys):
    cli.main(["run"])
    conn, (item_id, wid, _) = open_item(env, "AMBIGUOUS_LEARNER")
    location = env["hris"].get_worker(wid)["location"]["descriptor"]
    data = json.loads(RULES.read_text())
    data["locations"].pop(location, None)
    narrow = tmp_path / "rules.narrow.json"
    narrow.write_text(json.dumps(data))
    monkeypatch.setenv("RULES_PATH", str(narrow))
    email = env["hris"].get_worker(wid)["primaryWorkEmail"]
    lid = env["learning"].find_learners_by_email(email)[0]["id"]
    capsys.readouterr()
    assert cli.main(["resolve", str(item_id), "--map-learner", lid]) == 1
    err = capsys.readouterr().err
    assert "mapping saved" in err and "queued" in err and "UNKNOWN_LOCATION" in err
    assert store.mapped_learner(conn, wid) == lid


def test_unreachable_mocks_exit_cleanly(env, monkeypatch, capsys):
    import httpx
    down = lambda s: (WorkdayClient(httpx.Client(base_url="http://127.0.0.1:9")),
                      EthenaClient(httpx.Client(base_url="http://127.0.0.1:9")))
    monkeypatch.setattr(cli, "_build_clients", down)
    assert cli.main(["run"]) == 1
    assert "cannot reach the HRIS" in capsys.readouterr().err
    conn = store.connect(env["db"])
    store.queue_item(conn, "JC-x", "W-1001", "NO_LEARNER_MATCH", "t", "2026-10-23T00:00:00Z")
    conn.commit()
    item_id = conn.execute("SELECT MAX(id) FROM review_queue").fetchone()[0]
    assert cli.main(["resolve", str(item_id), "--map-learner", "L100001"]) == 2
    assert "cannot reach" in capsys.readouterr().err
