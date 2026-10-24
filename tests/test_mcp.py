import asyncio
import socket
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from ethena.app import create_app
from hris.app import create_app as create_hris
from hrsync.seeding import bad_day, seed_all
from hrsync import engine, rules, store
from hrsync.clients import EthenaClient, WorkdayClient
from mcp_server import tools

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
RULES = Path(engine.__file__).parent / "rules.default.json"


def seed_dbs(tmp_path):
    seed_all(tmp_path / "hris.db", tmp_path / "ethena.db")
    bad_day(tmp_path / "hris.db", tmp_path / "ethena.db")


@pytest.fixture
def env(tmp_path):
    seed_dbs(tmp_path)
    hc = TestClient(create_hris(tmp_path / "hris.db"))
    ec = TestClient(create_app(tmp_path / "ethena.db"))
    ec.auth = ("anyone", "test-key")
    r = rules.load(RULES)
    db = store.connect(tmp_path / "sync.db")
    hris, learning = WorkdayClient(hc), EthenaClient(ec)
    jc = hc.post("/jobChanges", json={
        "workerId": "W-1042", "effectiveDate": "2026-10-22", "reason": "Data_Change",
        "businessTitle": "Senior Specialist"})
    assert jc.status_code == 201
    assert hc.post(f"/jobChanges/{jc.json()['id']}/approve").status_code == 200
    engine.run(hris, learning, r, db, now=NOW)
    return {"hris": hris, "learning": learning, "rules": r, "db": db}


def open_item(env, reason):
    return env["db"].execute("SELECT id, worker_id FROM review_queue WHERE reason=?",
                             (reason,)).fetchone()


def test_explain_gap_names_queue_reason(env):
    _, wid = open_item(env, "UNKNOWN_LOCATION")
    text = tools.explain_gap(env["hris"], env["learning"], env["db"], env["rules"], wid,
                             "Anything")
    assert "manual review" in text and "office" in text


def test_list_queue_and_audit_trail(env):
    items = tools.list_queue(env["db"])
    assert {i["reason"] for i in items} >= {"UNKNOWN_LOCATION", "AMBIGUOUS_LEARNER"}
    _, wid = open_item(env, "AMBIGUOUS_LEARNER")
    trail = tools.get_audit_trail(env["db"], wid, limit=5)
    assert trail
    assert [r["id"] for r in trail] == sorted((r["id"] for r in trail), reverse=True)


def test_get_worker_and_learner_status(env):
    wid = env["db"].execute("SELECT worker_id FROM id_map LIMIT 1").fetchone()[0]
    w = tools.get_worker(env["hris"], env["db"], wid)
    assert w["id"] == wid and w["learner_id"]
    st = tools.get_learner_status(env["hris"], env["learning"], env["db"], wid)
    assert st["learner"]["id"] == w["learner_id"] and st["campaigns"]
    _, amb = open_item(env, "AMBIGUOUS_LEARNER")
    assert tools.get_learner_status(env["hris"], env["learning"], env["db"], amb)["ambiguous"]


def test_explain_gap_enrolled(env):
    wid = env["db"].execute("SELECT worker_id FROM id_map LIMIT 1").fetchone()[0]
    camp = tools.get_learner_status(env["hris"], env["learning"], env["db"],
                                    wid)["campaigns"][0]["campaignName"]
    assert "is enrolled in" in tools.explain_gap(env["hris"], env["learning"], env["db"],
                                                 env["rules"], wid, camp)


def test_writes_require_confirm(env):
    item_id, _ = open_item(env, "AMBIGUOUS_LEARNER")
    before = env["db"].execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    r = tools.resolve_queue_item(env["hris"], env["learning"], env["rules"], env["db"],
                                 item_id, "L100039")
    assert r == {"error": "confirm required"}
    assert tools.rerun_sync(env["hris"], env["learning"], env["rules"], env["db"]) == {
        "error": "confirm required"}
    assert env["db"].execute("SELECT status FROM review_queue WHERE id=?",
                             (item_id,)).fetchone()[0] == "open"
    assert env["db"].execute("SELECT COUNT(*) FROM audit").fetchone()[0] == before


def test_resolve_confirmed_and_errors(env):
    item_id, wid = open_item(env, "AMBIGUOUS_LEARNER")
    args = (env["hris"], env["learning"], env["rules"], env["db"])
    assert "error" in tools.resolve_queue_item(*args, item_id, "L-NOPE", confirm=True)
    r = tools.resolve_queue_item(*args, item_id, "L100039", confirm=True)
    assert r["item_id"] == item_id and r["learner_id"] == "L100039"
    assert store.mapped_learner(env["db"], wid) == "L100039"
    assert "error" in tools.resolve_queue_item(*args, item_id, "L100039", confirm=True)
    assert "changes_seen" in tools.rerun_sync(*args, confirm=True)


def _serve(app):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server, port


def test_stdio_smoke(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    seed_dbs(tmp_path)
    hs, hp = _serve(create_hris(tmp_path / "hris.db"))
    es, ep = _serve(create_app(tmp_path / "ethena.db", api_key="test-key"))
    env = {"HRIS_URL": f"http://127.0.0.1:{hp}", "ETHENA_URL": f"http://127.0.0.1:{ep}",
           "ETHENA_API_KEY": "test-key", "SYNC_DB": str(tmp_path / "sync.db"),
           "HRIS_DB": str(tmp_path / "hris.db"), "ETHENA_DB": str(tmp_path / "ethena.db"),
           "RULES_PATH": str(RULES)}
    root = Path(__file__).resolve().parent.parent

    async def main():
        params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"],
                                       env=env, cwd=str(root))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                res = await s.call_tool("list_queue", {})
                return names, res

    try:
        names, res = asyncio.run(main())
    finally:
        hs.should_exit = es.should_exit = True
    assert names == {"get_worker", "get_learner_status", "get_audit_trail", "list_queue",
                     "explain_gap", "resolve_queue_item", "rerun_sync"}
    assert not res.is_error


def test_rerun_sync_is_full(env):
    r = tools.rerun_sync(env["hris"], env["learning"], env["rules"], env["db"], True)
    assert r["changes_seen"] > 12 and r["failed"] == 0


def test_explain_gap_recon_item_sentence(env):
    env["db"].execute("INSERT INTO review_queue (change_id, worker_id, reason, detail, status,"
                      " created_at) VALUES ('RECON-W-1001','W-1001','UNKNOWN_PROFILE','x','open','t')")
    env["db"].execute("DELETE FROM id_map WHERE worker_id='W-1001'")
    env["db"].execute("DELETE FROM review_queue WHERE worker_id='W-1001' AND change_id!='RECON-W-1001'")
    text = tools.explain_gap(env["hris"], env["learning"], env["db"], env["rules"], "W-1001", "C")
    assert "manual review" in text and "RECON-" not in text


def test_explain_gap_pending_change_sentence(env):
    wid = "W-1001"
    env["db"].execute("DELETE FROM audit WHERE worker_id=?", (wid,))
    env["db"].execute("DELETE FROM review_queue WHERE worker_id=?", (wid,))
    env["db"].execute("DELETE FROM id_map WHERE worker_id=?", (wid,))
    env["hris"].http.post("/jobChanges", json={
        "workerId": wid, "effectiveDate": "2026-10-22", "reason": "Data_Change",
        "businessTitle": "Pending Title"})
    text = tools.explain_gap(env["hris"], env["learning"], env["db"], env["rules"], wid, "C")
    assert "is waiting for approval in Workday" in text and "correction" in text


def test_explain_gap_never_synced_sentence(env):
    wid = "W-1002"
    for t in ("audit", "review_queue", "id_map"):
        env["db"].execute(f"DELETE FROM {t} WHERE worker_id=?", (wid,))
    text = tools.explain_gap(env["hris"], env["learning"], env["db"], env["rules"], wid, "C")
    assert text.startswith("No sync has looked at") and "Run sync" in text
