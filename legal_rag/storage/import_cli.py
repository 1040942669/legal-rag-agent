from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping

from .import_workflow import (
    StorageImportWorkflowError,
    apply_prepared_import,
    build_import_plan,
    machine_json,
    validate_database_env_name,
    validate_import_plan,
    validation_receipt,
    write_machine_artifact,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m legal_rag.storage.import_cli",
        description=(
            "Plan, validate, and apply a fail-closed M3 PostgreSQL corpus import. "
            "The command never calls a model and never activates a snapshot."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser(
        "plan",
        aliases=["dry-run"],
        help="Build a deterministic machine-readable plan without a database.",
    )
    _add_local_inputs(plan)
    plan.add_argument(
        "--output",
        default=None,
        help="Optional new JSON file. Existing files are never overwritten.",
    )
    plan.set_defaults(handler=_handle_plan)

    validate = commands.add_parser(
        "validate",
        help="Rebuild and compare a plan without connecting to a database.",
    )
    _add_local_inputs(validate)
    validate.add_argument("--plan", required=True, help="Plan JSON created by plan.")
    validate.set_defaults(handler=_handle_validate)

    apply = commands.add_parser(
        "apply",
        help=(
            "Validate a plan, import immutable rows transactionally, and read them "
            "back for verification. Does not activate the snapshot."
        ),
    )
    _add_local_inputs(apply)
    apply.add_argument("--plan", required=True, help="Plan JSON created by plan.")
    apply.add_argument(
        "--database-url-env",
        default="LEGAL_RAG_DATABASE_URL",
        help="Environment variable containing a postgresql+psycopg URL.",
    )
    apply.add_argument(
        "--migrate",
        action="store_true",
        help="Explicitly upgrade the target database to the packaged schema first.",
    )
    apply.add_argument(
        "--receipt",
        default=None,
        help="Optional new JSON receipt file. Existing files are never overwritten.",
    )
    apply.set_defaults(handler=_handle_apply)
    return parser


def _add_local_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest",
        required=True,
        help="Strict local import request manifest.",
    )
    parser.add_argument(
        "--source-root",
        default=None,
        help=(
            "Root containing every referenced source artifact. Defaults to the "
            "manifest directory; request paths must be relative and cannot escape it."
        ),
    )


def _emit(value: Mapping[str, Any], output: str | None = None) -> None:
    if output is not None:
        write_machine_artifact(output, value)
    print(machine_json(value))


def _handle_plan(args: argparse.Namespace) -> int:
    prepared = build_import_plan(args.manifest, source_root=args.source_root)
    _emit(prepared.plan, args.output)
    return 0


def _handle_validate(args: argparse.Namespace) -> int:
    prepared = validate_import_plan(
        args.manifest,
        args.plan,
        source_root=args.source_root,
    )
    _emit(validation_receipt(prepared))
    return 0


def _handle_apply(args: argparse.Namespace) -> int:
    # Revalidate every local artifact before resolving database settings or
    # running an explicitly requested migration. Invalid input must remain a
    # strictly offline failure with no database side effects.
    prepared = validate_import_plan(
        args.manifest,
        args.plan,
        source_root=args.source_root,
    )
    environment_variable = validate_database_env_name(args.database_url_env)
    # Database imports stay lazy so plan/validate and all legacy offline CLI
    # commands remain usable without the optional database dependency.
    from .database import (
        DatabaseSettings,
        create_database_engine,
    )
    from .migrations import upgrade_database

    settings = DatabaseSettings.from_env(environment_variable)
    engine = create_database_engine(settings)
    try:
        if args.migrate:
            upgrade_database(engine)
        receipt = apply_prepared_import(
            prepared,
            engine=engine,
            migration_applied=bool(args.migrate),
        )
    finally:
        engine.dispose()
    _emit(receipt, args.receipt)
    return 0


def _error_payload(exc: BaseException) -> dict[str, Any]:
    error_code = "unexpected_error"
    message = "storage import failed without exposing source data or credentials"
    if isinstance(exc, StorageImportWorkflowError):
        error_code = "invalid_local_artifact"
        message = str(exc)
    else:
        # Import lazily: even rendering an offline input error must not require
        # SQLAlchemy or psycopg to be installed.
        try:
            from .database import DatabaseConfigurationError
            from .repository import ImportConflictError
        except ModuleNotFoundError:
            DatabaseConfigurationError = ()  # type: ignore[assignment,misc]
            ImportConflictError = ()  # type: ignore[assignment,misc]
        if isinstance(exc, DatabaseConfigurationError):
            error_code = "database_configuration"
            message = str(exc)
        elif isinstance(exc, ImportConflictError):
            error_code = "immutable_import_conflict"
            message = str(exc)
        elif isinstance(exc, ModuleNotFoundError):
            error_code = "database_dependency_missing"
            message = "install the database optional dependency before apply"
    return {
        "schema_version": 1,
        "artifact_kind": "m3_storage_import_error",
        "status": "failed",
        "error_code": error_code,
        "message": message,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI emits a sanitized error envelope.
        print(
            json.dumps(
                _error_payload(exc),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through main().
    raise SystemExit(main())
