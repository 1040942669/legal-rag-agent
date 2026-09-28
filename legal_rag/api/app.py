from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Security,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import and_, select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from legal_rag.services.run_service import (
    ActiveRunConflictError,
    IdempotencyConflictError,
    InvalidRunStateError,
    ResourceNotFoundError,
    ResumeUnsupportedError,
    RunConfigurationUnavailableError,
    RunRecord,
    RunService,
    ServiceContractError,
    SessionInactiveError,
)
from legal_rag.services.supervisor import RunSupervisor
from legal_rag.storage.schema import active_snapshot_pointers, embedding_imports

from .auth import AuthenticationError, ServicePrincipal, TokenAuthenticator
from .schemas import (
    CancelRunResponse,
    MessagePageResponse,
    MessageResponse,
    RunAcceptedResponse,
    RunCreateRequest,
    RunResponse,
    SessionCreateRequest,
    SessionResponse,
)
from .settings import ServiceSettings


M4_ALEMBIC_HEAD = "0005_m4_api_sessions"
STREAM_END_RUN_STATUSES = frozenset(
    {"interrupted", "succeeded", "failed", "cancelled"}
)
TERMINAL_EVENT_TYPES = frozenset(
    {"answer.final", "run.failed", "run.cancelled", "run.interrupted"}
)
URL_CREDENTIAL_NAMES = frozenset({"token", "access_token", "authorization"})


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _message_response(record) -> MessageResponse:
    return MessageResponse(
        message_id=record.message_id,
        session_id=record.session_id,
        run_id=record.run_id,
        role=record.role,
        content=record.content,
        ordinal=record.ordinal,
        created_at=record.created_at,
    )


def _run_response(record: RunRecord) -> RunResponse:
    result = record.result
    return RunResponse(
        run_id=record.run_id,
        session_id=record.session_id,
        status=record.status,
        snapshot_id=record.snapshot_id,
        snapshot_revision=record.snapshot_revision,
        activation_id=record.activation_id,
        profile_id=record.profile_id,
        graph_version=record.graph_version,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        error_code=record.error_code,
        answer=dict(result.answer_payload) if result is not None else None,
        evidence=dict(result.evidence_payload) if result is not None else None,
        verification=(
            dict(result.verification_payload) if result is not None else None
        ),
    )


def _parse_sse_cursor(last_event_id: str | None, after: str | None) -> int:
    raw = last_event_id if last_event_id is not None else after
    if raw is None:
        return 0
    if not raw.isascii() or not raw.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_event_cursor",
                "message": "event cursor is invalid",
            },
        )
    value = int(raw)
    if value < 0 or value > 9_223_372_036_854_775_807:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_event_cursor",
                "message": "event cursor is invalid",
            },
        )
    return value


def create_app(
    *,
    service: RunService,
    authenticator: TokenAuthenticator,
    supervisor: RunSupervisor,
    settings: ServiceSettings | None = None,
    start_supervisor: bool = True,
    close_engine: bool = False,
) -> FastAPI:
    resolved_settings = settings or ServiceSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if start_supervisor:
            await run_in_threadpool(supervisor.start)
        try:
            yield
        finally:
            supervisor_stopped = True
            if start_supervisor:
                supervisor_stopped = await run_in_threadpool(supervisor.stop)
            if close_engine and supervisor_stopped:
                await run_in_threadpool(service.engine.dispose)
            elif not supervisor_stopped:
                # Keep the engine valid for a loop that did not acknowledge
                # shutdown.  The hosting process may now terminate safely,
                # but disposing live database dependencies would create a
                # late-write race inside that still-running loop.
                app.state.shutdown_incomplete = True

    app = FastAPI(
        title="Legal RAG M4 Service",
        version="0.5.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.run_service = service
    app.state.authenticator = authenticator
    app.state.supervisor = supervisor
    app.state.settings = resolved_settings

    bearer_scheme = HTTPBearer(
        auto_error=False,
        bearerFormat="opaque",
        description="Server-issued opaque bearer token.",
    )

    @app.middleware("http")
    async def reject_url_credentials(request: Request, call_next):
        if any(key.casefold() in URL_CREDENTIAL_NAMES for key in request.query_params):
            return _error(
                400,
                "credential_in_url_forbidden",
                "credentials must be sent in the Authorization header",
            )
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        del request
        detail = exc.detail
        if (
            isinstance(detail, dict)
            and isinstance(detail.get("code"), str)
            and isinstance(detail.get("message"), str)
        ):
            code = detail["code"]
            message = detail["message"]
        else:
            code = {
                400: "invalid_request",
                401: "authentication_required",
                403: "forbidden",
                404: "resource_not_found",
                405: "method_not_allowed",
                422: "validation_error",
            }.get(exc.status_code, "http_error")
            message = {
                400: "the request is invalid",
                401: "authentication is required",
                403: "the request is forbidden",
                404: "resource was not found",
                405: "the method is not allowed",
                422: "request validation failed",
            }.get(exc.status_code, "the request could not be completed")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message}},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError):
        del request, exc
        return _error(422, "validation_error", "request validation failed")

    @app.exception_handler(ResourceNotFoundError)
    async def resource_not_found_handler(request: Request, exc: ResourceNotFoundError):
        del request, exc
        return _error(404, "resource_not_found", "resource was not found")

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict_handler(request: Request, exc):
        del request, exc
        return _error(
            409,
            "idempotency_conflict",
            "idempotency key is bound to a different request",
        )

    @app.exception_handler(ActiveRunConflictError)
    async def active_run_conflict_handler(request: Request, exc):
        del request, exc
        return _error(
            409, "active_run_conflict", "the session already has an active run"
        )

    @app.exception_handler(ResumeUnsupportedError)
    async def resume_unsupported_handler(request: Request, exc):
        del request, exc
        return _error(
            501,
            "resume_not_supported_until_m5",
            "run resume is not supported in M4",
        )

    @app.exception_handler(RunConfigurationUnavailableError)
    async def run_configuration_handler(request: Request, exc):
        del request, exc
        return _error(
            409,
            "run_configuration_unavailable",
            "the requested retrieval configuration is unavailable",
        )

    @app.exception_handler(SessionInactiveError)
    async def session_inactive_handler(request: Request, exc):
        del request, exc
        return _error(409, "session_inactive", "the session is not active")

    @app.exception_handler(InvalidRunStateError)
    async def invalid_run_state_handler(request: Request, exc):
        del request, exc
        return _error(
            409, "invalid_run_state", "the run state does not allow this operation"
        )

    @app.exception_handler(ServiceContractError)
    async def service_contract_handler(request: Request, exc):
        del request, exc
        return _error(
            422, "invalid_request", "the request violates the service contract"
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, exc):
        del request, exc
        return _error(503, "database_unavailable", "the database is unavailable")

    async def principal_dependency(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    ) -> ServicePrincipal:
        authorization = (
            None
            if credentials is None
            else f"{credentials.scheme} {credentials.credentials}"
        )
        try:
            return authenticator.authenticate_header(authorization)
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "authentication_required", "message": str(exc)},
                headers={"WWW-Authenticate": "Bearer"},
            ) from None

    def readiness_probe() -> bool:
        with service.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            migration = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            if migration != M4_ALEMBIC_HEAD:
                return False
            for principal in authenticator.principals:
                configured = connection.scalar(
                    select(active_snapshot_pointers.c.snapshot_id)
                    .select_from(
                        active_snapshot_pointers.join(
                            embedding_imports,
                            and_(
                                embedding_imports.c.snapshot_id
                                == active_snapshot_pointers.c.snapshot_id,
                                embedding_imports.c.profile_id == principal.profile_id,
                            ),
                        )
                    )
                    .where(
                        active_snapshot_pointers.c.scope_id == principal.scope_id,
                        embedding_imports.c.status == "validated",
                    )
                )
                if configured is None:
                    return False
        return not start_supervisor or supervisor.is_ready

    @app.get("/health/live", name="health_live")
    async def health_live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", name="health_ready")
    async def health_ready():
        try:
            ready = await run_in_threadpool(readiness_probe)
        except SQLAlchemyError:
            return _error(503, "database_unavailable", "the database is unavailable")
        if not ready:
            return _error(503, "service_not_ready", "the service is not ready")
        return {"status": "ready"}

    @app.post(
        "/api/v1/sessions",
        response_model=SessionResponse,
        status_code=201,
        name="create_session",
    )
    async def create_session_route(
        body: SessionCreateRequest,
        principal: ServicePrincipal = Depends(principal_dependency),
    ) -> SessionResponse:
        record = await run_in_threadpool(service.create_session, principal, body.title)
        return SessionResponse(
            session_id=record.session_id,
            title=record.title,
            status=record.status,
            created_at=record.created_at,
        )

    @app.get(
        "/api/v1/sessions/{session_id}/messages",
        response_model=MessagePageResponse,
        name="list_messages",
    )
    async def list_messages_route(
        session_id: str,
        limit: int = Query(
            default=min(50, resolved_settings.message_page_max),
            ge=1,
            le=resolved_settings.message_page_max,
        ),
        after_ordinal: int = Query(default=0, ge=0),
        principal: ServicePrincipal = Depends(principal_dependency),
    ) -> MessagePageResponse:
        page = await run_in_threadpool(
            service.list_messages,
            principal,
            session_id,
            after_ordinal=after_ordinal,
            limit=limit,
        )
        return MessagePageResponse(
            items=[_message_response(item) for item in page.messages],
            next_after_ordinal=page.next_after_ordinal,
        )

    @app.post(
        "/api/v1/sessions/{session_id}/runs",
        response_model=RunAcceptedResponse,
        status_code=202,
        name="create_run",
    )
    async def create_run_route(
        session_id: str,
        body: RunCreateRequest,
        request: Request,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
        ),
        principal: ServicePrincipal = Depends(principal_dependency),
    ) -> RunAcceptedResponse:
        if (
            idempotency_key is None
            or not idempotency_key
            or idempotency_key != idempotency_key.strip()
            or len(idempotency_key) > 255
            or any(
                ord(character) < 32 or 127 <= ord(character) <= 159
                for character in idempotency_key
            )
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_idempotency_key",
                    "message": "Idempotency-Key is required and invalid",
                },
            )
        if len(body.question) > resolved_settings.question_max_characters:
            return _error(
                422, "input_too_long", "question exceeds the configured limit"
            )
        payload = body.model_dump(mode="json", exclude_none=True)
        record, replayed = await run_in_threadpool(
            service.create_run,
            principal,
            session_id,
            idempotency_key,
            payload,
            resolved_settings.graph_version,
        )
        if not replayed:
            supervisor.wake()
        return RunAcceptedResponse(
            run_id=record.run_id,
            status=record.status,
            replayed=replayed,
            status_url=str(request.url_for("get_run", run_id=record.run_id)),
            events_url=str(request.url_for("stream_run_events", run_id=record.run_id)),
        )

    @app.get(
        "/api/v1/runs/{run_id}",
        response_model=RunResponse,
        name="get_run",
    )
    async def get_run_route(
        run_id: str,
        principal: ServicePrincipal = Depends(principal_dependency),
    ) -> RunResponse:
        record = await run_in_threadpool(service.get_run, principal, run_id)
        return _run_response(record)

    @app.get("/api/v1/runs/{run_id}/evidence", name="get_run_evidence")
    async def get_run_evidence_route(
        run_id: str,
        principal: ServicePrincipal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        record = await run_in_threadpool(service.get_run, principal, run_id)
        evidence = dict(record.result.evidence_payload) if record.result else None
        return {"run_id": record.run_id, "evidence": evidence}

    @app.post(
        "/api/v1/runs/{run_id}/cancel",
        response_model=CancelRunResponse,
        name="cancel_run",
    )
    async def cancel_run_route(
        run_id: str,
        principal: ServicePrincipal = Depends(principal_dependency),
    ) -> CancelRunResponse:
        record = await run_in_threadpool(service.cancel_run, principal, run_id)
        return CancelRunResponse(run_id=record.run_id, status=record.status)

    @app.post("/api/v1/runs/{run_id}/resume", name="resume_run")
    async def resume_run_route(
        run_id: str,
        principal: ServicePrincipal = Depends(principal_dependency),
    ):
        await run_in_threadpool(service.resume_unsupported, principal, run_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "resume_contract_violation",
                "message": "run resume unexpectedly returned",
            },
        )

    @app.get("/api/v1/runs/{run_id}/events", name="stream_run_events")
    async def stream_run_events_route(
        run_id: str,
        request: Request,
        after: str | None = Query(default=None),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        principal: ServicePrincipal = Depends(principal_dependency),
    ) -> StreamingResponse:
        cursor = _parse_sse_cursor(last_event_id, after)
        await run_in_threadpool(service.assert_owned_run, principal, run_id)

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            last_heartbeat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                events = await run_in_threadpool(
                    service.list_events,
                    principal,
                    run_id,
                    after_sequence=cursor,
                    limit=100,
                )
                terminal_seen = False
                for event in events:
                    cursor = event.sequence
                    data = json.dumps(
                        {
                            "run_id": event.run_id,
                            "sequence": event.sequence,
                            "payload": dict(event.safe_payload),
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"
                    terminal_seen = (
                        terminal_seen or event.event_type in TERMINAL_EVENT_TYPES
                    )
                if terminal_seen:
                    return
                record = await run_in_threadpool(service.get_run, principal, run_id)
                if record.status in STREAM_END_RUN_STATUSES:
                    if cursor >= record.event_sequence:
                        return
                    continue
                now = time.monotonic()
                if now - last_heartbeat >= resolved_settings.sse_heartbeat_seconds:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                await asyncio.sleep(resolved_settings.sse_poll_seconds)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


__all__ = ["M4_ALEMBIC_HEAD", "create_app"]
