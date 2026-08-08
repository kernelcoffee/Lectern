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

    # In production a single container serves the built SPA from this directory
    # (set by the image to /app/static). Unset in dev — the Vite server hosts
    # the frontend and proxies /api here.
    static_dir: Path | None = None

    # Sensible defaults surfaced in the create-server wizard.
    default_memory_mb: int = 2048
    # Proxies are lightweight — Velocity runs comfortably in 512 MB.
    default_proxy_memory_mb: int = 512

    # Max size of a world archive imported at creation. Modded worlds get large
    # (Distant Horizons alone can add gigabytes of LOD cache), so the ceiling is
    # generous; override with LECTERN_MAX_WORLD_UPLOAD_MB if needed.
    max_world_upload_mb: int = 20480  # 20 GiB

    # Max size of a file uploaded through the file manager (big enough for a
    # world/modpack zip you then unzip in place). Override with
    # LECTERN_MAX_FILE_UPLOAD_MB.
    max_file_upload_mb: int = 2048  # 2 GiB

    # These are the app-level tunables editable at runtime from the Settings UI
    # (stored in the ``Setting`` table, layered over the env defaults above).
    # Deployment settings (data dir, host, port) stay env-only.

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
