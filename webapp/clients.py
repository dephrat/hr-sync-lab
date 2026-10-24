"""Wires the sync engine to the two mock systems inside one process."""
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient

from ethena.app import create_app as create_ethena
from hris.app import create_app as create_hris
from hrsync import engine
from hrsync.clients import EthenaClient, WorkdayClient
from hrsync.config import Settings
from hrsync.rules import Rules, load as load_rules


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
    rules: Rules


def make_bundle(settings: Settings) -> Bundle:
    hris_app = create_hris(settings.hris_db)
    ethena_app = create_ethena(settings.ethena_db, api_key=settings.ethena_api_key)
    hc = TestClient(hris_app)
    ec = TestClient(ethena_app)
    ec.auth = ("web", settings.ethena_api_key)
    rules_path = Path(settings.rules_path)
    # Fall back to the packaged file only for the default setting; a custom path that is
    # missing should fail loudly in load_rules.
    default = Path(engine.__file__).parent / "rules.default.json"
    if settings.rules_path == "hrsync/rules.default.json" and not rules_path.exists():
        rules_path = default
    return Bundle(settings, hris_app, ethena_app, WorkdayClient(hc), EthenaClient(ec),
                  load_rules(rules_path))
