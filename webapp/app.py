"""The demo web app: static page, JSON endpoints, and both mock systems mounted in-process."""
import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from hrsync import store
from hrsync.config import Settings
from hrsync.seeding import bad_day, seed_all, summary
from reconcile import report
from webapp import api
from webapp.clients import make_bundle, reset_rules

STATIC = Path(__file__).with_name("static")


def _count(path, table) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _asset_version() -> str:
    """Short hash of the page's JS and CSS; changes whenever either file changes."""
    h = hashlib.sha1()
    for name in ("app.js", "style.css"):
        h.update((STATIC / name).read_bytes())
    return h.hexdigest()[:10]


def create_app(settings: Settings) -> FastAPI:
    bundle = make_bundle(settings)
    app = FastAPI(title="hr-sync-lab demo", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bundle = bundle

    @app.exception_handler(RequestValidationError)
    def invalid(request, exc):
        first = exc.errors()[0]
        where = ".".join(str(x) for x in first["loc"] if x not in ("body", "query", "path"))
        if first.get("type") == "json_invalid":
            msg = "the request body is not valid JSON"
        elif first.get("type") == "missing":
            msg = f"missing field: {where}"
        else:
            text = str(first["msg"]).removeprefix("Value error, ")
            msg = f"{where}: {text}" if where else text
        return JSONResponse({"error": msg}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request, exc):
        # Same {"error": ...} shape as the API's own errors. The mounted mocks have their own
        # handlers and keep their own shapes.
        words = {404: "not found", 405: "method not allowed"}
        detail = exc.detail if isinstance(exc.detail, str) else None
        msg = words.get(exc.status_code, detail or "error")
        return JSONResponse({"error": msg}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    app.include_router(api.router)
    app.mount("/workday", bundle.hris_app)
    app.mount("/ethena", bundle.ethena_app)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        # Stamp the asset URLs with a content hash so browsers (Safari especially) fetch new
        # JS and CSS after every deploy without a hard reload. The HTML itself is never cached.
        html = (STATIC / "index.html").read_text().replace("__ASSET_VERSION__", _asset_version())
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

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
            reset_rules(settings)
            return {"ok": True, "workers": _count(settings.hris_db, "workers"),
                    "learners": _count(settings.ethena_db, "learners"),
                    "changes": _count(settings.hris_db, "job_changes")}

    @app.post("/api/bad_day")
    def inject_bad_day():
        with api.WRITE_LOCK:
            return summary(bad_day(settings.hris_db, settings.ethena_db))

    @app.get("/api/report")
    def get_report():
        now = datetime.now(UTC)
        return {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sections": report.sections(settings.hris_db, settings.ethena_db,
                                            settings.sync_db, now)}

    return app
