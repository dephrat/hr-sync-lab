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


def seed(conn, n_workers=100, rng_seed=7) -> None:
    rng = random.Random(rng_seed)
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
             rng.choice(LOCATIONS), PROFILE_DEPARTMENT[profile], f"P-{1001 + i}", hire))
    conn.commit()

    # 12 completed changes on distinct workers, spread over the last ~29 days.
    ids = rng.sample([f"W-{1001 + i}" for i in range(n_workers)], 12)
    plans = [
        ("Hire", {}),
        ("Promotion", {"job_profile": "Sales_Team_Lead"}),
        ("Transfer", {"location": "Vancouver, BC"}),
        ("Termination", {}),
    ]
    for _ in range(8):
        reason = rng.choice(["Promotion", "Transfer", "Data_Change"])
        if reason == "Promotion":
            plans.append((reason, {"job_profile": rng.choice(JOB_PROFILES)}))
        elif reason == "Transfer":
            plans.append((reason, {"location": rng.choice(LOCATIONS)}))
        else:
            plans.append((reason, {"business_title": f"Senior {rng.choice(['Associate', 'Specialist'])}"}))
    for i, (wid, (reason, fields)) in enumerate(zip(ids, plans)):
        completed = now - timedelta(days=29 - i * 2.4)
        if "job_profile" in fields:
            fields["department"] = PROFILE_DEPARTMENT[fields["job_profile"]]
        if reason == "Hire":
            conn.execute("UPDATE workers SET hire_date=? WHERE id=?",
                         (completed.date().isoformat(), wid))
        apply_job_change(conn, worker_id=wid, effective_date=completed.date().isoformat(),
                         reason=reason, completed_date=completed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         **fields)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "hris.db"
    c = connect(path)
    seed(c)
    print(f"seeded {path}")
