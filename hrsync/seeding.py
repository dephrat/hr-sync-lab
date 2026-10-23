"""The one place that knows the two-system seed order.

Ethena is seeded from the workers' pre-change state, then the HRIS job changes are applied.
That way the first sync finds real drift (the promotion, the flipped country) to fix.
"""
from pathlib import Path

from ethena import db as ethena_db, seed as ethena_seed
from hris import db as hris_db, seed as hris_seed


def seed_all(hris_path, ethena_path, rng_seed=7) -> dict:
    """Seed fresh HRIS and Ethena databases at the given paths (existing files are replaced).
    Returns the Ethena mismatch facts plus `plans`, the HRIS job changes as planned."""
    for p in (hris_path, ethena_path):
        Path(p).unlink(missing_ok=True)
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    h = hris_db.connect(hris_path)
    e = ethena_db.connect(ethena_path)
    try:
        plans = hris_seed.seed_workers(h, rng_seed=rng_seed)
        facts = ethena_seed.seed(e, h, plans, rng_seed=rng_seed)
        e.commit()
        hris_seed.apply_changes(h, plans)
        h.commit()
    finally:
        e.close()
        h.close()
    return {**facts, "plans": plans}
