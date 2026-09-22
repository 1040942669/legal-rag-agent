from __future__ import annotations

import os
from dataclasses import dataclass, field

from sqlalchemy import Engine, create_engine, make_url


class DatabaseConfigurationError(ValueError):
    """Raised for missing, unsafe, or unsupported database configuration."""


@dataclass(frozen=True)
class DatabaseSettings:
    url: str = field(repr=False)
    pool_pre_ping: bool = True

    def __post_init__(self) -> None:
        try:
            parsed = make_url(self.url)
        except Exception as exc:  # noqa: BLE001 - SQLAlchemy exposes several parse errors.
            raise DatabaseConfigurationError("database URL is invalid") from exc
        if parsed.drivername != "postgresql+psycopg":
            raise DatabaseConfigurationError(
                "database URL must use the postgresql+psycopg driver"
            )
        if not parsed.database:
            raise DatabaseConfigurationError("database URL must name a database")

    @classmethod
    def from_env(cls, variable: str = "LEGAL_RAG_DATABASE_URL") -> DatabaseSettings:
        value = os.environ.get(variable, "").strip()
        if not value:
            raise DatabaseConfigurationError(f"{variable} is required")
        return cls(url=value)

    @property
    def redacted_url(self) -> str:
        return make_url(self.url).render_as_string(hide_password=True)


def create_database_engine(settings: DatabaseSettings) -> Engine:
    return create_engine(settings.url, pool_pre_ping=settings.pool_pre_ping)
