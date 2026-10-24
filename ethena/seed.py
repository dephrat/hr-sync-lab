"""Seed the mock Ethena from the HRIS workers, then (on the demo's bad day) break it.

seed() is the clean state: one learner per worker with the attributes the rules expect, and
exactly the enrollments the campaigns' audiences produce. bad_day() adds the mismatches
reconciliation is for (all deterministic for a given HRIS seed):
- 3 HRIS workers with no job change lose their learner (never loaded into Ethena).
- W-1042's email goes on a second learner, "Emma Parker-Dup".
- 2 enrollments of unchanged learners get a deadline in the past while still incomplete.
The terminated worker's learner stays ACTIVE because only the HRIS side changes.
"""
import json
import random
import sys
from datetime import datetime, timedelta, timezone

from ethena.db import connect, enroll

DUP_WORKER = "W-1042"
LOCATION_MAP = {  # HRIS location -> (country, state, customCategory)
    "Toronto, ON": ("CAN", None, "ON"), "Vancouver, BC": ("CAN", None, "BC"),
    "Remote - Canada": ("CAN", None, "ON"), "Minneapolis, MN": ("USA", "MN", None),
    "New York, NY": ("USA", "NY", None), "Austin, TX": ("USA", "TX", None),
    "Remote - US": ("USA", "MN", None),
}
CAMPAIGNS = [  # (id, course, name, audience)
    ("C000001", "Harassment Prevention", "Harassment Prevention (Ontario)",
     {"country": ["CAN"], "customCategory": ["ON"]}),
    ("C000002", "Harassment Prevention", "Harassment Prevention (US Managers)",
     {"country": ["USA"], "isManager": True}),
    ("C000003", "Harassment Prevention", "Harassment Prevention (US Employees)",
     {"country": ["USA"], "isManager": False}),
    ("C000004", "Manager Fundamentals", "Manager Fundamentals", {"isManager": True}),
    ("C000005", "Data Privacy Essentials", "Data Privacy Essentials", {}),
    ("C000006", "Workplace Safety", "Workplace Safety (Warehouse)",
     {"department": ["Operations"]}),
]


def _insert_learner(conn, lid, name, email, country, state, is_manager, department, cat):
    conn.execute(
        "INSERT INTO learners (id,name,email,status,country,state,is_manager,department,"
        "worker_type,custom_category,language,training_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (lid, name, email, "ACTIVE", country, state, is_manager, department, "Employee", cat,
         "en", f"https://training.example.com/{lid}"))


def seed(conn, hris_conn, rng_seed=7) -> dict:
    """Populate a clean Ethena that matches every HRIS worker. Returns {worker_id: learner_id}."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda d: (now + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for cid, course, name, aud in CAMPAIGNS:
        conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?)",
                     (cid, course, name, "ACTIVE", stamp(-90), stamp(275), 30, json.dumps(aud)))
    learners = {}
    for i, w in enumerate(hris_conn.execute("SELECT * FROM workers ORDER BY id").fetchall()):
        lid = f"L{100001 + i}"
        country, state, cat = LOCATION_MAP[w["location"]]
        learners[w["id"]] = lid
        _insert_learner(conn, lid, w["descriptor"], w["email"], country, state,
                        w["is_manager"], w["department"], cat)
    conn.commit()
    for lid in learners.values():
        enroll(conn, lid)
    conn.commit()
    return learners


def bad_day(conn, hris_conn, changed, terminated, avoid=(), rng_seed=7) -> dict:
    """Break a clean Ethena (see the module docstring). `changed` are the workers with a
    bad-day job change; `avoid` are other workers to keep the absent picks clear of."""
    rng = random.Random(rng_seed)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda d: (now + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    emails = {r["email"].lower(): r["id"] for r in conn.execute("SELECT id, email FROM learners")}
    workers = hris_conn.execute("SELECT id, email FROM workers ORDER BY id").fetchall()
    learner_of = {w["id"]: emails[w["email"].lower()] for w in workers
                  if w["email"].lower() in emails}
    plain = [w["id"] for w in workers
             if w["id"] not in changed and w["id"] != DUP_WORKER and w["id"] in learner_of]
    absent = sorted(rng.sample([w for w in plain if w not in avoid], 3))
    for wid in absent:
        lid = learner_of[wid]
        for t in ("modules", "enrollments"):
            conn.execute(f"DELETE FROM {t} WHERE learner_id=?", (lid,))
        conn.execute("DELETE FROM learners WHERE id=?", (lid,))
    dup_id = "L109001"
    dup_email = hris_conn.execute("SELECT email FROM workers WHERE id=?", (DUP_WORKER,)).fetchone()[0]
    _insert_learner(conn, dup_id, "Emma Parker-Dup", dup_email, "CAN", None, 0, "Sales", "ON")
    conn.commit()
    enroll(conn, dup_id)
    overdue = []
    for wid in [w for w in plain if w not in absent][:2]:
        ltc = conn.execute("SELECT id FROM enrollments WHERE learner_id=? ORDER BY id LIMIT 1",
                           (learner_of[wid],)).fetchone()[0]
        # Status stays WITHIN_DEADLINE: overdue means deadline passed and not completed.
        conn.execute("UPDATE enrollments SET enrolled_at=?, deadline=? WHERE id=?",
                     (stamp(-45), stamp(-15), ltc))
        overdue.append(ltc)
    conn.commit()
    return {"absent_workers": absent, "deleted_learner_ids": [learner_of[w] for w in absent],
            "duplicate_email": dup_email,
            "duplicate_learner_ids": [learner_of[DUP_WORKER], dup_id],
            "country_mismatch_workers": [], "country_mismatch_learner_ids": [],
            "terminated_active_workers": sorted(terminated), "overdue_enrollment_ids": overdue,
            "terminated_active_learner_ids": [learner_of[w] for w in sorted(terminated)
                                              if w in learner_of]}


if __name__ == "__main__":
    from hris.db import connect as hris_connect
    out = sys.argv[1] if len(sys.argv) > 1 else "ethena.db"
    src = sys.argv[2] if len(sys.argv) > 2 else "hris.db"
    print(json.dumps(seed(connect(out), hris_connect(src)), indent=2))
