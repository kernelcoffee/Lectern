"""Application configuration.

Settings are read from environment variables (prefixed ``LECTERN_``) or an
optional ``.env`` file. The one setting that matters most is ``LECTERN_DATA`` —
the single directory that holds the database, downloaded runtimes, caches,
backups, and all server files.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LECTERN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Where all persistent state lives (db, java/, cache/, backups/, servers/).
    data: Path = Path("./data")

    # HTTP bind address for the API/uvicorn server.
    host: str = "0.0.0.0"
    port: int = 8000

    # Sensible defaults surfaced in the create-server wizard.
    default_memory_mb: int = 2048

    @property
    def db_path(self) -> Path:
        return self.data / "lectern.sqlite"

    @property
    def java_dir(self) -> Path:
        return self.data / "java"

    @property
    def cache_dir(self) -> Path:
        return self.data / "cache"

    @property
    def backups_dir(self) -> Path:
        return self.data / "backups"

    @property
    def servers_dir(self) -> Path:
        return self.data / "servers"

    def ensure_dirs(self) -> None:
        """Create the data directory tree if it does not yet exist."""
        for path in (
            self.data,
            self.java_dir,
            self.cache_dir,
            self.backups_dir,
            self.servers_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
