from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from sqlalchemy import Engine, create_engine, make_url


class DatabaseConfigurationError(ValueError):
    """Raised for missing, unsafe, or unsupported database configuration."""


@dataclass(frozen=True)
class DatabaseSettings:
    url: str = field(repr=False)
    pool_pre_ping: bool = True
    connect_timeout_seconds: int = 10
    pool_timeout_seconds: float = 30.0
    statement_timeout_ms: int | None = None
    lock_timeout_ms: int | None = None

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
        if (
            type(self.connect_timeout_seconds) is not int
            or not 1 <= self.connect_timeout_seconds <= 300
        ):
            raise DatabaseConfigurationError(
                "connect_timeout_seconds must be between 1 and 300"
            )
        if (
            isinstance(self.pool_timeout_seconds, bool)
            or not isinstance(self.pool_timeout_seconds, (int, float))
            or not math.isfinite(self.pool_timeout_seconds)
            or not 0 < self.pool_timeout_seconds <= 300
        ):
            raise DatabaseConfigurationError(
                "pool_timeout_seconds must be greater than 0 and at most 300"
            )
        for name, value in (
            ("statement_timeout_ms", self.statement_timeout_ms),
            ("lock_timeout_ms", self.lock_timeout_ms),
        ):
            if value is not None and (
                type(value) is not int or not 1 <= value <= 3_600_000
            ):
                raise DatabaseConfigurationError(
                    f"{name} must be None or between 1 and 3600000"
                )

    @classmethod
    def from_env(
        cls,
        variable: str = "LEGAL_RAG_DATABASE_URL",
        *,
        pool_pre_ping: bool = True,
        connect_timeout_seconds: int = 10,
        pool_timeout_seconds: float = 30.0,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> DatabaseSettings:
        value = os.environ.get(variable, "").strip()
        if not value:
            raise DatabaseConfigurationError(f"{variable} is required")
        return cls(
            url=value,
            pool_pre_ping=pool_pre_ping,
            connect_timeout_seconds=connect_timeout_seconds,
            pool_timeout_seconds=pool_timeout_seconds,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )

    @property
    def redacted_url(self) -> str:
        return make_url(self.url).render_as_string(hide_password=True)


def create_database_engine(settings: DatabaseSettings) -> Engine:
    connect_args: dict[str, object] = {
        "connect_timeout": settings.connect_timeout_seconds,
    }
    options: list[str] = []
    if settings.statement_timeout_ms is not None:
        options.append(f"-c statement_timeout={settings.statement_timeout_ms}")
    if settings.lock_timeout_ms is not None:
        options.append(f"-c lock_timeout={settings.lock_timeout_ms}")
    if options:
        connect_args["options"] = " ".join(options)
    return create_engine(
        settings.url,
        pool_pre_ping=settings.pool_pre_ping,
        pool_timeout=settings.pool_timeout_seconds,
        connect_args=connect_args,
    )
