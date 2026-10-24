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
    audience TEXT NOT NULL DEFAULT '[{}]'
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


def normalize_audience(audience) -> list[dict]:
    """An audience is a list of groups (OR). The old single-dict form {...} reads as [{...}]."""
    if isinstance(audience, dict):
        return [audience]
    return list(audience or [])


def _group_matches(group: dict, learner) -> bool:
    """Every key present in the group must match; {} matches everyone."""
    if "isManager" in group and bool(learner["is_manager"]) != group["isManager"]:
        return False
    for key, col in (("country", "country"), ("state", "state"),
                     ("customCategory", "custom_category"), ("department", "department")):
        if key in group and learner[col] not in group[key]:
            return False
    return True


def audience_matches(audience, learner) -> bool:
    """A learner matches if they match ANY group; a group matches if every key matches.
    [] matches nobody, [{}] matches everyone, a bare dict is read as a single group."""
    return any(_group_matches(g, learner) for g in normalize_audience(audience))


def enroll(conn, learner_id: str) -> None:
    """Ensure an enrollment (+ module) exists for each ACTIVE campaign matching the learner.
    Enrollment rows are never deleted. Unfinished ones are marked REMOVED (the module reads
    SYSTEM_REMOVED, mirroring that status in Ethena's API) when the learner is TERMINATED or
    DEACTIVATED, or when the learner stops matching the campaign's audience or the campaign is
    no longer ACTIVE. Completed ones stay as records. The exact real-world trigger for
    SYSTEM_REMOVED is an assumption. Matching REMOVED enrollments are reopened with a new
    deadline when the learner matches again."""
    learner = conn.execute("SELECT * FROM learners WHERE id=?", (learner_id,)).fetchone()
    if learner is None:
        return
    if learner["status"] != "ACTIVE":
        conn.execute("UPDATE enrollments SET completion_status='REMOVED' "
                     "WHERE learner_id=? AND completion_status != 'COMPLETED'", (learner_id,))
        conn.commit()
        return
    now = datetime.now(timezone.utc).replace(microsecond=0)
    campaigns = conn.execute("SELECT * FROM campaigns ORDER BY id").fetchall()
    keep = {c["id"] for c in campaigns
            if c["status"] == "ACTIVE" and audience_matches(json.loads(c["audience"]), learner)}
    for e in conn.execute("SELECT id, campaign_id FROM enrollments WHERE learner_id=? "
                          "AND completion_status NOT IN ('COMPLETED','REMOVED')",
                          (learner_id,)).fetchall():
        if e["campaign_id"] not in keep:
            conn.execute("UPDATE enrollments SET completion_status='REMOVED' WHERE id=?",
                         (e["id"],))
    for c in campaigns:
        if c["id"] not in keep:
            continue
        existing = conn.execute("SELECT id, completion_status FROM enrollments "
                                "WHERE learner_id=? AND campaign_id=?",
                                (learner_id, c["id"])).fetchone()
        if existing:
            if existing["completion_status"] == "REMOVED":
                conn.execute("UPDATE enrollments SET completion_status='WITHIN_DEADLINE', "
                             "enrolled_at=?, deadline=? WHERE id=?",
                             (_iso(now), _iso(now + timedelta(days=c["deadline_in_days"])),
                              existing["id"]))
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
