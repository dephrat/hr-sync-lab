"""The one place that knows the two-system seed order.

seed_all builds the clean state: Ethena matches every HRIS worker, so there is no drift.
bad_day then breaks it the way a messy Monday would: job changes land in the HRIS only,
and Ethena picks up a duplicate account, missing people and overdue training. It never
touches sync.db; the drift appears because the two systems diverged.
"""
from pathlib import Path

from ethena import db as ethena_db, seed as ethena_seed
from hris import db as hris_db, seed as hris_seed

EMPTY_FACTS = {"absent_workers": [], "deleted_learner_ids": [], "duplicate_email": None,
               "duplicate_learner_ids": [], "country_mismatch_workers": [],
               "country_mismatch_learner_ids": [], "terminated_active_workers": [],
               "overdue_enrollment_ids": [], "terminated_active_learner_ids": [],
               "plans": [], "vancouver_workers": [], "changed_workers": []}


def seed_all(hris_path, ethena_path, rng_seed=7) -> dict:
    """Seed fresh, matching HRIS and Ethena databases (existing files are replaced).
    Returns the facts dict with every list empty: nothing is planted."""
    for p in (hris_path, ethena_path):
        Path(p).unlink(missing_ok=True)
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    h = hris_db.connect(hris_path)
    e = ethena_db.connect(ethena_path)
    try:
        hris_seed.seed_workers(h, rng_seed=rng_seed)
        ethena_seed.seed(e, h, rng_seed=rng_seed)
    finally:
        e.close()
        h.close()
    return {k: (list(v) if isinstance(v, list) else v) for k, v in EMPTY_FACTS.items()}


def bad_day(hris_path, ethena_path, rng_seed=7) -> dict:
    """Inject the demo's bad day into a clean pair. A second call is a no-op returning
    {"already": True}. The marker is a demo_flags row written by this function, so a
    visitor's own hires and job changes do not block it; Reset recreates the database
    and clears the flag."""
    h = hris_db.connect(hris_path)
    e = ethena_db.connect(ethena_path)
    try:
        h.execute("CREATE TABLE IF NOT EXISTS demo_flags (name TEXT PRIMARY KEY)")
        if h.execute("SELECT 1 FROM demo_flags WHERE name='bad_day'").fetchone():
            return {"already": True}
        h.execute("INSERT INTO demo_flags VALUES ('bad_day')")
        # Plan against the seeded population, not the current count, so a visitor's
        # hires do not change which workers the bad day picks.
        n = h.execute("SELECT COUNT(*) FROM workers WHERE id <= 'W-1100'").fetchone()[0]
        plans, vancouver = hris_seed.plan_bad_day(n, rng_seed)
        changed = {p["worker_id"] for p in plans}
        terminated = [p["worker_id"] for p in plans if p["reason"] == "Termination"]
        # Absent picks stay clear of the Vancouver movers, who queue anyway.
        facts = ethena_seed.bad_day(e, h, changed, terminated, avoid=set(vancouver),
                                    rng_seed=rng_seed)
        hris_seed.move_to_vancouver(h, vancouver)
        hris_seed.apply_changes(h, plans)
        h.commit()
    finally:
        e.close()
        h.close()
    return {**facts, "plans": plans, "vancouver_workers": vancouver,
            "changed_workers": sorted(changed)}


def summary(facts: dict) -> dict:
    """The short counts the web button and CLI print."""
    if facts.get("already"):
        return {"already": True}
    return {"changes": len(facts["plans"]), "duplicates": len(facts["duplicate_learner_ids"]) - 1,
            "missing": len(facts["deleted_learner_ids"]),
            "overdue": len(facts["overdue_enrollment_ids"])}
