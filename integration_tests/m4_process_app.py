from __future__ import annotations

import argparse
import os
import socket
import time
from dataclasses import replace
from pathlib import Path

import uvicorn

from legal_rag.api.app import create_app
from legal_rag.api.settings import load_environment_configuration
from legal_rag.services.run_executor import DeterministicRunExecutor
from legal_rag.services.run_service import RunService
from legal_rag.services.supervisor import RunSupervisor
from legal_rag.storage.database import create_database_engine


BLOCKING_QUESTION = "M4_PROCESS_RESTART_BLOCKING_SENTINEL"


class _ProcessFixtureExecutor:
    def __init__(self, *, blocking_enabled: bool) -> None:
        self.blocking_enabled = blocking_enabled
        self.deterministic = DeterministicRunExecutor("进程重启安全回答")

    def execute(self, execution_input, emit_event):
        if self.blocking_enabled and execution_input.question == BLOCKING_QUESTION:
            while True:
                time.sleep(1.0)
        return self.deterministic.execute(execution_input, emit_event)


def create_test_app():
    database, authenticator, settings = load_environment_configuration()
    # This executable is the cumulative M4 restart fixture. Keep its legacy
    # deterministic executor on the M4 graph identity so M5 production
    # readiness remains fail-closed when a PostgreSQL checkpointer is absent.
    settings = replace(settings, graph_version="m4-linear-v1")
    engine = create_database_engine(database)
    service = RunService(engine)
    executor = _ProcessFixtureExecutor(
        blocking_enabled=os.environ.get("M4_TEST_EXECUTOR_MODE") == "blocking"
    )
    supervisor = RunSupervisor(
        service,
        executor,
        lease_seconds=settings.lease_seconds,
        execution_timeout_seconds=settings.executor_timeout_seconds,
        poll_seconds=settings.supervisor_poll_seconds,
    )
    return create_app(
        service=service,
        authenticator=authenticator,
        supervisor=supervisor,
        settings=settings,
        close_engine=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", type=Path, required=True)
    args = parser.parse_args(argv)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    port = int(server_socket.getsockname()[1])
    args.port_file.write_text(f"{port}\n", encoding="ascii", newline="\n")
    server = uvicorn.Server(
        uvicorn.Config(
            create_test_app(),
            host="127.0.0.1",
            log_level="warning",
            access_log=False,
            server_header=False,
            lifespan="on",
        )
    )
    server.run(sockets=[server_socket])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
