"""Deterministic fake data for the mock HRIS. Names come from fixed lists; emails are @example.com."""
import random
import sys
from datetime import datetime, timedelta, timezone

from hris.db import (JOB_PROFILES, LOCATIONS, PROFILE_DEPARTMENT,
                     apply_job_change, connect, is_manager_profile, profile_descriptor)

FIRST_NAMES = ["Ava", "Liam", "Noah", "Emma", "Olivia", "Ethan", "Mia", "Lucas", "Sofia",
               "Mason", "Chloe", "Logan", "Zoe", "Owen", "Nora", "Caleb", "Ruby", "Isaac",
               "Hana", "Marcus"]
LAST_NAMES = ["Tremblay", "Nguyen", "Okafor", "Silva", "Kowalski", "Haddad", "Patel",
              "Lindqvist", "Moreau", "Chen", "Rossi", "Abara", "Fischer", "Park", "Dubois"]


PROMOTED_PROFILE = "Sales_Team_Lead"
DUP_WORKER = "W-1042"  # ethena.seed puts this worker's email on two learners
VANCOUVER = "Vancouver, BC"  # not in the rules table; exactly N_VANCOUVER workers start there
N_VANCOUVER = 2


def _location(rng, loc_rng, others):
    """The main stream draws as before; a Vancouver draw is swapped for another location."""
    loc = rng.choice(LOCATIONS)
    return loc if loc != VANCOUVER else loc_rng.choice(others)


def seed_workers(conn, n_workers=100, rng_seed=7) -> list[dict]:
    """Insert the workers in their pre-change state and return the 12 planned job changes
    (apply them with apply_changes). Callers that need the pre-change state (ethena.seed)
    run between the two calls."""
    rng = random.Random(rng_seed)
    loc_rng = random.Random(rng_seed + 1000)  # side stream, so the main draws stay put
    others = [loc for loc in LOCATIONS if loc != VANCOUVER]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for i in range(n_workers):
        wid = f"W-{1001 + i}"
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        profile = rng.choice(JOB_PROFILES)
        hire = (now - timedelta(days=rng.randint(60, 2500))).date().isoformat()
        conn.execute(
            "INSERT INTO workers (id, descriptor, email, business_title, is_manager,"
            " hire_date, job_profile, location, department, position_id, status_date)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wid, f"{first} {last}", f"{first}.{last}.{1001 + i}@example.com".lower(),
             profile_descriptor(profile), int(is_manager_profile(profile)), hire, profile,
             _location(rng, loc_rng, others), PROFILE_DEPARTMENT[profile], f"P-{1001 + i}", hire))
    conn.commit()

    workers = conn.execute("SELECT id, job_profile, location FROM workers ORDER BY id").fetchall()
    # The promotion is the README's story: a Toronto Sales_Rep becomes a Sales_Team_Lead.
    promoted = next(w["id"] for w in workers if w["id"] != DUP_WORKER
                    and w["job_profile"] == "Sales_Rep" and w["location"] == "Toronto, ON")
    pool = [w["id"] for w in workers if w["id"] not in (DUP_WORKER, promoted)]
    ids = rng.sample(pool, 10)
    # Exactly two workers start in Vancouver, none of them with a planned change.
    quiet = [w["id"] for w in workers if w["id"] not in (DUP_WORKER, promoted, *ids)]
    for wid in sorted(loc_rng.sample(quiet, N_VANCOUVER)):
        conn.execute("UPDATE workers SET location=? WHERE id=?", (VANCOUVER, wid))
    conn.commit()
    plans = [
        (ids[0], "Hire", {}),
        (promoted, "Promotion", {"job_profile": PROMOTED_PROFILE}),
        (ids[1], "Transfer", {"location": "Vancouver, BC"}),
        (ids[2], "Termination", {}),
        (DUP_WORKER, "Data_Change", {"business_title": "Senior Associate"}),
    ]
    for wid in ids[3:10]:
        reason = rng.choice(["Promotion", "Transfer", "Data_Change"])
        if reason == "Promotion":
            plans.append((wid, reason, {"job_profile": rng.choice(JOB_PROFILES)}))
        elif reason == "Transfer":
            plans.append((wid, reason, {"location": rng.choice(others)}))
        else:
            plans.append((wid, reason, {"business_title": f"Senior {rng.choice(['Associate', 'Specialist'])}"}))
    out = []
    for i, (wid, reason, fields) in enumerate(plans):
        completed = now - timedelta(days=29 - i * 2.4)
        if "job_profile" in fields:
            fields["department"] = PROFILE_DEPARTMENT[fields["job_profile"]]
        out.append({"worker_id": wid, "reason": reason, "fields": fields,
                    "completed": completed})
    return out


def apply_changes(conn, plans: list[dict]) -> None:
    """Record and apply the planned job changes (see seed_workers)."""
    for p in plans:
        completed, wid = p["completed"], p["worker_id"]
        if p["reason"] == "Hire":
            conn.execute("UPDATE workers SET hire_date=? WHERE id=?",
                         (completed.date().isoformat(), wid))
        apply_job_change(conn, worker_id=wid, effective_date=completed.date().isoformat(),
                         reason=p["reason"],
                         completed_date=completed.strftime("%Y-%m-%dT%H:%M:%SZ"), **p["fields"])


def seed(conn, n_workers=100, rng_seed=7) -> list[dict]:
    """Workers plus applied changes, for an HRIS on its own. hrsync.seeding.seed_all is the
    two-system seed that captures Ethena before the changes land."""
    plans = seed_workers(conn, n_workers, rng_seed)
    apply_changes(conn, plans)
    return plans


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "hris.db"
    c = connect(path)
    seed(c)
    print(f"seeded {path}")
