"""Stdio MCP server for hr-sync-lab. Thin wrapper: logic lives in tools.py.

Settings come from env (HRIS_URL, ETHENA_URL, SYNC_DB, RULES_PATH ...). Relative paths are made
absolute at import, so the result does not depend on the cwd the MCP client launches us in.
"""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _k, _v in {"SYNC_DB": "data/sync.db", "HRIS_DB": "data/hris.db", "ETHENA_DB": "data/ethena.db",
               "RULES_PATH": "hrsync/rules.default.json"}.items():
    os.environ[_k] = str(_ROOT / os.environ.get(_k, _v))  # absolute paths stay unchanged

from mcp.server.mcpserver import MCPServer  # noqa: E402

from hrsync import rules as rules_mod, store  # noqa: E402
from hrsync.cli import _build_clients  # noqa: E402
from hrsync.config import Settings  # noqa: E402
from mcp_server import tools  # noqa: E402

mcp = MCPServer("hr-sync-lab")
_settings = Settings.from_env()
_bundle = None


def _ctx():
    """(hris, learning, store_conn, rules), created on first use."""
    global _bundle
    if _bundle is None:
        hris, learning = _build_clients(_settings)
        _bundle = (hris, learning, store.connect(_settings.sync_db),
                   rules_mod.load(_settings.rules_path))
    return _bundle


@mcp.tool()
def get_worker(worker_id: str) -> dict:
    """Read-only. Workday worker record plus the mapped Ethena learner_id (or null).
    Cost: one HTTP call to the HRIS."""
    hris, _, db, _ = _ctx()
    return tools.get_worker(hris, db, worker_id)


@mcp.tool()
def get_learner_status(worker_id: str) -> dict:
    """Read-only. The worker's Ethena learner and campaign enrollments with completion status.
    If no mapping exists it looks up by email and reports ambiguous=true on several matches.
    Cost: two or three HTTP calls to Ethena (and one to the HRIS when unmapped)."""
    hris, learning, db, _ = _ctx()
    return tools.get_learner_status(hris, learning, db, worker_id)


@mcp.tool()
def get_audit_trail(worker_id: str, limit: int = 20) -> list[dict]:
    """Read-only. Newest-first sync audit rows for a worker. Local database only, no HTTP."""
    _, _, db, _ = _ctx()
    return tools.get_audit_trail(db, worker_id, limit)


@mcp.tool()
def list_queue() -> list[dict]:
    """Read-only. Open review-queue items (changes the sync could not apply on its own).
    Local database only, no HTTP."""
    _, _, db, _ = _ctx()
    return tools.list_queue(db)


@mcp.tool()
def explain_gap(worker_id: str, campaign_name: str) -> str:
    """Read-only. Plain-language reason a worker is or is not enrolled in a campaign: checks
    enrollment, the open review queue, the rules and the audit log. Start here when someone
    asks why a person is missing training. Cost: one to three HTTP calls."""
    hris, learning, db, rules = _ctx()
    return tools.explain_gap(hris, learning, db, rules, worker_id, campaign_name)


@mcp.tool()
def resolve_queue_item(item_id: int, learner_id: str, confirm: bool = False) -> dict:
    """WRITES. Maps the queue item's worker to an Ethena learner, closes the item and reprocesses
    the job change (may patch the learner and enroll campaigns). Requires confirm=True or it
    returns an error and changes nothing. The action is recorded in the audit log.
    Cost: several HTTP calls to both systems."""
    hris, learning, db, rules = _ctx()
    return tools.resolve_queue_item(hris, learning, rules, db, item_id, learner_id, confirm)


@mcp.tool()
def rerun_sync(confirm: bool = False) -> dict:
    """WRITES. Runs a full sync and returns the summary: first the job changes since the
    watermark, then a check of every other Workday worker (terminated ones too) against Ethena,
    so drift with no Workday event is fixed as well. Requires confirm=True or it returns an
    error and changes nothing. Every step is recorded in the audit log. Cost: one HTTP call per
    worker plus Ethena calls."""
    hris, learning, db, rules = _ctx()
    return tools.rerun_sync(hris, learning, rules, db, confirm)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
