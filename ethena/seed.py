"""Seed the mock Ethena from the HRIS workers, with deliberate mismatches for reconciliation.

Mismatches (all deterministic for a given HRIS seed):
- 6 HRIS workers with no job change are absent from Ethena.
- W-1042's email is on two learners (its own plus an extra learner "Emma Parker-Dup").
- 3 learners have country flipped CAN<->USA vs their HRIS location.
- The HRIS Termination worker's learner is still ACTIVE.
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


def seed(conn, hris_conn, rng_seed=7) -> dict:
    """Populate Ethena; returns the chosen mismatches (also useful for tests)."""
    rng = random.Random(rng_seed)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda d: (now + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for cid, course, name, aud in CAMPAIGNS:
        conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?)",
                     (cid, course, name, "ACTIVE", stamp(-90), stamp(275), 30, json.dumps(aud)))

    workers = hris_conn.execute("SELECT * FROM workers ORDER BY id").fetchall()
    changed = {r[0] for r in hris_conn.execute("SELECT DISTINCT worker_id FROM job_changes")}
    terminated = [r[0] for r in hris_conn.execute(
        "SELECT w.id FROM workers w JOIN job_changes j ON j.worker_id=w.id"
        " WHERE j.reason='Termination' ORDER BY w.id")]
    plain = [w["id"] for w in workers if w["id"] not in changed and w["id"] != DUP_WORKER]
    absent = sorted(rng.sample(plain, 6))
    flipped = sorted(rng.sample([i for i in plain if i not in absent], 3))

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
    return {"absent_workers": absent, "duplicate_email": dup_email,
            "duplicate_learner_ids": [learners[DUP_WORKER], dup_id],
            "country_mismatch_workers": flipped,
            "country_mismatch_learner_ids": [learners[w] for w in flipped],
            "terminated_active_workers": terminated,
            "terminated_active_learner_ids": [learners[w] for w in terminated if w in learners]}


if __name__ == "__main__":
    from hris.db import connect as hris_connect
    out = sys.argv[1] if len(sys.argv) > 1 else "ethena.db"
    src = sys.argv[2] if len(sys.argv) > 2 else "hris.db"
    print(json.dumps(seed(connect(out), hris_connect(src)), indent=2))
