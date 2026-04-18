"""Application configuration loaded from environment variables.

All family-assistant-specific env vars are prefixed with FA_. The values are
validated by pydantic-settings and exposed via ``get_settings()`` as a cached
singleton so every subsystem (API, Alembic migrations, CLI tools) reads the
exact same values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    FA_DB_HOST: str = "localhost"
    FA_DB_PORT: int = 5432
    FA_DB_USER: str = "family_assistant"
    FA_DB_PWD: str = ""
    FA_DB_NAME: str = "family_assistant"

    FA_ENCRYPTION_KEY: str = Field(
        default="",
        description=(
            "url-safe base64 32-byte Fernet key used to encrypt sensitive "
            "columns (SSNs, account numbers, VINs, etc.)."
        ),
    )

    FA_STORAGE_ROOT: str = "./resources/family"
    FA_CORS_ORIGINS: str = "http://localhost:5173"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.FA_DB_USER}:{self.FA_DB_PWD}"
            f"@{self.FA_DB_HOST}:{self.FA_DB_PORT}/{self.FA_DB_NAME}"
        )

    @property
    def storage_root(self) -> Path:
        p = Path(self.FA_STORAGE_ROOT)
        if not p.is_absolute():
            p = (PROJECT_ROOT / p).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.FA_CORS_ORIGINS.split(",") if o.strip()]

    @field_validator("FA_ENCRYPTION_KEY")
    @classmethod
    def _warn_if_unset(cls, v: str) -> str:
        # Allow empty at import time so alembic/CLI tools can run, but the
        # crypto module will hard-fail on first use if still unset.
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
