"""hrsync command line: seed / serve / run / queue / resolve / report."""
import argparse
import json
import sys
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from hrsync import engine, rules, store
from hrsync.clients import EthenaClient, HrSyncClientError, WorkdayClient
from hrsync.config import Settings


def _build_clients(settings: Settings) -> tuple[WorkdayClient, EthenaClient]:
    hris = httpx.Client(base_url=settings.hris_url)
    ethena = httpx.Client(base_url=settings.ethena_url,
                          auth=("hrsync", settings.ethena_api_key))
    return WorkdayClient(hris), EthenaClient(ethena)


def _table(rows: list[tuple], headers: tuple) -> str:
    cells = [tuple(str(c) for c in r) for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    fmt = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([fmt(headers), *map(fmt, cells)])


def _seed(settings: Settings, args) -> int:
    from ethena import db as ethena_db, seed as ethena_seed
    from hris import db as hris_db, seed as hris_seed

    for p in (settings.hris_db, settings.ethena_db, settings.sync_db):
        Path(p).unlink(missing_ok=True)
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    h = hris_db.connect(settings.hris_db)
    hris_seed.seed(h)
    e = ethena_db.connect(settings.ethena_db)
    ethena_seed.seed(e, h)
    e.commit()
    h.commit()
    n = lambda c, t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"seeded {n(h, 'workers')} workers, {n(e, 'learners')} learners, "
          f"{n(h, 'job_changes')} job changes")
    h.close()
    e.close()
    return 0


def _serve(settings: Settings, args) -> int:
    import uvicorn

    from ethena.app import create_app as ethena_app
    from hris.app import create_app as hris_app

    apps = [(hris_app(settings.hris_db), 8001),
            (ethena_app(settings.ethena_db, api_key=settings.ethena_api_key), 8002)]
    for app, port in apps:
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
    print("serving hris on :8001, ethena on :8002 (Ctrl-C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


def _run(settings: Settings, args) -> int:
    hris, learning = _build_clients(settings)
    conn = store.connect(settings.sync_db)
    summary = engine.run(hris, learning, rules.load(settings.rules_path), conn,
                         create_missing=not args.no_create)
    if args.json:
        print(json.dumps(asdict(summary)))
    else:
        print(_table(list(asdict(summary).items()), ("field", "value")))
    return 1 if summary.failed > 0 else 0


def _queue(settings: Settings, args) -> int:
    conn = store.connect(settings.sync_db)
    where = "" if args.all else " WHERE status='open'"
    now = datetime.now(UTC)
    rows = []
    for id_, reason, worker, detail, created in conn.execute(
            f"SELECT id, reason, worker_id, detail, created_at FROM review_queue{where}"
            " ORDER BY id"):
        age = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).days
        rows.append((id_, reason, worker, detail, f"{age}d"))
    print(_table(rows, ("id", "reason", "worker", "detail", "age")) if rows else "queue empty")
    return 0


def _resolve(settings: Settings, args) -> int:
    conn = store.connect(settings.sync_db)
    item = conn.execute("SELECT change_id, worker_id, status FROM review_queue WHERE id=?",
                        (args.id,)).fetchone()
    if item is None or item[2] != "open":
        print(f"queue item {args.id} not found or not open", file=sys.stderr)
        return 2
    change_id, worker_id, _ = item
    now = datetime.now(UTC)
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    note = f" {args.note}" if args.note else ""
    if args.skip:
        conn.execute("UPDATE review_queue SET status='skipped', resolved_at=?, resolution=?"
                     " WHERE id=?", (at, f"skipped:{args.note}", args.id))
        conn.commit()
        print(f"item {args.id} skipped")
        return 0
    hris, learning = _build_clients(settings)
    try:
        learning.get_learner(args.map_learner)
    except HrSyncClientError as exc:
        print(f"cannot map to {args.map_learner}: {exc}", file=sys.stderr)
        return 2
    store.map_learner(conn, worker_id, args.map_learner, "manual", at)
    conn.execute("UPDATE review_queue SET status='resolved', resolved_at=?, resolution=?"
                 " WHERE id=?", (at, f"mapped:{args.map_learner}{note}", args.id))
    conn.commit()
    try:
        change = hris.get_job_change(change_id)
    except HrSyncClientError as exc:
        print(f"mapped, but could not reload change {change_id}: {exc}", file=sys.stderr)
        return 2
    outcome = engine.process_change(hris, learning, rules.load(settings.rules_path), conn,
                                    change, engine.new_run_id(now), now, create_missing=True, force=True)
    print(f"item {args.id} resolved; change {change_id}: {outcome}")
    return 0


def _report(settings: Settings, args) -> int:
    from reconcile import report

    md = report.build(settings.hris_db, settings.ethena_db, settings.sync_db, datetime.now(UTC))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(md)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hrsync")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed").set_defaults(fn=_seed)
    sub.add_parser("serve").set_defaults(fn=_serve)
    r = sub.add_parser("run")
    r.add_argument("--no-create", action="store_true")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=_run)
    q = sub.add_parser("queue")
    q.add_argument("--all", action="store_true")
    q.set_defaults(fn=_queue)
    s = sub.add_parser("resolve")
    s.add_argument("id", type=int)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--map-learner")
    g.add_argument("--skip", action="store_true")
    s.add_argument("--note", default="")
    s.set_defaults(fn=_resolve)
    rp = sub.add_parser("report")
    rp.add_argument("--out", default="data/report.md")
    rp.set_defaults(fn=_report)
    args = p.parse_args(argv)
    return args.fn(Settings.from_env(), args)


if __name__ == "__main__":
    sys.exit(main())
