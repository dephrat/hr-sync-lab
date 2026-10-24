"""SQLite state for the sync engine: watermark, worker->learner map, audit log, review queue."""
import json
import sqlite3
from pathlib import Path

WATERMARK_KEY = "job_changes.completed_from"

SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS id_map (
    worker_id TEXT PRIMARY KEY, learner_id TEXT NOT NULL, source TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, run_id TEXT, change_id TEXT, worker_id TEXT, learner_id TEXT,
    action TEXT, outcome TEXT, detail TEXT, created_at TEXT, UNIQUE(run_id, change_id, action));
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY, change_id TEXT, worker_id TEXT, reason TEXT, detail TEXT,
    status TEXT, created_at TEXT, resolved_at TEXT, resolution TEXT);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, started_at TEXT, summary TEXT);
"""


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path))
    conn.executescript(SCHEMA)
    return conn


def get_watermark(conn) -> str | None:
    row = conn.execute("SELECT value FROM watermark WHERE key=?", (WATERMARK_KEY,)).fetchone()
    return row[0] if row else None


def set_watermark(conn, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO watermark VALUES (?,?)", (WATERMARK_KEY, value))


def mapped_learner(conn, worker_id: str) -> str | None:
    row = conn.execute("SELECT learner_id FROM id_map WHERE worker_id=?", (worker_id,)).fetchone()
    return row[0] if row else None


def map_learner(conn, worker_id: str, learner_id: str, source: str, at: str) -> None:
    conn.execute("INSERT OR REPLACE INTO id_map VALUES (?,?,?,?)",
                 (worker_id, learner_id, source, at))


def audit(conn, run_id, change_id, worker_id, learner_id, action, outcome, detail, at) -> None:
    """Append one audit row. The log is append-only: uniqueness is (run_id, change_id, action),
    so recording the same step twice in a run raises IntegrityError."""
    conn.execute(
        "INSERT INTO audit (run_id, change_id, worker_id, learner_id, action, outcome,"
        " detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, change_id, worker_id, learner_id, action, outcome, detail, at))


def already_processed(conn, change_id: str) -> bool:
    """True when a terminal audit row exists; otherwise the change re-runs from the top."""
    return conn.execute(
        "SELECT 1 FROM audit WHERE change_id=? AND ((action IN ('verify_enrollment',"
        "'terminate_learner') AND outcome='ok') OR (action='queue' AND outcome='queued'))",
        (change_id,)).fetchone() is not None


def queue_item(conn, change_id, worker_id, reason, detail, at) -> None:
    """Open a review item unless the worker already has an open one for the same reason
    (whatever change or reconcile check raised it)."""
    if conn.execute("SELECT 1 FROM review_queue WHERE worker_id=? AND reason=?"
                    " AND status='open'", (worker_id, reason)).fetchone():
        return
    conn.execute("INSERT INTO review_queue (change_id, worker_id, reason, detail, status,"
                 " created_at) VALUES (?,?,?,?,'open',?)",
                 (change_id, worker_id, reason, detail, at))


def record_run(conn, summary: dict, started_at: str) -> None:
    """Store one run's summary (a dict with a run_id) so it can be shown after the fact."""
    conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)",
                 (summary["run_id"], started_at, json.dumps(summary, sort_keys=True)))
    conn.commit()


def last_run(conn) -> dict | None:
    """The newest recorded run by started_at: {"run_id", "started_at", **summary}."""
    row = conn.execute("SELECT run_id, started_at, summary FROM runs"
                       " ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {**json.loads(row[2]), "run_id": row[0], "started_at": row[1]}


def skip_item(conn, item_id: int, note: str, at: str) -> bool:
    """Close an open review item as skipped without touching Ethena. False when the item is
    missing or not open."""
    cur = conn.execute("UPDATE review_queue SET status='skipped', resolved_at=?, resolution=?"
                       " WHERE id=? AND status='open'", (at, f"skipped:{note}", item_id))
    conn.commit()
    return cur.rowcount == 1
