from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, make_url, text

from legal_rag.storage.database import DatabaseSettings, create_database_engine
from legal_rag.storage.migrations import upgrade_database


@pytest.fixture(scope="session")
def integration_database_url() -> Iterator[str]:
    if os.environ.get("LEGAL_RAG_INTEGRATION_TEST") != "1":
        pytest.fail(
            "LEGAL_RAG_INTEGRATION_TEST=1 is required for destructive integration fixtures"
        )
    value = os.environ.get("LEGAL_RAG_DATABASE_URL", "").strip()
    if not value:
        pytest.fail(
            "LEGAL_RAG_DATABASE_URL is required for PostgreSQL integration tests"
        )
    parsed = make_url(value)
    if parsed.drivername != "postgresql+psycopg":
        pytest.fail("integration database must use postgresql+psycopg")
    if not parsed.database or not parsed.database.startswith("legal_rag_m3_test"):
        pytest.fail("integration database name must start with legal_rag_m3_test")
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("destructive integration database provisioning requires loopback")

    database_name = f"legal_rag_m3_test_{uuid.uuid4().hex[:12]}"
    if not re.fullmatch(r"[a-z0-9_]+", database_name):  # defensive identifier guard
        pytest.fail("generated integration database name is unsafe")
    admin_url = parsed.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        yield parsed.set(database=database_name).render_as_string(hide_password=False)
    finally:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
            )
        admin_engine.dispose()


@pytest.fixture(scope="session")
def migrated_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_database_engine(DatabaseSettings(integration_database_url))
    existing_tables = set(inspect(engine).get_table_names())
    if existing_tables:
        pytest.fail(
            "M3 empty-database migration requires a fresh dedicated database; "
            f"found tables: {sorted(existing_tables)}"
        )
    with engine.connect() as connection:
        extension_exists = connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        )
    if extension_exists:
        pytest.fail("fresh M3 integration database unexpectedly has vector installed")
    upgrade_database(engine)
    yield engine
    engine.dispose()
