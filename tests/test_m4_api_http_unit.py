from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from legal_rag.api.app import create_app
from legal_rag.api.auth import ServicePrincipal, TokenAuthenticator
from legal_rag.api.settings import ServiceSettings
from legal_rag.services.run_service import (
    MessagePage,
    MessageRecord,
    ResourceNotFoundError,
    ResumeUnsupportedError,
    RunEventRecord,
    RunRecord,
    RunResultRecord,
    SessionRecord,
)


NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
OWNER_TOKEN = "owner-token-" + "a" * 32
FOREIGN_TOKEN = "foreign-token-" + "b" * 32
OWNER = ServicePrincipal(
    user_id="user-owner",
    scope_id="scope-owner",
    profile_id="a" * 64,
)
FOREIGN = ServicePrincipal(
    user_id="user-foreign",
    scope_id="scope-foreign",
    profile_id="b" * 64,
)
SESSION_ID = "session-owner"
RUN_ID = "run-owner"
DATABASE_ERROR_RUN_ID = "run-database-error"


def _authorization(token: str = OWNER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _error_code(response) -> str:
    payload = response.json()
    detail = payload.get("error", payload.get("detail"))
    assert isinstance(detail, dict), payload
    return detail["code"]


def _run_record(
    *,
    status: str = "succeeded",
    result: RunResultRecord | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        user_id=OWNER.user_id,
        status=status,
        request_hash="1" * 64,
        request_payload={"question": "Which rule applies?", "retrieval": {"top_k": 5}},
        scope_id=OWNER.scope_id,
        snapshot_id="snapshot-2026",
        snapshot_revision=7,
        activation_id="activation-7",
        profile_id=OWNER.profile_id,
        boundary_fingerprint="2" * 64,
        retrieval_config_hash="3" * 64,
        graph_version="m4-linear-v1",
        created_at=NOW,
        queued_at=NOW,
        started_at=NOW if status != "queued" else None,
        finished_at=NOW if status in {"succeeded", "failed", "cancelled"} else None,
        updated_at=NOW,
        error_code=None,
        revision=3,
        event_sequence=4,
        lease_owner=None,
        lease_expires_at=None,
        result=result,
    )


class _FakeEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


class _FakeSupervisor:
    def __init__(self, *, stop_result: bool = True) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.wake_calls = 0
        self.is_alive = True
        self.stop_result = stop_result

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> bool:
        self.stop_calls += 1
        return self.stop_result

    def wake(self) -> None:
        self.wake_calls += 1


class _FakeRunService:
    def __init__(self) -> None:
        self.engine = _FakeEngine()
        self.calls: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        self.session = SessionRecord(
            session_id=SESSION_ID,
            user_id=OWNER.user_id,
            title="Owner session",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
        self.result = RunResultRecord(
            run_id=RUN_ID,
            final_message_id="message-3",
            answer_text="Article 7 applies.",
            answer_payload={"text": "Article 7 applies."},
            evidence_payload={"citations": [{"article": 7}]},
            verification_payload={"accepted": True},
            created_at=NOW,
        )
        self.run = _run_record(result=self.result)
        self.messages = (
            MessageRecord(
                message_id="message-2",
                session_id=SESSION_ID,
                user_id=OWNER.user_id,
                role="user",
                content="Which rule applies?",
                run_id=RUN_ID,
                ordinal=2,
                created_at=NOW,
            ),
            MessageRecord(
                message_id="message-3",
                session_id=SESSION_ID,
                user_id=OWNER.user_id,
                role="assistant",
                content="Article 7 applies.",
                run_id=RUN_ID,
                ordinal=3,
                created_at=NOW,
            ),
        )
        self.events = (
            RunEventRecord(
                run_id=RUN_ID,
                sequence=1,
                event_type="run.queued",
                safe_payload={"status": "queued"},
                created_at=NOW,
            ),
            RunEventRecord(
                run_id=RUN_ID,
                sequence=2,
                event_type="run.started",
                safe_payload={"status": "running"},
                created_at=NOW,
            ),
            RunEventRecord(
                run_id=RUN_ID,
                sequence=3,
                event_type="verification.completed",
                safe_payload={"accepted": True},
                created_at=NOW,
            ),
            RunEventRecord(
                run_id=RUN_ID,
                sequence=4,
                event_type="answer.final",
                safe_payload={"status": "succeeded"},
                created_at=NOW,
            ),
        )

    @staticmethod
    def _require_owner(
        principal: ServicePrincipal,
        resource_id: str,
        expected_id: str,
    ) -> None:
        if principal.user_id != OWNER.user_id or resource_id != expected_id:
            raise ResourceNotFoundError()

    def create_session(
        self,
        principal: ServicePrincipal,
        title: str | None,
    ) -> SessionRecord:
        self.calls["create_session"].append((principal, title))
        return replace(self.session, title=title)

    def list_messages(
        self,
        principal: ServicePrincipal,
        session_id: str,
        *,
        after_ordinal: int,
        limit: int,
    ) -> MessagePage:
        self.calls["list_messages"].append(
            (principal, session_id, after_ordinal, limit)
        )
        self._require_owner(principal, session_id, SESSION_ID)
        return MessagePage(
            messages=self.messages[:limit],
            next_after_ordinal=3,
            has_more=True,
        )

    def create_run(
        self,
        principal: ServicePrincipal,
        session_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        graph_version: str,
    ) -> tuple[RunRecord, bool]:
        self.calls["create_run"].append(
            (
                principal,
                session_id,
                idempotency_key,
                payload,
                graph_version,
            )
        )
        self._require_owner(principal, session_id, SESSION_ID)
        return _run_record(status="queued"), False

    def get_run(
        self,
        principal: ServicePrincipal,
        run_id: str,
    ) -> RunRecord:
        self.calls["get_run"].append((principal, run_id))
        if principal.user_id == OWNER.user_id and run_id == DATABASE_ERROR_RUN_ID:
            raise SQLAlchemyError(
                "postgresql://service:super-secret@database.invalid/legal_rag"
            )
        self._require_owner(principal, run_id, RUN_ID)
        return self.run

    def cancel_run(
        self,
        principal: ServicePrincipal,
        run_id: str,
    ) -> RunRecord:
        self.calls["cancel_run"].append((principal, run_id))
        self._require_owner(principal, run_id, RUN_ID)
        return replace(self.run, status="cancelled", result=None)

    def resume_unsupported(
        self,
        principal: ServicePrincipal,
        run_id: str,
    ) -> None:
        self.calls["resume_unsupported"].append((principal, run_id))
        self._require_owner(principal, run_id, RUN_ID)
        raise ResumeUnsupportedError("M5 checkpoint recovery is required")

    def assert_owned_run(
        self,
        principal: ServicePrincipal,
        run_id: str,
    ) -> RunRecord:
        self.calls["assert_owned_run"].append((principal, run_id))
        self._require_owner(principal, run_id, RUN_ID)
        return self.run

    def list_events(
        self,
        principal: ServicePrincipal,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[RunEventRecord, ...]:
        self.calls["list_events"].append((principal, run_id, after_sequence, limit))
        self._require_owner(principal, run_id, RUN_ID)
        return tuple(event for event in self.events if event.sequence > after_sequence)[
            :limit
        ]


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    service: _FakeRunService
    supervisor: _FakeSupervisor


def _application(
    *,
    service: _FakeRunService | None = None,
    settings: ServiceSettings | None = None,
):
    fake_service = service or _FakeRunService()
    supervisor = _FakeSupervisor()
    authenticator = TokenAuthenticator(
        {
            OWNER_TOKEN: OWNER,
            FOREIGN_TOKEN: FOREIGN,
        }
    )
    app = create_app(
        service=fake_service,
        authenticator=authenticator,
        supervisor=supervisor,
        settings=settings or ServiceSettings(graph_version="m4-linear-v1"),
        start_supervisor=False,
    )
    return app, fake_service, supervisor


@pytest.fixture
def api() -> _Harness:
    app, service, supervisor = _application()
    with TestClient(app) as client:
        yield _Harness(client=client, service=service, supervisor=supervisor)


def test_live_is_public_and_does_not_start_the_supervisor(api: _Harness) -> None:
    response = api.client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}
    assert api.supervisor.start_calls == 0


def test_shutdown_keeps_engine_alive_when_supervisor_does_not_stop() -> None:
    service = _FakeRunService()
    supervisor = _FakeSupervisor(stop_result=False)
    app = create_app(
        service=service,
        authenticator=TokenAuthenticator({OWNER_TOKEN: OWNER}),
        supervisor=supervisor,
        start_supervisor=True,
        close_engine=True,
    )

    with TestClient(app):
        pass

    assert supervisor.start_calls == 1
    assert supervisor.stop_calls == 1
    assert service.engine.dispose_calls == 0
    assert app.state.shutdown_incomplete is True


def test_openapi_declares_bearer_auth_only_for_protected_routes(
    api: _Harness,
) -> None:
    response = api.client.get("/openapi.json")

    assert response.status_code == 200
    document = response.json()
    assert document["components"]["securitySchemes"] == {
        "HTTPBearer": {
            "type": "http",
            "description": "Server-issued opaque bearer token.",
            "scheme": "bearer",
            "bearerFormat": "opaque",
        }
    }
    assert document["paths"]["/api/v1/sessions"]["post"]["security"] == [
        {"HTTPBearer": []}
    ]
    assert document["paths"]["/api/v1/runs/{run_id}/events"]["get"]["security"] == [
        {"HTTPBearer": []}
    ]
    assert "security" not in document["paths"]["/health/live"]["get"]


def test_protected_routes_require_bearer_authentication(api: _Harness) -> None:
    response = api.client.post("/api/v1/sessions", json={})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert _error_code(response) == "authentication_required"
    assert api.service.calls["create_session"] == []


@pytest.mark.parametrize("credential_name", ["token", "access_token", "Authorization"])
def test_query_string_credentials_are_forbidden(
    api: _Harness,
    credential_name: str,
) -> None:
    response = api.client.post(
        "/api/v1/sessions",
        params={credential_name: "must-not-appear-in-a-url"},
        headers=_authorization(),
        json={},
    )

    assert response.status_code == 400
    assert _error_code(response) == "credential_in_url_forbidden"
    assert api.service.calls["create_session"] == []


@pytest.mark.parametrize("path", ["/health/live", "/openapi.json", "/not-found"])
def test_query_string_credentials_are_forbidden_on_public_and_unknown_routes(
    api: _Harness,
    path: str,
) -> None:
    response = api.client.get(path, params={"access_token": "must-not-be-in-url"})

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "credential_in_url_forbidden",
            "message": "credentials must be sent in the Authorization header",
        }
    }


def test_framework_http_and_request_validation_errors_use_the_error_envelope(
    api: _Harness,
) -> None:
    method_response = api.client.post("/health/live")
    validation_response = api.client.post(
        "/api/v1/sessions",
        headers=_authorization(),
        json={"unexpected": True},
    )

    assert method_response.status_code == 405
    assert method_response.json() == {
        "error": {
            "code": "method_not_allowed",
            "message": "the method is not allowed",
        }
    }
    assert validation_response.status_code == 422
    assert validation_response.json() == {
        "error": {
            "code": "validation_error",
            "message": "request validation failed",
        }
    }
    assert api.service.calls["create_session"] == []


def test_create_session_uses_bearer_identity_and_ignores_x_user_id(
    api: _Harness,
) -> None:
    headers = _authorization()
    headers["X-User-ID"] = FOREIGN.user_id

    response = api.client.post(
        "/api/v1/sessions",
        headers=headers,
        json={"title": "  Owner case  "},
    )

    assert response.status_code == 201
    assert response.json() == {
        "session_id": SESSION_ID,
        "title": "Owner case",
        "status": "active",
        "created_at": "2026-01-02T03:04:05Z",
    }
    assert api.service.calls["create_session"] == [(OWNER, "Owner case")]


def test_create_run_returns_202_and_absolute_status_and_event_locations(
    api: _Harness,
) -> None:
    headers = _authorization()
    headers["Idempotency-Key"] = "request-123"

    response = api.client.post(
        f"/api/v1/sessions/{SESSION_ID}/runs",
        headers=headers,
        json={
            "question": "  Which rule applies?  ",
            "snapshot_id": "snapshot-2026",
            "retrieval": {"top_k": 7},
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "run_id": RUN_ID,
        "status": "queued",
        "replayed": False,
        "status_url": f"http://testserver/api/v1/runs/{RUN_ID}",
        "events_url": f"http://testserver/api/v1/runs/{RUN_ID}/events",
    }
    assert api.service.calls["create_run"] == [
        (
            OWNER,
            SESSION_ID,
            "request-123",
            {
                "question": "Which rule applies?",
                "snapshot_id": "snapshot-2026",
                "retrieval": {"top_k": 7},
            },
            "m4-linear-v1",
        )
    ]
    assert api.supervisor.wake_calls == 1


def test_input_too_long_is_rejected_without_calling_the_service() -> None:
    app, service, supervisor = _application(
        settings=ServiceSettings(question_max_characters=5)
    )
    headers = _authorization()
    headers["Idempotency-Key"] = "request-too-long"

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/sessions/{SESSION_ID}/runs",
            headers=headers,
            json={"question": "123456"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "input_too_long",
            "message": "question exceeds the configured limit",
        }
    }
    assert service.calls["create_run"] == []
    assert supervisor.wake_calls == 0


def test_messages_map_page_items_and_forward_pagination(api: _Harness) -> None:
    response = api.client.get(
        f"/api/v1/sessions/{SESSION_ID}/messages",
        params={"after_ordinal": 1, "limit": 2},
        headers=_authorization(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "message_id": "message-2",
                "session_id": SESSION_ID,
                "run_id": RUN_ID,
                "role": "user",
                "content": "Which rule applies?",
                "ordinal": 2,
                "created_at": "2026-01-02T03:04:05Z",
            },
            {
                "message_id": "message-3",
                "session_id": SESSION_ID,
                "run_id": RUN_ID,
                "role": "assistant",
                "content": "Article 7 applies.",
                "ordinal": 3,
                "created_at": "2026-01-02T03:04:05Z",
            },
        ],
        "next_after_ordinal": 3,
    }
    assert api.service.calls["list_messages"] == [(OWNER, SESSION_ID, 1, 2)]


def test_message_page_limit_uses_the_configured_service_maximum() -> None:
    app, service, _ = _application(settings=ServiceSettings(message_page_max=2))

    with TestClient(app) as client:
        default_response = client.get(
            f"/api/v1/sessions/{SESSION_ID}/messages",
            headers=_authorization(),
        )
        oversized_response = client.get(
            f"/api/v1/sessions/{SESSION_ID}/messages",
            params={"limit": 3},
            headers=_authorization(),
        )

    assert default_response.status_code == 200
    assert service.calls["list_messages"] == [(OWNER, SESSION_ID, 0, 2)]
    assert oversized_response.status_code == 422
    assert oversized_response.json() == {
        "error": {
            "code": "validation_error",
            "message": "request validation failed",
        }
    }


def test_message_page_setting_cannot_exceed_the_run_service_contract() -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        ServiceSettings(message_page_max=101)


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/runs/{RUN_ID}",
        f"/api/v1/runs/{RUN_ID}/evidence",
    ],
)
def test_get_run_and_evidence_require_authentication(
    api: _Harness,
    path: str,
) -> None:
    response = api.client.get(path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert _error_code(response) == "authentication_required"


def test_get_run_and_evidence_map_only_the_owned_result(api: _Harness) -> None:
    run_response = api.client.get(f"/api/v1/runs/{RUN_ID}", headers=_authorization())
    evidence_response = api.client.get(
        f"/api/v1/runs/{RUN_ID}/evidence", headers=_authorization()
    )

    assert run_response.status_code == 200
    assert run_response.json() == {
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "status": "succeeded",
        "snapshot_id": "snapshot-2026",
        "snapshot_revision": 7,
        "activation_id": "activation-7",
        "profile_id": OWNER.profile_id,
        "graph_version": "m4-linear-v1",
        "created_at": "2026-01-02T03:04:05Z",
        "started_at": "2026-01-02T03:04:05Z",
        "finished_at": "2026-01-02T03:04:05Z",
        "error_code": None,
        "answer": {"text": "Article 7 applies."},
        "evidence": {"citations": [{"article": 7}]},
        "verification": {"accepted": True},
    }
    assert evidence_response.status_code == 200
    assert evidence_response.json() == {
        "run_id": RUN_ID,
        "evidence": {"citations": [{"article": 7}]},
    }
    assert api.service.calls["get_run"] == [
        (OWNER, RUN_ID),
        (OWNER, RUN_ID),
    ]


def test_foreign_resources_share_one_non_enumerating_404_response(
    api: _Harness,
) -> None:
    foreign_headers = _authorization(FOREIGN_TOKEN)
    run_headers = dict(foreign_headers)
    run_headers["Idempotency-Key"] = "foreign-request"
    requests = (
        api.client.get(
            f"/api/v1/sessions/{SESSION_ID}/messages",
            headers=foreign_headers,
        ),
        api.client.post(
            f"/api/v1/sessions/{SESSION_ID}/runs",
            headers=run_headers,
            json={"question": "Can I see it?"},
        ),
        api.client.get(f"/api/v1/runs/{RUN_ID}", headers=foreign_headers),
        api.client.get(f"/api/v1/runs/{RUN_ID}/evidence", headers=foreign_headers),
        api.client.post(f"/api/v1/runs/{RUN_ID}/cancel", headers=foreign_headers),
        api.client.get(f"/api/v1/runs/{RUN_ID}/events", headers=foreign_headers),
    )
    expected = {
        "error": {
            "code": "resource_not_found",
            "message": "resource was not found",
        }
    }

    assert all(response.status_code == 404 for response in requests)
    assert all(response.json() == expected for response in requests)


def test_cancel_maps_status_and_never_claims_provider_revocation(api: _Harness) -> None:
    response = api.client.post(
        f"/api/v1/runs/{RUN_ID}/cancel", headers=_authorization()
    )

    assert response.status_code == 200
    assert response.json() == {
        "run_id": RUN_ID,
        "status": "cancelled",
        "provider_revoked": False,
    }
    assert api.service.calls["cancel_run"] == [(OWNER, RUN_ID)]


def test_resume_is_501_for_owner_but_404_for_foreign_principal(
    api: _Harness,
) -> None:
    owner_response = api.client.post(
        f"/api/v1/runs/{RUN_ID}/resume", headers=_authorization()
    )
    foreign_response = api.client.post(
        f"/api/v1/runs/{RUN_ID}/resume",
        headers=_authorization(FOREIGN_TOKEN),
    )

    assert owner_response.status_code == 501
    assert owner_response.json() == {
        "error": {
            "code": "resume_not_supported_until_m5",
            "message": "run resume is not supported in M4",
        }
    }
    assert foreign_response.status_code == 404
    assert foreign_response.json() == {
        "error": {
            "code": "resource_not_found",
            "message": "resource was not found",
        }
    }
    assert api.service.calls["resume_unsupported"] == [
        (OWNER, RUN_ID),
        (FOREIGN, RUN_ID),
    ]


def test_resume_never_returns_a_success_if_the_service_contract_is_broken() -> None:
    service = _FakeRunService()
    service.resume_unsupported = lambda principal, run_id: None  # type: ignore[method-assign]
    app, _, _ = _application(service=service)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runs/{RUN_ID}/resume",
            headers=_authorization(),
        )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "resume_contract_violation",
            "message": "run resume unexpectedly returned",
        }
    }


@pytest.mark.parametrize("control_byte", [b"\x7f", b"\x85"])
def test_idempotency_key_rejects_del_and_c1_control_characters(
    api: _Harness,
    control_byte: bytes,
) -> None:
    headers = [
        (b"Authorization", _authorization()["Authorization"].encode("ascii")),
        (b"Idempotency-Key", b"request" + control_byte + b"key"),
    ]

    response = api.client.post(
        f"/api/v1/sessions/{SESSION_ID}/runs",
        headers=headers,
        json={"question": "Which rule applies?"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_idempotency_key",
            "message": "Idempotency-Key is required and invalid",
        }
    }
    assert api.service.calls["create_run"] == []


def test_sse_last_event_id_returns_the_strictly_ordered_suffix(
    api: _Harness,
) -> None:
    headers = _authorization()
    headers["Last-Event-ID"] = "2"

    response = api.client.get(
        f"/api/v1/runs/{RUN_ID}/events",
        params={"after": "1"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = response.text.strip().split("\n\n")
    assert len(frames) == 2
    assert frames[0].splitlines()[:2] == [
        "id: 3",
        "event: verification.completed",
    ]
    assert frames[1].splitlines()[:2] == ["id: 4", "event: answer.final"]
    first_data = json.loads(frames[0].splitlines()[2].removeprefix("data: "))
    second_data = json.loads(frames[1].splitlines()[2].removeprefix("data: "))
    assert first_data == {
        "run_id": RUN_ID,
        "sequence": 3,
        "payload": {"accepted": True},
    }
    assert second_data == {
        "run_id": RUN_ID,
        "sequence": 4,
        "payload": {"status": "succeeded"},
    }
    assert api.service.calls["assert_owned_run"] == [(OWNER, RUN_ID)]
    assert api.service.calls["list_events"] == [(OWNER, RUN_ID, 2, 100)]


def test_sse_refetches_terminal_event_committed_after_an_empty_poll() -> None:
    class TerminalCommitRaceService(_FakeRunService):
        def __init__(self) -> None:
            super().__init__()
            self.run = _run_record(status="running", result=None)
            self.events = (self.events[-1],)
            self._first_poll = True

        def list_events(
            self,
            principal: ServicePrincipal,
            run_id: str,
            *,
            after_sequence: int,
            limit: int,
        ) -> tuple[RunEventRecord, ...]:
            self.calls["list_events"].append(
                (principal, run_id, after_sequence, limit)
            )
            self._require_owner(principal, run_id, RUN_ID)
            if self._first_poll:
                self._first_poll = False
                self.run = _run_record(result=self.result)
                return ()
            return tuple(
                event for event in self.events if event.sequence > after_sequence
            )[:limit]

    service = TerminalCommitRaceService()
    app, _, _ = _application(service=service)
    headers = _authorization()
    headers["Last-Event-ID"] = "3"

    with TestClient(app) as client:
        response = client.get(f"/api/v1/runs/{RUN_ID}/events", headers=headers)

    assert response.status_code == 200
    assert "id: 4\nevent: answer.final\n" in response.text
    assert service.calls["list_events"] == [
        (OWNER, RUN_ID, 3, 100),
        (OWNER, RUN_ID, 3, 100),
    ]


def test_sse_reconnect_after_interrupted_event_closes_without_heartbeating() -> None:
    service = _FakeRunService()
    service.run = replace(
        _run_record(status="interrupted", result=None),
        event_sequence=1,
    )
    service.events = (
        RunEventRecord(
            run_id=RUN_ID,
            sequence=1,
            event_type="run.interrupted",
            safe_payload={"status": "interrupted"},
            created_at=NOW,
        ),
    )
    app, _, _ = _application(service=service)
    headers = _authorization()
    headers["Last-Event-ID"] = "1"

    with TestClient(app) as client:
        response = client.get(f"/api/v1/runs/{RUN_ID}/events", headers=headers)

    assert response.status_code == 200
    assert response.text == ""
    assert service.calls["list_events"] == [(OWNER, RUN_ID, 1, 100)]
    assert service.calls["get_run"] == [(OWNER, RUN_ID)]


@pytest.mark.parametrize(
    "cursor",
    ["-1", "not-a-number", "9223372036854775808", "+12"],
)
def test_sse_rejects_an_invalid_cursor_before_service_access(
    api: _Harness,
    cursor: str,
) -> None:
    headers = _authorization()
    headers["Last-Event-ID"] = cursor

    response = api.client.get(
        f"/api/v1/runs/{RUN_ID}/events",
        headers=headers,
    )

    assert response.status_code == 400
    assert _error_code(response) == "invalid_event_cursor"
    assert api.service.calls["assert_owned_run"] == []
    assert api.service.calls["list_events"] == []


def test_database_exception_handler_returns_only_a_redacted_error(
    api: _Harness,
) -> None:
    response = api.client.get(
        f"/api/v1/runs/{DATABASE_ERROR_RUN_ID}",
        headers=_authorization(),
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "database_unavailable",
            "message": "the database is unavailable",
        }
    }
    assert "super-secret" not in response.text
    assert "database.invalid" not in response.text
