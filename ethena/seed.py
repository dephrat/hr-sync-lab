"""Seed the mock Ethena from the HRIS workers, with deliberate mismatches for reconciliation.

Run it after hris.seed.seed_workers and before hris.seed.apply_changes (hrsync.seeding does
this), so learners carry each worker's pre-change attributes and the first sync has work to do.

Mismatches (all deterministic for a given HRIS seed):
- 3 HRIS workers with no planned job change are absent from Ethena.
- W-1042's email is on two learners (its own plus an extra learner "Emma Parker-Dup").
- The HRIS Termination worker's learner is still ACTIVE.
- 2 enrollments of unchanged learners are past their deadline and not completed (overdue).
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
     {"customCategory": ["WAREHOUSE"]}),
]


def seed(conn, hris_conn, plans=None, rng_seed=7) -> dict:
    """Populate Ethena; returns the chosen mismatches (also useful for tests). `plans` is the
    list from hris.seed.seed_workers; without it the HRIS job_changes table is used."""
    rng = random.Random(rng_seed)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda d: (now + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for cid, course, name, aud in CAMPAIGNS:
        conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?)",
                     (cid, course, name, "ACTIVE", stamp(-90), stamp(275), 30, json.dumps(aud)))

    workers = hris_conn.execute("SELECT * FROM workers ORDER BY id").fetchall()
    if plans is None:
        plans = [{"worker_id": r[0], "reason": r[1], } for r in
                 hris_conn.execute("SELECT worker_id, reason FROM job_changes ORDER BY seq")]
    changed = {p["worker_id"] for p in plans}
    terminated = sorted(p["worker_id"] for p in plans if p["reason"] == "Termination")
    plain = [w["id"] for w in workers if w["id"] not in changed and w["id"] != DUP_WORKER]
    # Vancouver workers queue anyway, so keep the three absent ones clear of them.
    can_create = [w["id"] for w in workers if w["id"] in plain and w["location"] != "Vancouver, BC"]
    absent = sorted(rng.sample(can_create, 3))
    flipped: list[str] = []  # the country mismatch was retired; the key stays for callers

    learners = {}
    for i, w in enumerate(w for w in workers if w["id"] not in absent):
        lid = f"L{100001 + i}"
        country, state, cat = LOCATION_MAP[w["location"]]
        if w["id"] in flipped:
            country, state = ("USA", "MN") if country == "CAN" else ("CAN", None)
        if w["job_profile"].startswith("Warehouse"):
            cat = "WAREHOUSE"
        learners[w["id"]] = lid
        conn.execute(
            "INSERT INTO learners (id,name,email,status,country,state,is_manager,department,"
            "worker_type,custom_category,language,training_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, w["descriptor"], w["email"], "ACTIVE", country, state, w["is_manager"],
             w["department"], "Employee", cat, "en", f"https://training.example.com/{lid}"))
    dup_id = "L109001"
    dup_email = hris_conn.execute("SELECT email FROM workers WHERE id=?", (DUP_WORKER,)).fetchone()[0]
    conn.execute(
        "INSERT INTO learners (id,name,email,status,country,state,is_manager,department,"
        "worker_type,custom_category,language,training_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (dup_id, "Emma Parker-Dup", dup_email, "ACTIVE", "CAN", None, 0, "Sales", "Employee",
         "ON", "en", f"https://training.example.com/{dup_id}"))
    conn.commit()
    for lid in [*learners.values(), dup_id]:
        enroll(conn, lid)
    overdue_learners = [learners[w] for w in plain if w not in absent and w not in flipped][:2]
    overdue = []
    for lid in overdue_learners:
        ltc = conn.execute("SELECT id FROM enrollments WHERE learner_id=? ORDER BY id LIMIT 1",
                           (lid,)).fetchone()[0]
        conn.execute("UPDATE enrollments SET enrolled_at=?, deadline=?,"
                     " completion_status='PAST_DEADLINE' WHERE id=?", (stamp(-45), stamp(-15), ltc))
        overdue.append(ltc)
    conn.commit()
    return {"absent_workers": absent, "duplicate_email": dup_email,
            "duplicate_learner_ids": [learners[DUP_WORKER], dup_id],
            "country_mismatch_workers": flipped,
            "country_mismatch_learner_ids": [learners[w] for w in flipped],
            "terminated_active_workers": terminated, "overdue_enrollment_ids": overdue,
            "terminated_active_learner_ids": [learners[w] for w in terminated if w in learners]}


if __name__ == "__main__":
    from hris.db import connect as hris_connect
    out = sys.argv[1] if len(sys.argv) > 1 else "ethena.db"
    src = sys.argv[2] if len(sys.argv) > 2 else "hris.db"
    print(json.dumps(seed(connect(out), hris_connect(src)), indent=2))
