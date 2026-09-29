"""Operational entry points for the optional M6 dispatcher and worker."""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
import uuid
from typing import Any

from .dispatcher import CeleryPublisher, dispatch_once
from .worker import WorkerSettings, build_celery_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="legal-rag-jobs")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("worker", help="Run a Linux Celery prefork worker.")
    dispatch = commands.add_parser(
        "dispatch", help="Deliver PostgreSQL outbox messages."
    )
    dispatch.add_argument(
        "--once", action="store_true", help="Make one bounded dispatch pass."
    )
    dispatch.add_argument(
        "--poll",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Continue dispatching with this interval (0.1 to 60 seconds).",
    )
    dispatch.add_argument("--limit", type=int, default=20)
    return parser


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _dispatch(args: argparse.Namespace) -> int:
    if args.poll is not None and args.once:
        raise ValueError("--once and --poll are mutually exclusive")
    if args.poll is not None and not 0.1 <= args.poll <= 60:
        raise ValueError("--poll must be between 0.1 and 60 seconds")
    if not 1 <= args.limit <= 100:
        raise ValueError("--limit must be between 1 and 100")
    from legal_rag.storage.database import DatabaseSettings, create_database_engine
    from legal_rag.observability.config import (
        close_observer,
        observer_from_environment,
    )

    from .store import JobStore

    settings = WorkerSettings.from_env()
    engine = create_database_engine(
        DatabaseSettings.from_env(connect_timeout_seconds=5)
    )
    observer = None
    try:
        store = JobStore(engine)
        publisher = CeleryPublisher(build_celery_app(settings))
        try:
            observer = observer_from_environment()
        except Exception:  # noqa: BLE001 - optional exporter is not a delivery gate.
            observer = None
        dispatcher_id = (
            f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        )
        while True:
            report = dispatch_once(
                store,
                publisher,
                dispatcher_id=dispatcher_id,
                limit=args.limit,
                observer=observer,
            )
            _emit(
                {
                    "schema_version": 1,
                    "recovered": report.recovered,
                    "attempted": report.attempted,
                    "delivered": report.delivered,
                    "deferred": report.deferred,
                }
            )
            if args.poll is None:
                return 0
            time.sleep(args.poll)
    finally:
        close_observer(observer)
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "dispatch":
            return _dispatch(args)
        if platform.system() == "Windows":
            raise ValueError("Celery prefork workers require Linux or WSL2")
        settings = WorkerSettings.from_env()
        app = build_celery_app(settings)
        result = app.worker_main(
            [
                "worker",
                "--loglevel=INFO",
                "--pool=prefork",
                f"--concurrency={settings.concurrency}",
                "--queues=legal_rag_jobs",
            ]
        )
        return int(result or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI errors must not expose DSNs.
        _emit(
            {
                "schema_version": 1,
                "status": "failed",
                "error_code": (
                    "configuration_error"
                    if isinstance(exc, ValueError)
                    else "jobs_runtime_error"
                ),
            }
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
