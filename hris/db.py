"""SQLite storage for the mock Workday HRIS, plus the one rule that mutates workers."""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

JOB_PROFILES = [
    "Sales_Rep", "Sales_Team_Lead", "Software_Engineer", "Engineering_Manager",
    "HR_Generalist", "Finance_Analyst", "Warehouse_Associate", "Warehouse_Supervisor",
]
LOCATIONS = [
    "Toronto, ON", "Vancouver, BC", "Minneapolis, MN", "New York, NY",
    "Austin, TX", "Remote - Canada", "Remote - US",
]
DEPARTMENTS = ["Sales", "Engineering", "People", "Finance", "Operations"]
REASONS = ["Promotion", "Transfer", "Hire", "Termination", "Data_Change"]
PROFILE_DEPARTMENT = {
    "Sales_Rep": "Sales", "Sales_Team_Lead": "Sales",
    "Software_Engineer": "Engineering", "Engineering_Manager": "Engineering",
    "HR_Generalist": "People", "Finance_Analyst": "Finance",
    "Warehouse_Associate": "Operations", "Warehouse_Supervisor": "Operations",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    descriptor TEXT NOT NULL,
    email TEXT NOT NULL,
    business_title TEXT NOT NULL,
    is_manager INTEGER NOT NULL,
    hire_date TEXT NOT NULL,
    job_profile TEXT NOT NULL,
    location TEXT NOT NULL,
    department TEXT NOT NULL,
    position_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    terminated INTEGER NOT NULL DEFAULT 0,
    status_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL REFERENCES workers(id),
    effective_date TEXT NOT NULL,
    completed_date TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    reason TEXT NOT NULL,
    job_profile TEXT,
    location TEXT,
    business_title TEXT,
    department TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_changes_completed ON job_changes(completed_date, seq);
"""


def connect(path) -> sqlite3.Connection:
    """Open (and create, if needed) the HRIS database."""
    conn = sqlite3.connect(Path(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(job_changes)")]
    if cols and "status" not in cols:
        # Older databases: completed_date was NOT NULL and there was no status column.
        conn.executescript(
            "ALTER TABLE job_changes RENAME TO job_changes_old;"
            "DROP INDEX IF EXISTS idx_job_changes_completed;")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO job_changes (seq, worker_id, effective_date, completed_date, status, reason,"
            " job_profile, location, business_title, department) SELECT seq, worker_id,"
            " effective_date, completed_date, 'completed', reason, job_profile, location,"
            " business_title, department FROM job_changes_old")
        conn.execute("DROP TABLE job_changes_old")
        conn.commit()
    conn.executescript(SCHEMA)
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_manager_profile(profile: str) -> bool:
    return profile.endswith(("_Lead", "_Manager", "_Supervisor"))


def profile_descriptor(profile_id: str) -> str:
    return profile_id.replace("_", " ")


def location_id(descriptor: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", descriptor).strip("_")


def apply_job_change(conn, *, worker_id, effective_date, reason, completed_date,
                     job_profile=None, location=None, business_title=None,
                     department=None) -> int:
    """Record a completed job change and apply it to the worker. Returns the change's seq."""
    cur = conn.execute(
        "INSERT INTO job_changes (worker_id, effective_date, completed_date, status, reason,"
        " job_profile, location, business_title, department) VALUES (?,?,?,'completed',?,?,?,?,?)",
        (worker_id, effective_date, completed_date, reason, job_profile, location,
         business_title, department))
    _apply_to_worker(conn, worker_id, effective_date, reason, job_profile, location,
                     business_title, department)
    conn.commit()
    return cur.lastrowid


def create_pending_change(conn, *, worker_id, effective_date, reason, job_profile=None,
                          location=None, business_title=None, department=None) -> int:
    """Record a job change awaiting approval. The worker is not touched."""
    cur = conn.execute(
        "INSERT INTO job_changes (worker_id, effective_date, completed_date, status, reason,"
        " job_profile, location, business_title, department) VALUES (?,?,NULL,'pending',?,?,?,?,?)",
        (worker_id, effective_date, reason, job_profile, location, business_title, department))
    conn.commit()
    return cur.lastrowid


def approve_change(conn, seq: int, completed_date: str) -> None:
    """Complete a pending change: apply it to the worker and stamp completed_date."""
    r = conn.execute("SELECT * FROM job_changes WHERE seq=?", (seq,)).fetchone()
    conn.execute("UPDATE job_changes SET status='completed', completed_date=? WHERE seq=?",
                 (completed_date, seq))
    _apply_to_worker(conn, r["worker_id"], r["effective_date"], r["reason"], r["job_profile"],
                     r["location"], r["business_title"], r["department"])
    conn.commit()


def _apply_to_worker(conn, worker_id, effective_date, reason, job_profile, location,
                     business_title, department) -> None:
    if job_profile:
        conn.execute("UPDATE workers SET job_profile=?, is_manager=? WHERE id=?",
                     (job_profile, int(is_manager_profile(job_profile)), worker_id))
        if not business_title:
            conn.execute("UPDATE workers SET business_title=? WHERE id=?",
                         (profile_descriptor(job_profile), worker_id))
    if location:
        conn.execute("UPDATE workers SET location=? WHERE id=?", (location, worker_id))
    if business_title:
        conn.execute("UPDATE workers SET business_title=? WHERE id=?",
                     (business_title, worker_id))
    if department:
        conn.execute("UPDATE workers SET department=? WHERE id=?", (department, worker_id))
    if reason == "Termination":
        conn.execute("UPDATE workers SET active=0, terminated=1, status_date=? WHERE id=?",
                     (effective_date, worker_id))
