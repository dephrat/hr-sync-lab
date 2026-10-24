"""Runtime settings read from environment variables."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    hris_url: str
    ethena_url: str
    ethena_api_key: str
    sync_db: str
    hris_db: str
    ethena_db: str
    rules_path: str
    web_host: str = "127.0.0.1"
    web_port: int = 8003

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            hris_url=env("HRIS_URL", "http://127.0.0.1:8001"),
            ethena_url=env("ETHENA_URL", "http://127.0.0.1:8002"),
            ethena_api_key=env("ETHENA_API_KEY", "test-key"),
            sync_db=env("SYNC_DB", "./data/sync.db"),
            hris_db=env("HRIS_DB", "./data/hris.db"),
            ethena_db=env("ETHENA_DB", "./data/ethena.db"),
            rules_path=env("RULES_PATH", "hrsync/rules.default.json"),
            web_host=env("WEB_HOST", "127.0.0.1"),
            web_port=int(env("WEB_PORT", "8003")),
        )
