"""Cross-system drift report over the HRIS, Ethena and sync-store databases."""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from ethena.db import audience_matches
from hrsync import store

QUERIES = Path(__file__).with_name("queries.sql")

# (query name, heading, column headers are taken from the result)
SECTIONS = [
    ("overdue_by_department", "Overdue training by department"),
    ("in_hris_not_ethena", "In HRIS, not in Ethena"),
    ("in_ethena_not_hris", "In Ethena, not in HRIS"),
    ("terminated_but_active", "Terminated in HRIS but active in Ethena"),
    ("stale_enrollments", "Stale enrollments"),
    ("queue_age", "Review queue age"),
]


def load_queries() -> dict[str, str]:
    parts = re.split(r"^-- name: (\w+)\s*$", QUERIES.read_text(), flags=re.M)
    return {name: sql.strip() for name, sql in zip(parts[1::2], parts[2::2])}


def _stale_enrollments(conn, queries) -> tuple[list[str], list[tuple]]:
    """Enrollments whose campaign audience no longer matches the learner's attributes.
    Uses the mock's campaign `audience` column; a real tenant would need the campaign
    audience exported, as the Ethena API does not expose it."""
    rows = []
    for r in conn.execute(queries["enrollments_with_audience"]):
        if not audience_matches(json.loads(r["audience"]), r):
            rows.append((r["enrollment_id"], r["learner_id"], r["campaign_id"],
                         r["campaign_name"]))
    return ["enrollment_id", "learner_id", "campaign_id", "campaign_name"], rows


def _table(headers, rows) -> str:
    if not rows:
        return "_none_"
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def build(hris_db, ethena_db, sync_db, now: datetime) -> str:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    queries = load_queries()
    store.connect(sync_db).close()  # a never-run sync has no tables yet
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for alias, path in (("hris", hris_db), ("ethena", ethena_db), ("sync", sync_db)):
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(path),))
    out = [f"# HR sync reconciliation, {stamp}"]
    for name, title in SECTIONS:
        if name == "stale_enrollments":
            headers, rows = _stale_enrollments(conn, queries)
        else:
            cur = conn.execute(queries[name], {"now": stamp} if ":now" in queries[name] else {})
            headers = [d[0] for d in cur.description]
            rows = [tuple(r) for r in cur.fetchall()]
        out.append(f"## {title}\n\nCount: {len(rows)}\n\n{_table(headers, rows)}")
    conn.close()
    return "\n\n".join(out) + "\n"
