"""SQLite storage for the mock Ethena, plus audience matching and the enrollment rule."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS learners (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    country TEXT,
    state TEXT,
    is_manager INTEGER NOT NULL DEFAULT 0,
    manager_learner_id TEXT,
    department TEXT,
    worker_type TEXT,
    custom_category TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    training_url TEXT
);
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    course_name TEXT NOT NULL,
    campaign_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    initial_training_at TEXT,
    next_training_at TEXT,
    deadline_in_days INTEGER NOT NULL,
    audience TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS enrollments (
    id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL REFERENCES learners(id),
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    enrollment_status TEXT NOT NULL,
    completion_status TEXT NOT NULL,
    enrolled_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    UNIQUE (learner_id, campaign_id)
);
CREATE TABLE IF NOT EXISTS modules (
    id TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
    learner_id TEXT NOT NULL,
    module_name TEXT NOT NULL,
    length_in_minutes INTEGER NOT NULL,
    assigned_at TEXT NOT NULL
);
"""


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def audience_matches(audience: dict, learner) -> bool:
    """Every key present in the audience must match; {} matches everyone."""
    if "isManager" in audience and bool(learner["is_manager"]) != audience["isManager"]:
        return False
    for key, col in (("country", "country"), ("state", "state"),
                     ("customCategory", "custom_category")):
        if key in audience and learner[col] not in audience[key]:
            return False
    return True


def enroll(conn, learner_id: str) -> None:
    """Ensure an enrollment (+ module) exists for each ACTIVE campaign matching the learner.
    Existing enrollments are never removed. TERMINATED/DEACTIVATED learners get nothing new."""
    learner = conn.execute("SELECT * FROM learners WHERE id=?", (learner_id,)).fetchone()
    if learner is None or learner["status"] != "ACTIVE":
        return
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for c in conn.execute("SELECT * FROM campaigns WHERE status='ACTIVE' ORDER BY id").fetchall():
        if not audience_matches(json.loads(c["audience"]), learner):
            continue
        if conn.execute("SELECT 1 FROM enrollments WHERE learner_id=? AND campaign_id=?",
                        (learner_id, c["id"])).fetchone():
            continue
        n = conn.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0] + 1
        while conn.execute("SELECT 1 FROM enrollments WHERE id=?", (f"LTC{n:06d}",)).fetchone():
            n += 1
        ltc, ltm = f"LTC{n:06d}", f"LTM{n:06d}"
        conn.execute("INSERT INTO enrollments VALUES (?,?,?,?,?,?,?)",
                     (ltc, learner_id, c["id"], "ACTIVE", "WITHIN_DEADLINE", _iso(now),
                      _iso(now + timedelta(days=c["deadline_in_days"]))))
        conn.execute("INSERT INTO modules VALUES (?,?,?,?,?,?)",
                     (ltm, ltc, learner_id, c["course_name"], 45, _iso(now)))
    conn.commit()
