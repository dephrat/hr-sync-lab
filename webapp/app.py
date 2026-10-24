"""The demo web app: static page, JSON endpoints, and both mock systems mounted in-process."""
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from hrsync import store
from hrsync.config import Settings
from hrsync.seeding import seed_all
from reconcile import report
from webapp import api
from webapp.clients import make_bundle

STATIC = Path(__file__).with_name("static")


def _count(path, table) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def create_app(settings: Settings) -> FastAPI:
    bundle = make_bundle(settings)
    app = FastAPI(title="hr-sync-lab demo", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bundle = bundle

    @app.exception_handler(RequestValidationError)
    def invalid(request, exc):
        first = exc.errors()[0]
        where = ".".join(str(x) for x in first["loc"] if x != "body")
        return JSONResponse({"error": f"invalid {where}: {first['msg']}"}, status_code=422)
    app.include_router(api.router)
    app.mount("/workday", bundle.hris_app)
    app.mount("/ethena", bundle.ethena_app)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/api/state")
    def state():
        conn = store.connect(settings.sync_db)
        try:
            last = store.last_run(conn)
            open_queue = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status='open'").fetchone()[0]
            return {"last_run": last, "watermark": store.get_watermark(conn),
                    "open_queue": open_queue,
                    "failed_last_run": last["failed"] if last else 0}
        finally:
            conn.close()

    @app.post("/api/reset")
    def reset():
        with api.WRITE_LOCK:
            Path(settings.sync_db).unlink(missing_ok=True)
            seed_all(settings.hris_db, settings.ethena_db)
            return {"ok": True, "workers": _count(settings.hris_db, "workers"),
                    "learners": _count(settings.ethena_db, "learners"),
                    "changes": _count(settings.hris_db, "job_changes")}

    @app.get("/api/report")
    def get_report():
        now = datetime.now(UTC)
        return {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sections": report.sections(settings.hris_db, settings.ethena_db,
                                            settings.sync_db, now)}

    return app
