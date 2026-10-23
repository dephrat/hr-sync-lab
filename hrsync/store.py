"""SQLite state for the sync engine: watermark, worker->learner map, audit log, review queue."""
import sqlite3
from pathlib import Path

WATERMARK_KEY = "job_changes.completed_from"

SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS id_map (
    worker_id TEXT PRIMARY KEY, learner_id TEXT NOT NULL, source TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, run_id TEXT, change_id TEXT, worker_id TEXT, learner_id TEXT,
    action TEXT, outcome TEXT, detail TEXT, created_at TEXT, UNIQUE(change_id, action));
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY, change_id TEXT, worker_id TEXT, reason TEXT, detail TEXT,
    status TEXT, created_at TEXT, resolved_at TEXT, resolution TEXT);
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
    """INSERT OR REPLACE on UNIQUE(change_id, action): the latest attempt of a step wins.
    That lets a change that failed be retried (its `failed` row is overwritten) and lets a
    repeated `skip` for an already-processed change replace the previous skip row."""
    conn.execute(
        "INSERT OR REPLACE INTO audit (run_id, change_id, worker_id, learner_id, action, outcome,"
        " detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, change_id, worker_id, learner_id, action, outcome, detail, at))


def already_processed(conn, change_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM audit WHERE change_id=? AND outcome != 'failed'"
        " AND action IN ('patch_learner','terminate_learner','queue')", (change_id,)
    ).fetchone() is not None


def queue_item(conn, change_id, worker_id, reason, detail, at) -> None:
    """Open a review item unless an identical open one already exists."""
    if conn.execute("SELECT 1 FROM review_queue WHERE change_id IS ? AND worker_id=? AND reason=?"
                    " AND status='open'", (change_id, worker_id, reason)).fetchone():
        return
    conn.execute("INSERT INTO review_queue (change_id, worker_id, reason, detail, status,"
                 " created_at) VALUES (?,?,?,?,'open',?)",
                 (change_id, worker_id, reason, detail, at))
