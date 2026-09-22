from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine


def alembic_config(*, connection=None) -> Config:
    package_root = Path(__file__).resolve().parent
    repository_root = package_root.parents[1]
    config_path = repository_root / "alembic.ini"
    config = Config(str(config_path) if config_path.is_file() else None)
    config.set_main_option("script_location", str(package_root / "alembic"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def upgrade_database(engine: Engine, revision: str = "head") -> None:
    """Upgrade using an existing connection so credentials are never logged."""

    with engine.begin() as connection:
        command.upgrade(alembic_config(connection=connection), revision)
