"""Cross-system drift report over the HRIS, Ethena and sync-store databases."""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from ethena.db import audience_matches
from hrsync import store

QUERIES = Path(__file__).with_name("queries.sql")

# (query name, heading, plain column headers in result order). Names are stable keys.
SECTIONS = [
    ("overdue_by_department", "Overdue training by department", ["Department", "Overdue courses"]),
    ("in_hris_not_ethena", "In Workday, not in Ethena", ["Worker", "Name", "Email", "Department"]),
    ("in_ethena_not_hris", "In Ethena, not in Workday", ["Account", "Name", "Email", "Department"]),
    ("terminated_but_active", "Left in Workday but active in Ethena",
     ["Worker", "Account", "Email", "Left on"]),
    ("duplicate_accounts", "Duplicate Ethena accounts",
     ["Account", "Name", "Email", "Worker", "Account the sync uses"]),
    ("stale_enrollments", "Stale enrollments", ["Enrollment", "Account", "Course id", "Course"]),
    ("queue_age", "Review queue age", ["Item", "Worker", "Problem", "Waiting"]),
]

# Same wording as the demo page's review queue.
REASON_LABEL = {"UNKNOWN_LOCATION": "Courses not specified for this office",
                "UNKNOWN_PROFILE": "Courses not specified for this job",
                "AMBIGUOUS_LEARNER": "Two Ethena accounts share this email",
                "NO_LEARNER_MATCH": "No Ethena account for this person"}


def _age(days) -> str:
    days = int(days or 0)
    return "today" if days <= 0 else "1 day" if days == 1 else f"{days} days"


def load_queries() -> dict[str, str]:
    parts = re.split(r"^-- name: (\w+)\s*$", QUERIES.read_text(), flags=re.M)
    return {name: sql.strip() for name, sql in zip(parts[1::2], parts[2::2])}


def _stale_enrollments(conn, queries) -> tuple[list[str], list[tuple]]:
    """Unfinished, not-removed enrollments whose campaign audience no longer matches the
    learner's attributes. Ethena removes those itself, so this should normally be empty.
    Uses the mock's campaign `audience` column; a real tenant would need the campaign
    audience exported, as the Ethena API does not expose it."""
    rows = []
    for r in conn.execute(queries["enrollments_with_audience"]):
        if r["completion_status"] in ("COMPLETED", "REMOVED"):
            continue
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


def sections(hris_db, ethena_db, sync_db, now: datetime) -> list[dict]:
    """The reconciliation sections as structured rows, in SECTIONS order. Writes nothing
    except creating the empty sync schema when the sync database does not exist yet."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    queries = load_queries()
    store.connect(sync_db).close()  # a never-run sync has no tables yet
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for alias, path in (("hris", hris_db), ("ethena", ethena_db), ("sync", sync_db)):
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(path),))
    out = []
    for name, title, headers in SECTIONS:
        if name == "stale_enrollments":
            _, rows = _stale_enrollments(conn, queries)
        else:
            cur = conn.execute(queries[name], {"now": stamp} if ":now" in queries[name] else {})
            rows = [tuple(r) for r in cur.fetchall()]
        if name == "queue_age":
            rows = [(i, w, REASON_LABEL.get(r, r), _age(d)) for i, w, r, d in rows]
        out.append({"name": name, "title": title, "headers": headers,
                    "rows": [list(r) for r in rows]})
    conn.close()
    return out


def build(hris_db, ethena_db, sync_db, now: datetime) -> str:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = [f"# HR sync reconciliation, {stamp}"]
    for s in sections(hris_db, ethena_db, sync_db, now):
        out.append(f"## {s['title']}\n\nCount: {len(s['rows'])}\n\n"
                   f"{_table(s['headers'], s['rows'])}")
    return "\n\n".join(out) + "\n"
