from __future__ import annotations

import argparse
from datetime import timedelta


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the optional M5 Legal RAG HTTP service.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Upgrade the configured PostgreSQL database before startup.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")

    # Service dependencies remain lazy so the legacy ``legal-rag`` command
    # never requires FastAPI, SQLAlchemy, or a database configuration.
    import uvicorn

    from legal_rag.harness.budget import HarnessBudgetConfig
    from legal_rag.harness.checkpoint import (
        assert_postgres_checkpointer_ready,
        setup_postgres_checkpointer,
    )
    from legal_rag.harness.runner import GraphRunExecutor
    from legal_rag.harness.state import HARNESS_GRAPH_VERSION
    from legal_rag.services.run_executor import LegalChatRunExecutor
    from legal_rag.services.run_service import RunService
    from legal_rag.services.service_retrieval import PostgresAssistantFactory
    from legal_rag.services.supervisor import RunSupervisor
    from legal_rag.storage.database import create_database_engine
    from legal_rag.storage.migrations import upgrade_database

    from .app import create_app
    from .settings import load_environment_configuration

    database, authenticator, settings = load_environment_configuration()
    engine = create_database_engine(database)
    if args.migrate:
        upgrade_database(engine)
        if settings.graph_version == HARNESS_GRAPH_VERSION:
            setup_postgres_checkpointer(engine)
    if settings.graph_version == HARNESS_GRAPH_VERSION:
        assert_postgres_checkpointer_ready(engine)
    service = RunService(
        engine,
        idempotency_ttl=timedelta(seconds=settings.idempotency_ttl_seconds),
        history_message_limit=settings.history_max_messages,
        history_character_limit=settings.history_max_characters,
        budget_config=HarnessBudgetConfig(
            max_retrieval_rounds=settings.max_retrieval_rounds,
            max_queries_per_round=settings.max_queries_per_round,
            max_tool_attempts=settings.max_tool_attempts,
            max_model_attempts=settings.max_model_attempts,
            max_embedding_attempts=settings.max_embedding_attempts,
            max_retry_per_operation=settings.max_retry_per_operation,
            execution_deadline_seconds=settings.execution_deadline_seconds,
            evidence_top_k=settings.evidence_top_k,
        ),
    )
    assistant_factory = PostgresAssistantFactory(engine)
    if settings.graph_version == HARNESS_GRAPH_VERSION:
        executor = GraphRunExecutor(
            service,
            assistant_factory,
            generate=False,
        )
    else:
        executor = LegalChatRunExecutor(
            assistant_factory,
            generate=False,
        )
    supervisor = RunSupervisor(
        service,
        executor,
        lease_seconds=settings.lease_seconds,
        execution_timeout_seconds=settings.executor_timeout_seconds,
        poll_seconds=settings.supervisor_poll_seconds,
    )
    app = create_app(
        service=service,
        authenticator=authenticator,
        supervisor=supervisor,
        settings=settings,
        close_engine=True,
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
