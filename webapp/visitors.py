"""Personal links: each invited visitor gets their own copy of the demo.

`.../tasha/` serves the same page as `.../`, backed by Tasha's own Workday, Ethena and sync
databases, so nothing one visitor does shows up for another. Names are invited by listing
them in `visitors.txt` next to the main databases (one per line); any other name is a 404,
so a stranger cannot create copies by inventing names. A copy is created and seeded clean
on its first visit."""
import re
import threading
from dataclasses import replace
from pathlib import Path

from starlette.responses import JSONResponse

from hrsync.config import Settings
from hrsync.seeding import seed_all

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
RESERVED = {"api", "static", "workday", "ethena"}


def data_dir(settings: Settings) -> Path:
    return Path(settings.sync_db).parent


def invited(settings: Settings) -> list[str]:
    f = data_dir(settings) / "visitors.txt"
    if not f.exists():
        return []
    names = (line.strip().lower() for line in f.read_text().splitlines())
    return [n for n in names if NAME.match(n) and n not in RESERVED]


def visitor_settings(settings: Settings, name: str) -> Settings:
    d = data_dir(settings) / "visitors" / name
    return replace(settings, sync_db=str(d / "sync.db"), hris_db=str(d / "hris.db"),
                   ethena_db=str(d / "ethena.db"))


class Site:
    """ASGI app: `/<invited name>/...` goes to that visitor's copy, anything else to the
    main copy."""

    def __init__(self, settings: Settings, make_app):
        self.settings, self.make_app = settings, make_app
        self.main = make_app(settings)
        self.copies: dict[str, object] = {}
        self.lock = threading.Lock()

    def _copy(self, name: str):
        with self.lock:
            if name not in self.copies:
                s = visitor_settings(self.settings, name)
                if not Path(s.hris_db).exists():
                    Path(s.hris_db).parent.mkdir(parents=True, exist_ok=True)
                    seed_all(s.hris_db, s.ethena_db)
                self.copies[name] = self.make_app(s)
            return self.copies[name]

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            first = scope["path"].lstrip("/").partition("/")[0]
            if first and first not in RESERVED and NAME.match(first) and "." not in first:
                if first not in invited(self.settings):
                    return await JSONResponse({"error": "not found"}, status_code=404)(
                        scope, receive, send)
                app = self._copy(first)
                # Starlette routes on the path minus root_path, so keep the full path and
                # add the visitor's name to root_path.
                root = scope.get("root_path", "")
                scope = dict(scope, root_path=root + "/" + first)
                return await app(scope, receive, send)
        return await self.main(scope, receive, send)
