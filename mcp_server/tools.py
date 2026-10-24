"""Plain functions behind the MCP tools. Reads never write; the two writes need confirm=True."""
from dataclasses import asdict
from datetime import UTC, datetime

from hrsync import engine, store
from hrsync.clients import HrSyncClientError

CONFIRM_REQUIRED = {"error": "confirm required"}


def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_worker(hris, store_conn, worker_id: str) -> dict:
    return {**hris.get_worker(worker_id), "learner_id": store.mapped_learner(store_conn, worker_id)}


def get_learner_status(hris, learning, store_conn, worker_id: str) -> dict:
    lid = store.mapped_learner(store_conn, worker_id)
    out = {"worker_id": worker_id, "learner": None, "campaigns": []}
    if lid is None:
        found = learning.find_learners_by_email(hris.get_worker(worker_id)["primaryWorkEmail"])
        if len(found) > 1:
            out["ambiguous"] = True
            out["candidates"] = [f["id"] for f in found]
            return out
        if not found:
            return out
        lid = found[0]["id"]
    out["learner"] = learning.get_learner(lid)
    out["campaigns"] = [{k: c.get(k) for k in ("campaignName", "completionStatus", "deadline")}
                        for c in learning.campaigns_for_learner(lid)]
    return out


def get_audit_trail(store_conn, worker_id: str, limit: int = 20) -> list[dict]:
    return _rows(store_conn.execute(
        "SELECT * FROM audit WHERE worker_id=? ORDER BY id DESC LIMIT ?", (worker_id, limit)))


def list_queue(store_conn) -> list[dict]:
    return _rows(store_conn.execute("SELECT * FROM review_queue WHERE status='open' ORDER BY id"))


def explain_gap(hris, learning, store_conn, rules, worker_id: str, campaign_name: str) -> str:
    lid = store.mapped_learner(store_conn, worker_id)
    if lid:
        for c in learning.campaigns_for_learner(lid):
            if c.get("campaignName") == campaign_name:
                return (f"Learner {lid} is enrolled in {campaign_name!r} "
                        f"(status {c.get('completionStatus')}, deadline {c.get('deadline')}).")
    item = store_conn.execute(
        "SELECT change_id, reason, detail FROM review_queue WHERE worker_id=? AND status='open'"
        " ORDER BY id DESC LIMIT 1", (worker_id,)).fetchone()
    if item:
        change_id, reason, detail = item
        if change_id.startswith(engine.RECON_PREFIX):
            name = hris.get_worker(worker_id)["descriptor"]
            return (f"A reconcile check queued {name} ({reason}: {detail!r}); no attributes "
                    f"were written, so Ethena never enrolled this learner in {campaign_name!r}.")
        when = ""
        try:
            when = f" on {hris.get_job_change(change_id)['effectiveDate']}"
        except (HrSyncClientError, KeyError, TypeError):
            pass
        return (f"Job change {change_id}{when} is in the review queue ({reason}: {detail!r}); "
                f"no attributes were written, so Ethena never enrolled this learner in "
                f"{campaign_name!r}.")
    ok = store_conn.execute(
        "SELECT 1 FROM audit WHERE worker_id=? AND action='verify_enrollment' AND outcome='ok'"
        " ORDER BY id DESC LIMIT 1", (worker_id,)).fetchone()
    if ok:
        w = hris.get_worker(worker_id)
        profile, loc = w["jobProfile"]["id"], w["location"]["descriptor"]
        attrs = rules.attributes_for(profile, loc, w["primarySupervisoryOrganization"]["descriptor"])
        if attrs is not None and campaign_name not in rules.expected_campaigns(attrs):
            return f"{campaign_name!r} is not required by the rules for {profile} in {loc}."
    last = store_conn.execute(
        "SELECT action, outcome, created_at FROM audit WHERE worker_id=? ORDER BY id DESC LIMIT 1",
        (worker_id,)).fetchone()
    if last is None:
        try:
            pending = hris.pending_changes(worker_id)
        except (HrSyncClientError, AttributeError):
            pending = []
        if pending:
            p = pending[0]
            return (f"Change {p['id']} ({p['reason']['descriptor']}) is waiting for approval in "
                    f"Workday; the sync only sees approved changes.")
        return "No sync has processed this worker yet; run the sync."
    return (f"Last sync action was {last[0]} {last[1]} at {last[2]}; no queue item or rule "
            f"explains the missing {campaign_name!r} enrollment.")


def resolve_queue_item(hris, learning, rules, store_conn, item_id: int, learner_id: str,
                       confirm: bool = False) -> dict:
    if confirm is not True:
        return dict(CONFIRM_REQUIRED)
    try:
        return engine.resolve_with_learner(hris, learning, rules, store_conn, item_id,
                                           learner_id, datetime.now(UTC), "via mcp")
    except (ValueError, HrSyncClientError) as exc:
        return {"error": str(exc)}


def rerun_sync(hris, learning, rules, store_conn, confirm: bool = False) -> dict:
    if confirm is not True:
        return dict(CONFIRM_REQUIRED)
    return asdict(engine.run(hris, learning, rules, store_conn))
