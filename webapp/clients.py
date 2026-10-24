"""Wires the sync engine to the two mock systems inside one process."""
import json
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient

from ethena.app import create_app as create_ethena
from hris.app import create_app as create_hris
from hrsync import engine
from hrsync.clients import EthenaClient, WorkdayClient
from hrsync.config import Settings
from hrsync.rules import Rules, load as load_rules, save as rules_save


@dataclass
class Bundle:
    """The two mock apps plus the clients the engine uses to call them.

    The clients are `starlette.testclient.TestClient` instances, which are httpx clients
    that talk to an ASGI app in-process (the same thing the test suite uses). That is
    unusual in production; it is chosen so one process on one port serves the page, both
    mocks and the sync engine, with no sockets between them. They are created without
    `with` because neither mock app has a lifespan."""
    settings: Settings
    hris_app: FastAPI
    ethena_app: FastAPI
    hris: WorkdayClient
    learning: EthenaClient
    rules_path: Path

    @property
    def rules(self) -> Rules:
        """Re-read from disk when the file changes, so edits and resets apply at once."""
        st = self.rules_path.stat()
        key = (st.st_mtime_ns, st.st_ino, st.st_size)
        if getattr(self, "_rules_key", None) != key:
            self._rules, self._rules_key = load_rules(self.rules_path), key
        return self._rules


def make_bundle(settings: Settings) -> Bundle:
    hris_app = create_hris(settings.hris_db)
    ethena_app = create_ethena(settings.ethena_db, api_key=settings.ethena_api_key)
    hc = TestClient(hris_app)
    ec = TestClient(ethena_app)
    ec.auth = ("web", settings.ethena_api_key)
    rules_path = Path(settings.web_rules_path)
    if not rules_path.exists():
        reset_rules(settings)
    return Bundle(settings, hris_app, ethena_app, WorkdayClient(hc), EthenaClient(ec),
                  rules_path)


def reset_rules(settings: Settings) -> None:
    """Copy the packaged rules over the web app's writable copy."""
    default = Path(engine.__file__).parent / "rules.default.json"
    dest = Path(settings.web_rules_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rules_save(dest, json.loads(default.read_text()))
